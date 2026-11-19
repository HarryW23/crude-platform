"""命令行入口。

  crudelab init                       升級表結構並同步 config/ 下的配置
  crudelab check                      只校驗 config/，不碰數據庫
  crudelab serve [--port 8000]        啟動後台
  crudelab import 文件 [--source …]   導入 Excel / CSV（只入庫能自動識別的列）
  crudelab pull eia                   從拉取型數據源取數
  crudelab make-sample 輸出.xlsx      生成一個演示用的假數據 Excel
  crudelab run-job [任務代碼]          立即運行一個定時任務（不給代碼則列出全部）
  crudelab backtest [--mode …]        回測方向信號和推演區間
  crudelab scheduler                  單獨運行調度器（serve 預設已包含）
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

    with_scheduler = not args.no_scheduler and "admin" in sites      # 調度器跟著後台跑；只起前台時不跑
    if with_scheduler:
        print("  定時任務：已啟用（在後台的「定時任務」頁查看）", flush=True)

    async def schedule_loop() -> None:
        from . import jobs, scheduler
        await asyncio.to_thread(jobs.recover_stale)
        state = servers[-1].config.app.state.crudelab                # 後台應用的狀態：配置改了它會自動重新載入
        while True:
            try:
                await asyncio.to_thread(scheduler.tick, state.cfg, settings)
            except Exception:                                        # 調度器不能因為一次意外就停掉
                scheduler.log.exception("調度器這一輪出錯")
            await asyncio.sleep(scheduler.TICK_SECONDS)

    async def run() -> None:
        ticker = asyncio.create_task(schedule_loop()) if with_scheduler else None
        try:
            await asyncio.gather(*(server.serve() for server in servers))
        finally:
            if ticker:
                ticker.cancel()

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


def cmd_backtest(args) -> int:
    from datetime import date as _date

    from . import backtest
    from .config import ConfigError, load_config
    settings = get_settings()
    db.init_engine(settings)
    try:
        cfg = load_config(settings.config_dir)
        options = backtest.Options(args.mode, _date.fromisoformat(args.start) if args.start else None,
                                   _date.fromisoformat(args.end) if args.end else None, args.step)
        _init()
        with db.session_scope() as session:
            run_id = backtest.create(session, options, "cli").id
    except (ConfigError, backtest.BacktestError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"回測 #{run_id} 執行中（{backtest.MODES[args.mode]}）…", flush=True)
    status = backtest.execute(cfg, run_id)
    from .models import BacktestRun
    with db.session_scope() as session:
        row = session.get(BacktestRun, run_id)
        if status != "done":
            print(f"失敗：{row.message}", file=sys.stderr)
            return 1
        r = row.results
    pct = lambda v: "   —  " if v is None else f"{v * 100:5.1f}%"  # noqa: E731
    num = lambda v: "   —  " if v is None else f"{v:+.2f}"  # noqa: E731
    print(f"{r['anchor_name']}，{r['start']} 至 {r['end']}，{r['dates']} 個評估日，每 {r['step']} 個交易日一次")
    codes = [h["code"] for h in r["horizons"]]
    print("\n" + f"{'':<26}" + "".join(f"{h['name_zh']:>18}" for h in r["horizons"]))
    print(f"{'':<26}" + "      命中率    IC" * len(codes))
    rows = [(s["name_zh"] + ("*" if s["approx"] else ""), s["by_horizon"]) for s in r["signals"]]
    rows += [(f"［{m['name_zh']}模組］", m["by_horizon"]) for m in r["modules"]] + [("［綜合］", r["overall"]["by_horizon"])]
    for name, by in rows:
        cells = "".join(f"  {pct(by[c]['hit_rate'])} {num(by[c]['ic'])}{'!' if by[c]['significant'] else ' '}" for c in codes)
        print(f"{name:<24}{cells}")
    for fv in r.get("fair_values", []):
        print(f"\n{fv['name_zh']}" + ("*" if fv["approx"] else "") +
              f"（可給漂移的日子 {fv['coverage']['ok']}，解釋力弱 {fv['coverage']['weak']}，無法計算 {fv['coverage']['unavailable']}）")
        print("  偏離作為信號：" + "　".join(f"{h['name_zh']} 命中 {pct(fv['by_horizon'][h['code']]['hit_rate'])} IC {num(fv['by_horizon'][h['code']]['ic'])}" for h in r["horizons"]))
        print("  漂移減少的誤差：" + "　".join(f"{h['name_zh']} {pct(fv['drift'][h['code']]['improvement'])}{'!' if fv['drift'][h['code']]['significant'] else ''}" for h in r["horizons"]))
    band = r["band"]["by_horizon"]
    print("\n價格區間覆蓋率（理想約 68%）：" + "　".join(f"{h['name_zh']} {pct(band[h['code']]['coverage'])}" for h in r["horizons"]))
    print("\n* 近似：用到修訂後的月度數據。! IC 在有效樣本下顯著。完整結果在前台的「回測」頁。")
    return 0


def cmd_scheduler(_args) -> int:
    """單獨運行調度器，不起網站。服務器上想把它和網站分成兩個進程時用。"""
    import time

    from . import jobs, scheduler
    from .api.deps import AppState
    settings = get_settings()
    db.init_engine(settings)
    credentials_ready()
    state = AppState(settings, "admin")
    jobs.recover_stale()
    print(f"調度器已啟動，每 {scheduler.TICK_SECONDS} 秒檢查一次（按 Ctrl+C 停止）", flush=True)
    try:
        while True:
            try:
                scheduler.tick(state.cfg, settings)
            except Exception:
                scheduler.log.exception("調度器這一輪出錯")
            time.sleep(scheduler.TICK_SECONDS)
    except KeyboardInterrupt:
        return 0


def _backup_file(settings, cfg, given: str) -> Path:
    """命令行裡的備份文件：可以給路徑，也可以只給備份目錄裡的文件名。"""
    from . import backup
    path = Path(given).expanduser()
    if not path.exists() and backup.NAME.match(given):
        path = backup.directory(settings, cfg) / given
    if not path.is_file():
        raise backup.BackupError(f"找不到備份文件 {given}")
    return path


def cmd_jin10(args) -> int:
    """金十數據：probe 把真實接口的樣子列出來（用來核對字段和規則）；usage 看今天的用量。不打印 token。"""
    import json as _json
    from datetime import UTC, datetime

    from . import jin10
    from .config import load_config
    from .connectors.mcp import MCPError
    settings = get_settings()
    db.init_engine(settings)
    cfg = load_config(settings.config_dir)
    if args.action == "backfill":
        try:
            client = jin10.Jin10(cfg)
        except jin10.Jin10NotReady as exc:
            print(exc, file=sys.stderr)
            return 1
        try:
            with db.session_scope() as session:
                result = jin10.backfill_closes(session, cfg, client, args.days)
        except (MCPError, jin10.Jin10LimitReached) as exc:
            print(f"回填失敗：{exc}", file=sys.stderr)
            return 1
        finally:
            client.close()
        print(f"紐約交易日 {result['first']} → {result['last']}，調用 get_kline {result['calls']} 次（口徑：紐約 16:59 的收盤價）")
        for r in result["rows"]:
            print(f"  {r['series']:<16} {r['code']:<8} 取到 {r['closes']}/{r['days']} 天，休市 {r['no_trading']} 天，"
                  f"新增 {r['written']}")
        return 0
    if args.action == "usage":
        tools = ("list_calendar", "list_flash", "search_flash", "get_quote", "get_kline", "quote://codes")
        print(f"今天（北京時間 {jin10.beijing_today()}）各工具的調用次數，上限 {cfg.jin10.max_calls_per_tool_per_day}：")
        for tool in tools:
            print(f"  {tool:<16} {jin10.used_today(tool)}")
        return 0
    try:
        client = jin10.Jin10(cfg)
    except jin10.Jin10NotReady as exc:
        print(exc, file=sys.stderr)
        return 1

    def show(title, fn):
        print(f"\n== {title}")
        try:
            return fn()
        except (MCPError, jin10.Jin10LimitReached) as exc:
            print(f"  ✗ {exc}")
            return None
    try:
        tools = show("連接與工具", lambda: client.client.list_tools())
        if tools is not None:
            print(f"  ✓ 已連接（協議 {client.client.protocol}，服務 {client.client.server_info.get('name', '?')}）")
            print("  工具：" + "、".join(tools))
        codes = show("品種代碼（quote://codes）", lambda: client.resource("quote://codes"))
        if codes is not None:
            flat = _json.dumps(codes, ensure_ascii=False)
            items = jin10.rows_of(codes) or (codes if isinstance(codes, list) else [])
            print(f"  共約 {len(items)} 個；前幾個：{flat[:300]}")
            listed = {str(r.get("code")) for r in items if isinstance(r, dict)}
            wanted = sorted({s.jin10 for s in cfg.series.values() if s.jin10})
            missing = [c for c in wanted if c not in listed]
            print(f"  配置裡登記的 {len(wanted)} 個代碼：{'全部在列表裡' if not missing else '缺 ' + '、'.join(missing)}")
            extra = [f"{r.get('code')}={r.get('name')}" for r in items if isinstance(r, dict)
                     and any(k in str(r.get("name")) + str(r.get("code")) for k in ("债", "債", "利率", "US10", "US02", "Bond"))]
            print(f"  利率 / 債券類：{'、'.join(extra) or '沒有'}")
        quote = show("報價 UKOIL", lambda: client.call("get_quote", code="UKOIL"))
        if quote is not None:
            print("  " + _json.dumps(quote, ensure_ascii=False)[:400])
        def close_check():
            day = jin10.completed_trade_days(datetime.now(UTC).replace(tzinfo=None), 1)[0]
            got = jin10.close_on(client, "UKOIL", day)
            if got is None:
                return f"紐約 {day} 沒有取到收盤價（可能休市）"
            return f"紐約 {day} 16:59 的 K 線：{got[1]:%Y-%m-%d %H:%M}Z，收盤 {got[0]}"
        result = show("收盤價 UKOIL（get_kline，time = 紐約 16:59 的秒）", close_check)
        if result is not None:
            print("  " + result)
        calendar = show("財經日曆", lambda: client.call("list_calendar"))
        if calendar is not None:
            rows = jin10.rows_of(calendar)
            print(f"  {len(rows)} 項；第一項的字段：{sorted(rows[0]) if rows else '—'}")
            for r in rows[:6]:
                print(f"  {r.get('pub_time')}  {r.get('star')}★  {r.get('title')}  "
                      f"前值 {r.get('previous')}  預期 {r.get('consensus')}  實際 {r.get('actual')}")
            matched = [r.get("title") for r in rows if any(rule.match in str(r.get("title")) for rule in cfg.jin10.calendar_rules)]
            print(f"  能對上 jin10.yaml 換算規則的：{matched[:6] or '本周沒有（或標題寫法不同，請把上面的標題發給負責平台的人）'}")
        flash = show("快訊（list_flash）", lambda: client.call("list_flash"))
        if flash is not None:
            rows = jin10.rows_of(flash)
            print(f"  {len(rows)} 條；第一條的字段：{sorted(rows[0]) if rows else '—'}")
            if rows:
                print("  第一條：" + _json.dumps(rows[0], ensure_ascii=False)[:300])
            data = flash.get("data") if isinstance(flash, dict) else None
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            print(f"  data 的字段（翻頁用）：{keys}；外層字段：{sorted(flash) if isinstance(flash, dict) else '—'}")
    finally:
        client.close()
    print("\n把上面的輸出發給負責平台的人即可核對（裡面沒有 token）。")
    return 0


def cmd_lseg_rics(args) -> int:
    from .config import load_config
    from .data_needs import lseg_rics_text
    text = lseg_rics_text(load_config(get_settings().config_dir))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已寫出 {args.output}")
    else:
        print(text, end="")
    return 0


def cmd_backup(args) -> int:
    from . import backup
    from .config import load_config
    settings = get_settings()
    cfg = load_config(settings.config_dir)
    try:
        if args.action == "list":
            rows = backup.list_backups(settings, cfg)
            print(f"備份目錄：{backup.directory(settings, cfg)}")
            for b in rows:
                note = f"  ！{b['error']}" if b["error"] else f"  數據點 {b['observations']}，原始文件 {b['raw_files']} 個"
                print(f"  {b['name']}  {b['size'] / 1e6:8.1f} MB  {b['label_zh']}{note}")
            if not rows:
                print("  還沒有備份")
            return 0
        if args.action == "verify":
            if not args.file:
                print("請給出要校驗的備份文件", file=sys.stderr)
                return 1
            result = backup.verify(_backup_file(settings, cfg, args.file))
            print(f"校驗通過：{result['name']}，{result['files']} 個文件的校驗碼都一致")
            return 0
        if args.action == "prune":
            removed = backup.prune(settings, cfg)
            print(f"清理了 {len(removed)} 份舊的定時備份" + "".join(f"\n  {n}" for n in removed))
            return 0
        _init()
        made = backup.create(settings, cfg, "manual")
        print(f"已備份並校驗：{backup.directory(settings, cfg) / made['name']}（{made['size'] / 1e6:.1f} MB）")
        if backup.same_disk(settings, cfg):
            print("注意：備份和數據在同一塊磁盤上，防不了硬盤損壞。正式使用時把 config/backup.yaml 的 directory "
                  "指到另一塊硬盤、NAS 或雲同步文件夾")
        print("備份裡不含密鑰：換機器恢復後要用 crudelab secret set 重新錄入")
        return 0
    except backup.BackupError as exc:
        print(exc, file=sys.stderr)
        return 1


def cmd_restore(args) -> int:
    from . import backup
    from .config import load_config
    settings = get_settings()
    cfg = load_config(settings.config_dir)
    try:
        path = _backup_file(settings, cfg, args.file)
        manifest = backup.read_manifest(path)
        counts = manifest.get("counts", {})
        print(f"備份：{path.name}\n  做於 {manifest['created_at']}（UTC），程序版本 {manifest.get('version')}，"
              f"數據點 {counts.get('observation')} 個，文件 {len(manifest['files'])} 個")
        print(f"將覆蓋：{settings.safe_database_url} 和 {settings.data_dir} 下的同名文件"
              + ("，以及配置文件" if args.with_config else "（配置文件不動，要一起恢復加 --with-config）"))
        if not args.yes:
            print("恢復前會先自動備份現在的數據。確認恢復請加 --yes", file=sys.stderr)
            return 1
        result = backup.restore(settings, cfg, path, with_config=args.with_config, force=args.force)
    except backup.BackupError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("恢復完成。" + (f"恢復前的數據已備份為 {result['safety_backup']}，需要時可以再恢復回去。"
                        if result["safety_backup"] else ""))
    print("密鑰不在備份裡：如果是在新機器上恢復，用 crudelab secret list 檢查並重新錄入。")
    return 0


def cmd_local(args) -> int:
    """本機設定：存在 ~/.crudelab/local.env，在項目文件夾之外，更新代碼不受影響。"""
    from . import credentials
    from .settings import (
        LOCAL_SETTINGS,
        LocalSettingsError,
        local_settings_path,
        read_local_settings,
        set_local_setting,
    )
    if args.action == "list":
        values = read_local_settings()
        print(f"本機設定文件：{local_settings_path()}（{'存在' if local_settings_path().exists() else '尚未建立'}）")
        for name, description in LOCAL_SETTINGS.items():
            value = values.get(name)
            env = os.environ.get(name)
            shown = f"{value}" + ("（被同名環境變量蓋過）" if env and env != value else "") if value else "—"
            print(f"  {name:<24} {shown:<24} {description}")
        return 0
    if args.name in credentials.KNOWN:
        print(f"{args.name} 是密鑰，請用 crudelab secret set {args.name} 保存", file=sys.stderr)
        return 1
    try:
        if args.action == "set":
            if args.value is None:
                print("請給出要設定的值，例如：crudelab local set CRUDELAB_LLM_PROVIDER deepseek", file=sys.stderr)
                return 1
            set_local_setting(args.name, args.value)
            print(f"已設定 {args.name}={args.value}（{local_settings_path()}）。重新啟動後生效。")
        else:
            set_local_setting(args.name, None)
            print(f"已刪除 {args.name}")
    except LocalSettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_llm(args) -> int:
    """大模型：查看配置和用量、做一次最小的連通測試、解除自動暫停。"""
    import time as _time

    from .config import load_config
    from .llm import LLMError, LLMNotConfigured, get_provider, limits, status
    from .llm.registry import model_lister

    def limits_lister(cfg, provider):
        return model_lister(cfg.llm, provider)
    settings = get_settings()
    db.init_engine(settings)
    cfg = load_config(settings.config_dir)
    st = status(cfg.llm)
    if args.action == "use":
        from .llm.registry import save_choice
        if not args.provider:
            print("請給出供應商，例如：crudelab llm use deepseek --model 型號名", file=sys.stderr)
            return 1
        try:
            with db.session_scope() as session:
                value = save_choice(session, args.provider, args.model or "", args.thinking == "on", "local")
        except LLMNotConfigured as exc:
            print(f"{exc}。可以先用 crudelab llm models 查看能用的模型名", file=sys.stderr)
            return 1
        print(f"已改用 {value['provider']} / {value['model'] or '—'}，思考模式{'開' if value['thinking'] else '關'}"
              "（和後台「大模型」頁是同一個設定，立即生效）。用 crudelab llm test 試一下")
        return 0
    if args.action == "models":
        try:
            names = limits_lister(cfg, args.provider).list_models()
        except LLMError as exc:
            print(f"查模型列表失敗：{exc}", file=sys.stderr)
            return 1
        print("這個密鑰能用的模型：" if names else "供應商沒有返回模型列表")
        for name in names:
            print(f"  {name}")
        return 0
    if args.action == "reset":
        n = limits.reset()
        print(f"已解除 {n} 個自動暫停" if n else "目前沒有自動暫停")
        return 0
    if args.action == "status":
        print(f"供應商：{st['provider_name']}　模型：{st['model'] or '—'}　思考模式：{'開' if st['thinking'] else '關'}"
              f"　（設定來自{st['source']}）\n{st['message']}")
        k = cfg.llm
        print(f"限制：每分鐘 {k.max_requests_per_minute} 次，24 小時 {k.max_requests_per_day} 次 / "
              f"{k.max_tokens_per_day:,} token，同時 {k.max_concurrent} 個，間隔 ≥ {k.min_interval_seconds} 秒")
        with db.session_scope() as session:
            u = limits.usage(session)
            print(f"最近 24 小時：{u['day']} 次（失敗 {u['failed_day']}），"
                  f"token 輸入 {u['prompt_tokens_day']:,} / 輸出 {u['completion_tokens_day']:,}")
            if st["configured"]:
                pause = limits.active_pause(session, st["provider"])
                if pause:
                    print(f"！已自動暫停到 {pause.until:%Y-%m-%d %H:%M}（UTC）：{pause.reason}。解除：crudelab llm reset")
        return 0
    # test：一次最小的請求，確認密鑰、模型名、網絡都通
    try:
        provider = get_provider("test", cfg.llm)
        started = _time.monotonic()
        reply = provider.complete("你是連通測試。只回覆兩個字：正常", "連通測試", max_tokens=50)
    except LLMNotConfigured as exc:
        print(exc, file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"連通測試失敗：{exc}", file=sys.stderr)
        return 1
    tokens = provider.inner.last_usage
    print(f"連通正常：{provider.name} / {provider.model}，用時 {_time.monotonic() - started:.1f} 秒，"
          f"token 輸入 {tokens[0]} / 輸出 {tokens[1]}。回覆：{reply.strip()[:40]}")
    return 0


def cmd_run_job(args) -> int:
    from . import jobs
    from .config import ConfigError, load_config
    settings = get_settings()
    db.init_engine(settings)
    try:
        cfg = load_config(settings.config_dir)
        if args.code is None:
            from . import scheduler
            for job in cfg.schedule.jobs:
                off = "" if job.enabled else "（停用）"
                print(f"  {job.code:<18} {off}{scheduler.describe(job):<16} {job.name_zh}")
            return 0
        _init()
        result = jobs.run_now(cfg, settings, args.code)
    except (ConfigError, jobs.JobError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{result['status']}：{result['message']}")
    return 0 if result["status"] != "failed" else 1


def credentials_ready() -> None:
    from . import credentials
    credentials.install_log_redaction()


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
    from .sample import last_weekday, write_sample_workbook
    write_sample_workbook(Path(args.output), days=780, end=last_weekday())    # 三年日度行情，結束在最近一個工作日
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
    p.add_argument("--no-scheduler", action="store_true", help="不運行定時任務（另外用 crudelab scheduler 單獨跑時用）")
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
    sub.add_parser("scheduler", help="單獨運行定時任務調度器").set_defaults(func=cmd_scheduler)
    p = sub.add_parser("backtest", help="回測方向信號和推演區間")
    p.add_argument("--mode", choices=["observation", "vintage"], default="observation",
                   help="observation 按數據日期（預設）/ vintage 按入庫時間（嚴格）")
    p.add_argument("--start", help="開始日期 YYYY-MM-DD")
    p.add_argument("--end", help="結束日期 YYYY-MM-DD")
    p.add_argument("--step", type=int, help="每隔幾個交易日評估一次")
    p.set_defaults(func=cmd_backtest)
    p = sub.add_parser("run-job", help="立即運行一個定時任務；不給名字則列出全部")
    p.add_argument("code", nargs="?")
    p.set_defaults(func=cmd_run_job)
    p = sub.add_parser("jin10", help="金十數據：probe 檢測接口 / usage 今天的調用次數 / backfill 補報價歷史收盤價")
    p.add_argument("action", choices=["probe", "usage", "backfill"])
    p.add_argument("--days", type=int, default=60, help="backfill 補最近多少個紐約交易日（每品種每天調用一次 get_kline）")
    p.set_defaults(func=cmd_jin10)
    p = sub.add_parser("lseg-rics", help="生成 LSEG 導出腳本用的 RIC 清單（scripts/lseg_rics.txt）")
    p.add_argument("--output", help="寫入這個文件；不給就打印")
    p.set_defaults(func=cmd_lseg_rics)
    p = sub.add_parser("backup", help="備份：不給動作就立即做一份手動備份")
    p.add_argument("action", nargs="?", choices=["create", "list", "verify", "prune"], default="create",
                   help="create 立即備份（預設）/ list 列出 / verify 校驗一份 / prune 按保留策略清理定時備份")
    p.add_argument("file", nargs="?", help="verify 時要校驗的備份（路徑，或備份目錄裡的文件名）")
    p.set_defaults(func=cmd_backup)
    p = sub.add_parser("restore", help="從備份恢復（會先自動備份現在的數據）")
    p.add_argument("file", help="備份文件（路徑，或備份目錄裡的文件名）")
    p.add_argument("--yes", action="store_true", help="確認恢復")
    p.add_argument("--with-config", action="store_true", help="連配置文件一起恢復（預設不動配置文件）")
    p.add_argument("--force", action="store_true", help="跳過「服務還開著」和「恢復前備份失敗」的檢查")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("llm", help="大模型：status 配置和用量 / models 查能用的模型 / use 選擇 / test 連通測試 / reset 解除暫停")
    p.add_argument("action", choices=["status", "test", "reset", "use", "models"])
    p.add_argument("provider", nargs="?", choices=["none", "deepseek", "openai", "anthropic", "compatible"],
                   help="use 時：用哪一家；models 時：查哪一家（不給就查當前選的）")
    p.add_argument("--model", help="use 時：型號名，以 crudelab llm models 列出的為準")
    p.add_argument("--thinking", choices=["on", "off"], default="off", help="use 時：思考模式（只有 DeepSeek 支持）")
    p.set_defaults(func=cmd_llm)
    p = sub.add_parser("local", help="本機設定（不含密鑰）：list / set 名字 值 / unset 名字。存在項目文件夾之外")
    p.add_argument("action", choices=["list", "set", "unset"])
    p.add_argument("name", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_local)
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
    from .settings import apply_local_settings
    apply_local_settings()             # ~/.crudelab/local.env：這台機器自己的設定，git pull 不會動到它
    if args.command == "user" and args.action != "list" and not args.username:
        parser.error("這個操作需要給用戶名")
    if args.command == "local" and args.action != "list" and not args.name:
        parser.error("這個操作需要給設定名")
    if args.command == "secret" and args.action != "list" and not args.name:
        parser.error("這個操作需要給密鑰名")
    from . import credentials
    credentials.install_log_redaction()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
