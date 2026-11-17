"""命令行入口。

  crudelab init                       升級表結構並同步 config/ 下的配置
  crudelab check                      只校驗 config/，不碰數據庫
  crudelab serve [--port 8000]        啟動後台
  crudelab import 文件 [--source …]   導入 Excel / CSV（只入庫能自動識別的列）
  crudelab pull eia                   從拉取型數據源取數
  crudelab make-sample 輸出.xlsx      生成一個演示用的假數據 Excel
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
    print(f"數據庫：{get_settings().database_url}")
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
    try:
        servers = [uvicorn.Server(uvicorn.Config(create_app(settings, site), host=args.host, port=ports[site],
                                                 log_level="warning")) for site in sites]
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    names = {"front": "前台", "admin": "後台"}
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
        stats = write_observations(session, observations, args.source)
    print(f"{args.source}：新增 {stats['inserted']}，更新 {stats['updated']}，未變 {stats['unchanged']}")
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
    p = sub.add_parser("purge-source", help="刪除某個數據源的全部數據點")
    p.add_argument("source")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
