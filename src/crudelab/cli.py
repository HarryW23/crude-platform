"""命令行入口。

  crudelab init                       升級表結構並同步 config/ 下的配置
  crudelab check                      只校驗 config/，不碰數據庫
  crudelab serve [--port 8000]        啟動後台
  crudelab import 文件 [--source …]   導入 Excel / CSV（只入庫能自動識別的列）
  crudelab pull eia                   從拉取型數據源取數
  crudelab make-sample 輸出.xlsx      生成一個演示用的假數據 Excel
  crudelab data-needs [--output 文件] 生成數據與接口需求清單
  crudelab secret set 名稱            保存一個 API key（輸入不回顯；另有 list / unset）
  crudelab user add 名字 --role admin  建立帳號（另有 list / passwd / role / disable / enable）
  crudelab purge-source demo          刪除某個數據源的全部數據點
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

from . import db
from .settings import get_settings


def _init() -> dict:
    from .config import load_config
    from .master import sync_to_db
    settings = get_settings()
    db.init_engine(settings)
    cfg = load_config(settings.config_dir)
    with db.session_scope() as session:
        return sync_to_db(session, cfg)


def cmd_init(_args) -> int:
    stats = _init()
    print(f"數據庫：{get_settings().safe_database_url}")
    print("已同步：" + "，".join(f"{k}={v}" for k, v in stats.items()))
    return 0


def cmd_check(_args) -> int:
    from .config import ConfigError, load_config
    try:
        cfg = load_config(get_settings().config_dir)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"配置無誤：{len(cfg.series)} 條序列，{len(cfg.parameters)} 個參數，"
          f"{len(cfg.indicators)} 個指標，{len(cfg.refineries)} 個煉廠模型，"
          f"{len(cfg.balances)} 張平衡表，{len(cfg.relations)} 個關係模型，"
          f"{len(cfg.signals)} 個信號，{len(cfg.playbooks)} 個事件預案")
    return 0


def _port_in_use(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def cmd_serve(args) -> int:
    """啟動站點。預設前台和後台一起啟動，各佔一個端口；--site 可以只啟動其中一個（分開部署時用）。"""
    import asyncio
    from dataclasses import replace

    import uvicorn

    from .api.app import create_app
    from .config import ConfigError
    ports = {"front": args.port, "admin": args.admin_port or args.port + 1}
    sites = ["front", "admin"] if args.site == "both" else [args.site]
    for site in sites:
        if _port_in_use(args.host, ports[site]):
            print(f"端口 {ports[site]} 已經被佔用。如果是之前啟動的 crudelab 還開著，直接用瀏覽器打開 "
                  f"http://{args.host}:{ports[site]} 即可；否則用 --port / --admin-port 換一個端口", file=sys.stderr)
            return 1
    base = get_settings()
    settings = replace(base,      # 沒有用環境變量指定對外地址時，按實際端口互相連結
                       front_url=os.environ.get("CRUDELAB_FRONT_URL") or f"http://{args.host}:{ports['front']}",
                       admin_url=os.environ.get("CRUDELAB_ADMIN_URL") or f"http://{args.host}:{ports['admin']}")
    from .api.app import NoAdminAccount
    try:
        servers = [uvicorn.Server(uvicorn.Config(create_app(settings, site), host=args.host, port=ports[site],
                                                 log_level="warning")) for site in sites]
    except (ConfigError, NoAdminAccount) as exc:
        print(exc, file=sys.stderr)
        return 1
    names = {"front": "前台", "admin": "後台"}
    if not settings.auth_enabled and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("警告：登錄功能沒有啟用，而站點正對本機以外開放，任何連得上的人都能查看和修改數據。\n"
              "      對外提供服務前請設 CRUDELAB_AUTH=on。", file=sys.stderr)
    print("crudelab 已啟動（這個窗口要保持開著；按 Ctrl+C 停止）", flush=True)
    for site in sites:
        print(f"  {names[site]}：http://{args.host}:{ports[site]}", flush=True)
    if args.open:
        import threading
        import webbrowser
        threading.Timer(1.0, webbrowser.open, args=(f"http://{args.host}:{ports[sites[0]]}",)).start()

    async def run() -> None:
        await asyncio.gather(*(server.serve() for server in servers))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_import(args) -> int:
    from .imports import commit_batch, preview_file
    _init()
    path = Path(args.file)
    with db.session_scope() as session:
        preview = preview_file(session, get_settings(), path.read_bytes(), path.name, args.source)
        for sheet in preview["sheets"]:
            print(f"[{sheet['name']}] 版式：{sheet['layout']}" + (f"（{sheet['note']}）" if sheet["note"] else ""))
            for col in sheet["columns"]:
                target = col["series_code"] or "未識別，跳過"
                print(f"  {col['ref']:<8} {col['label'][:40]:<40} → {target}  ({col['n_values']} 個值)")
        for warning in preview["warnings"]:
            print("注意：" + warning)
        if args.dry_run:
            print("（--dry-run：只預覽，沒有入庫）")
            session.rollback()
            return 0
        result = commit_batch(session, preview["batch_id"])
    print(f"入庫完成：新增 {result['inserted']}，更新 {result['updated']}，未變 {result['unchanged']}，"
          f"未識別的列 {result['unmapped_columns']} 個（可在後台手動指定）")
    return 0


def cmd_pull(args) -> int:
    from .connectors import ConnectorNotReady, get_connector
    from .ingest import write_observations
    from .models import Series
    _init()
    conn = get_connector(args.source)
    with db.session_scope() as session:
        try:
            observations = conn.fetch(list(session.scalars(select(Series))), None, None)
        except ConnectorNotReady as exc:
            print(f"無法取數：{exc}", file=sys.stderr)
            return 1
        except Exception as exc:        # 不打印異常原文：可能含請求網址（EIA 的 key 在網址裡）
            from .connectors import ConnectorError
            detail = str(exc) if isinstance(exc, ConnectorError) else f"未預期的錯誤（{type(exc).__name__}）"
            print(f"取數失敗：{detail}", file=sys.stderr)
            return 1
        stats = write_observations(session, observations, args.source)
    print(f"{args.source}：新增 {stats['inserted']}，更新 {stats['updated']}，未變 {stats['unchanged']}")
    return 0


def cmd_user(args) -> int:
    """帳號管理。第一個管理員只能從這裡建（那時還沒有人能登錄後台）。"""
    import getpass

    from sqlalchemy import select

    from . import auth
    from .models import UserAccount
    db.init_engine(get_settings())

    def ask_password() -> str:
        if args.password_stdin:                      # 給腳本用：從標準輸入讀一行
            return sys.stdin.readline().rstrip("\n")
        first = getpass.getpass("密碼：")
        if first != getpass.getpass("再輸入一次："):
            raise auth.AuthError("兩次輸入的密碼不一樣")
        return first

    try:
        with db.session_scope() as session:
            if args.action == "list":
                rows = list(session.scalars(select(UserAccount).order_by(UserAccount.id)))
                for u in rows:
                    print(f"{u.id:>3}  {u.username:<24} {auth.ROLE_NAMES[u.role]:<6} {'' if u.active else '已停用'}")
                if not rows:
                    print("還沒有任何帳號。")
                return 0
            if args.action == "add":
                user = auth.create_user(session, args.username, ask_password(), args.role, actor="cli")
                print(f"已建立帳號 {user.username}（{auth.ROLE_NAMES[user.role]}）")
                return 0
            user = auth.find(session, args.username)
            if user is None:
                raise auth.AuthError(f"沒有名為 {args.username} 的帳號")
            if args.action == "passwd":
                auth.set_password(session, user.id, ask_password(), actor="cli")
                print(f"已重設 {user.username} 的密碼，它在各處的登錄都已失效")
            elif args.action == "role":
                auth.update_user(session, user.id, role=args.role, actor="cli")
                print(f"{user.username} 的角色已改為 {auth.ROLE_NAMES[args.role]}")
            else:
                auth.update_user(session, user.id, active=args.action == "enable", actor="cli")
                print(f"已{'啟用' if args.action == 'enable' else '停用'} {user.username}")
    except auth.AuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_data_needs(args) -> int:
    from .config import ConfigError, load_config
    from .data_needs import render
    try:
        text = render(load_config(get_settings().config_dir))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已寫入 {args.output}")
    else:
        print(text)
    return 0


def cmd_secret(args) -> int:
    """密鑰管理。任何時候都不顯示密鑰的內容，只顯示設了沒有、來自哪裡。"""
    import getpass

    from . import credentials
    try:
        if args.action == "list":
            path = credentials.secrets_path()
            print(f"密鑰文件：{path}（{'存在' if path.exists() else '尚未建立'}）")
            for name, description in credentials.KNOWN.items():
                where = credentials.source(name)
                print(f"  {'✓' if where else '·'} {name:<22} {where or '未設定':<6}  {description}")
            return 0
        if args.name not in credentials.KNOWN:
            print(f"不認識的密鑰名 {args.name}。運行 crudelab secret list 查看可用的名字", file=sys.stderr)
            return 1
        if args.action == "set":
            value = (sys.stdin.readline().rstrip("\n") if args.stdin
                     else getpass.getpass(f"{args.name}（輸入不會顯示）："))
            credentials.set_secret(args.name, value)
            print(f"已保存 {args.name} 到 {credentials.secrets_path()}。下次取數或調用時生效，不用重啟。")
            if os.environ.get(args.name):
                print("注意：環境變量裡也設了同名的值，它優先於密鑰文件。")
        else:
            removed = credentials.unset_secret(args.name)
            print(f"已從密鑰文件移除 {args.name}" if removed else f"密鑰文件裡沒有 {args.name}")
    except credentials.CredentialError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_make_sample(args) -> int:
    from .sample import write_sample_workbook
    write_sample_workbook(Path(args.output), days=780)       # 三年日度行情，夠關係模型擬合
    print(f"已生成 {args.output}（全部是隨機生成的假數據，僅供試用；導入時數據源請選 demo）")
    return 0


def cmd_purge(args) -> int:
    from .models import AuditLog, Observation, ObservationRevision
    _init()
    with db.session_scope() as session:
        n = session.scalar(select(func.count()).select_from(Observation).where(
            Observation.source_code == args.source))
        if not args.yes:
            print(f"數據源 {args.source} 共有 {n} 個數據點。確認刪除請加 --yes")
            return 1
        session.execute(delete(Observation).where(Observation.source_code == args.source))
        session.execute(delete(ObservationRevision).where(ObservationRevision.source_code == args.source))
        session.add(AuditLog(actor="local", action="purge", entity="data_source", entity_id=args.source,
                             detail={"deleted": n}))
    print(f"已刪除 {n} 個數據點")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crudelab", description="原油研究平台")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="升級表結構並同步配置").set_defaults(func=cmd_init)
    sub.add_parser("check", help="只校驗配置").set_defaults(func=cmd_check)
    p = sub.add_parser("serve", help="啟動前台和後台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000, help="前台端口")
    p.add_argument("--admin-port", type=int, default=None, help="後台端口，預設是前台端口 + 1")
    p.add_argument("--site", choices=["both", "front", "admin"], default="both", help="啟動哪個站點")
    p.add_argument("--open", action="store_true", help="啟動後自動打開瀏覽器")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("import", help="導入 Excel / CSV")
    p.add_argument("file")
    p.add_argument("--source", default="reuters_xls")
    p.add_argument("--dry-run", action="store_true", help="只預覽，不入庫")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("pull", help="從拉取型數據源取數")
    p.add_argument("source")
    p.set_defaults(func=cmd_pull)
    p = sub.add_parser("make-sample", help="生成演示用假數據 Excel")
    p.add_argument("output")
    p.set_defaults(func=cmd_make_sample)
    p = sub.add_parser("data-needs", help="生成數據與接口需求清單")
    p.add_argument("--output", help="寫入這個文件；不給就打印")
    p.set_defaults(func=cmd_data_needs)
    p = sub.add_parser("secret", help="密鑰管理（API key 等）")
    p.add_argument("action", choices=["list", "set", "unset"])
    p.add_argument("name", nargs="?")
    p.add_argument("--stdin", action="store_true", help="從標準輸入讀（給腳本用）；預設是不回顯的互動輸入")
    p.set_defaults(func=cmd_secret)
    p = sub.add_parser("user", help="帳號管理")
    p.add_argument("action", choices=["list", "add", "passwd", "role", "disable", "enable"])
    p.add_argument("username", nargs="?")
    p.add_argument("--role", choices=["viewer", "analyst", "admin"], default="viewer",
                   help="viewer 只讀 / analyst 研究員 / admin 管理員")
    p.add_argument("--password-stdin", action="store_true", help="從標準輸入讀密碼（給腳本用）")
    p.set_defaults(func=cmd_user)
    p = sub.add_parser("purge-source", help="刪除某個數據源的全部數據點")
    p.add_argument("source")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)
    args = parser.parse_args(argv)
    if args.command == "user" and args.action != "list" and not args.username:
        parser.error("這個操作需要給用戶名")
    if args.command == "secret" and args.action != "list" and not args.name:
        parser.error("這個操作需要給密鑰名")
    from . import credentials
    credentials.install_log_redaction()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
