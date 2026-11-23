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
  crudelab user add 名字 --role admin  建立帳號（另有 list / passwd / role / disable / enable / unlock）
  crudelab data-model --output docs/data-model.md   重新生成數據字典
  crudelab watchdog 平台地址           從另一台機器檢查平台是否在運行，掛了發群通知（見 watchdog.py）
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
    from datetime import date as _date

    from .config import calendar_warnings
    for line in calendar_warnings(cfg, _date.today()):
        print("⚠ " + line)                      # 提醒，不算失敗：沒登記的年份退回只按周一到周五算
    from . import zh
    rules = cfg.jin10.calendar_rules
    issues = zh.traditional_issues({"config/jin10.yaml 快訊關鍵詞 flash_keywords": cfg.jin10.flash_keywords,
                                    "config/jin10.yaml 日曆規則 match": [r.match for r in rules],
                                    "config/jin10.yaml 日曆規則 require": [r.require for r in rules if r.require]},
                                   cfg.jin10.term_overrides)
    if issues:
        print("\n金十的內容是簡體，下面這些寫成了繁體，會匹配不到（把右邊的寫法複製過去即可）：", file=sys.stderr)
        for line in issues:
            print("  " + line, file=sys.stderr)
        return 1
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
    from .api.deps import Site
    from .config import ConfigError
    ports = {"front": args.port, "admin": args.admin_port or args.port + 1}
    sites: list[Site] = ["front", "admin"] if args.site == "both" else [args.site]
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
        apps = {site: create_app(settings, site) for site in sites}
        servers = [uvicorn.Server(uvicorn.Config(apps[site], host=args.host, port=ports[site], log_level="warning"))
                   for site in sites]
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
        state = apps["admin"].state.crudelab                         # 後台應用的狀態：配置改了它會自動重新載入
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
    from datetime import date

    from .imports import commit_batch, nothing_importable, preview_file
    _init()
    path = Path(args.file)
    with db.session_scope() as session:
        vintage = date.fromisoformat(args.vintage) if args.vintage else None
        preview = preview_file(session, get_settings(), path.read_bytes(), path.name, args.source, vintage=vintage)
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
        if (reason := nothing_importable(preview)) is not None:
            print(f"沒有入庫：{reason}", file=sys.stderr)
            session.rollback()
            return 1
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


def cmd_watchdog(args) -> int:
    """看門狗：退出碼 0 = 正常，1 = 平台有問題，2 = 有問題且告警沒送達任何渠道。"""
    import time as _time
    from datetime import datetime as _dt

    from . import watchdog
    from .config import ConfigError, load_config
    try:
        cfg = load_config(get_settings().config_dir)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    unknown = [c for c in args.channel or [] if c not in {ch.code for ch in cfg.notifications.channels}]
    if unknown:
        print(f"notifications.yaml 裡沒有這些渠道：{'、'.join(unknown)}", file=sys.stderr)
        return 1
    while True:
        r = watchdog.run_once(cfg, args.url, require_scheduler=args.require_scheduler, failures=args.failures,
                              channels=args.channel or None, notify=not args.no_notify)
        stamp = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {'正常' if r['ok'] else '有問題：' + r['reason']}"
              + (f"；已發「{r['title']}」：{r['results'] or '沒有可用的渠道'}" if r["event"] else ""), flush=True)
        code = 0 if r["ok"] else (2 if r["event"] and not r["delivered"] else 1)
        if not args.loop:
            return code
        _time.sleep(args.loop)


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
                    lock = "暫時鎖定" if auth.locked_until(session, u.username) else ""
                    print(f"{u.id:>3}  {u.username:<24} {auth.ROLE_NAMES[u.role]:<6} {'' if u.active else '已停用'} {lock}")
                if not rows:
                    print("還沒有任何帳號。")
                return 0
            if args.action == "add":
                user = auth.create_user(session, args.username, ask_password(), args.role, actor="cli")
                print(f"已建立帳號 {user.username}（{auth.ROLE_NAMES[user.role]}）")
                return 0
            found = auth.find(session, args.username)
            if found is None:
                raise auth.AuthError(f"沒有名為 {args.username} 的帳號")
            user = found
            if args.action == "passwd":
                auth.set_password(session, user.id, ask_password(), actor="cli")
                print(f"已重設 {user.username} 的密碼，它在各處的登錄都已失效")
            elif args.action == "unlock":
                print(f"已解鎖 {user.username}" if auth.unlock(session, user.username, actor="cli")
                      else f"{user.username} 沒有被鎖定")
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
        row = db.require(session, BacktestRun, run_id)
        if status != "done":
            print(f"失敗：{row.message}", file=sys.stderr)
            return 1
        r: dict = row.results or {}
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
        if not 1 <= args.days <= 2000:
            print("--days 要在 1 到 2000 之間", file=sys.stderr)
            return 1
        from .master import sync_to_db
        with db.session_scope() as session:              # 先把配置裡的序列同步進數據庫（剛更新、還沒啟動過也能用）
            sync_to_db(session, cfg)
            if not jin10.quote_series(session):
                print("配置裡沒有登記 jin10 代碼的序列（config/master/series/macro.yaml），沒有可以補的。", file=sys.stderr)
                return 1
        try:
            client = jin10.Jin10(cfg)
        except jin10.Jin10NotReady as exc:
            print(exc, file=sys.stderr)
            return 1
        import time as _time
        started = _time.monotonic()
        interactive = sys.stdout.isatty()

        def progress(event, info):
            if event == "plan":
                print(f"紐約交易日 {info['first']} → {info['last']}：{info['series']} 個品種 × {info['days']} 天 = "
                      f"最多調用 get_kline {info['calls']} 次（今天還剩 {info['left']} 次）。")
                print(f"從最近一天往前補，連續 {jin10.STOP_AFTER_MISSES} 個交易日取不到就停（超出金十保存的範圍）。"
                      "每次調用約 1–2 秒；每補完一個品種寫入並打印一行，按 Ctrl+C 中斷不會丟失已完成的。")
            elif event == "day" and interactive:
                print(f"\r  正在補 {info['series']}（第 {info['n']}/{info['of']} 個品種）："
                      f"{info['i']}/{info['days']} 天，已用 {_time.monotonic() - started:.0f} 秒", end="", flush=True)
            elif event == "series":
                r = info
                print(("\r\033[K" if interactive else "") +
                      f"  ✓ {r['series']:<16} {r['code']:<8} 取到 {r['closes']}/{r['days']} 天"
                      f"（最早 {r['earliest'] or '—'}），沒取到 {r['no_trading']} 天，"
                      f"新增 {r['written']}，更正 {r['corrected']}"
                      + (f"；{r['stopped_at']} 之前金十沒有數據，停止" if r["stopped_at"] else ""), flush=True)
        try:
            with db.session_scope() as session:
                jin10.backfill_closes(session, cfg, client, args.days, progress=progress)
        except KeyboardInterrupt:
            print("\n已中斷：前面打 ✓ 的品種已經寫入；再運行一次會跳過已有的值（但仍會調用金十）。", file=sys.stderr)
            return 130
        except (MCPError, jin10.Jin10LimitReached) as exc:
            print(f"\n回填失敗：{exc}（前面打 ✓ 的品種已經寫入）", file=sys.stderr)
            return 1
        finally:
            client.close()
        print(f"完成，用時 {_time.monotonic() - started:.0f} 秒。")
        print("金十只保存最近一兩天的分鐘線：更早的歷史從金十補不了，要從其他來源導入（見 README「金十數據」）。")
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

        def depth_check():
            # 金十的分鐘線保存多久：往前試幾個交易日（共 5 次調用）
            days = jin10.completed_trade_days(datetime.now(UTC).replace(tzinfo=None), 10)[::-1]
            marks = []
            for back in (1, 2, 3, 5, 10):
                day = days[back - 1]
                marks.append(f"{back} 天前（{day}）{'有' if jin10.close_on(client, 'UKOIL', day) else '沒有'}")
            return "、".join(marks)
        result = show("分鐘線保存深度 UKOIL（往前試 1、2、3、5、10 個交易日）", depth_check)
        if result is not None:
            print("  " + result)
        calendar = show("財經日曆", lambda: client.call("list_calendar"))
        if calendar is not None:
            rows = jin10.rows_of(calendar)
            print(f"  {len(rows)} 項；第一項的字段：{sorted(rows[0]) if rows else '—'}")
            for r in rows[:6]:
                print(f"  {r.get('pub_time')}  {r.get('star')}★  {r.get('title')}  "
                      f"前值 {r.get('previous')}  預期 {r.get('consensus')}  實際 {r.get('actual')}")
            calendar_matched = [r.get("title") for r in rows if any(rule.match in str(r.get("title")) for rule in cfg.jin10.calendar_rules)]
            print(f"  能對上 jin10.yaml 換算規則的：{calendar_matched[:6] or '本周沒有（或標題寫法不同，請把上面的標題發給負責平台的人）'}")
        flash = show("快訊（list_flash）", lambda: client.call("list_flash"))
        if flash is not None:
            rows = jin10.rows_of(flash)
            print(f"  {len(rows)} 條；第一條的字段：{sorted(rows[0]) if rows else '—'}")
            if rows:
                print("  第一條：" + _json.dumps(rows[0], ensure_ascii=False)[:300])
            data = flash.get("data") if isinstance(flash, dict) else None
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            print(f"  data 的字段（翻頁用）：{keys}；外層字段：{sorted(flash) if isinstance(flash, dict) else '—'}")
            # 用平台的關鍵詞篩一遍這頁快訊（不再調用金十）
            hits = [(jin10.flash_hits(cfg, jin10.flash_text(r)), jin10.flash_text(r)) for r in rows]
            matched = [hit for hit in hits if hit[0]]
            print(f"  按 config/jin10.yaml 的關鍵詞篩選：{len(rows)} 條裡命中 {len(matched)} 條")
            for keywords, text in matched[:8]:
                print(f"    [{'、'.join(keywords)}] {text[:60]}")
            counts: dict[str, int] = {}
            for keywords, _ in matched:
                for k in keywords:
                    counts[k] = counts.get(k, 0) + 1
            if counts:
                print("  各關鍵詞命中次數：" + "、".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda x: -x[1])))

        # 簡繁對比：同一個詞用簡體和繁體各搜一次，看金十的搜索認不認繁體（共 2 次調用）
        from . import zh
        word_tw = "歐佩克"
        word_cn = zh.to_simplified(word_tw, cfg.jin10.term_overrides)
        for word, label in ((word_cn, "簡體"), (word_tw, "繁體")):
            found = show(f"search_flash（{label}：{word}）", lambda w=word: client.call("search_flash", keyword=w))
            if found is not None:
                rows = jin10.rows_of(found)
                print(f"  {len(rows)} 條" + (f"；第一條：{jin10.flash_text(rows[0])[:60]}" if rows else ""))
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


def cmd_verify_rolls(args) -> int:
    """用路透導出的合約對照文件核對期貨曲線的換月規則。有出入時返回 1。"""
    from . import rollcheck
    from .config import load_config
    try:
        data = rollcheck.read_contracts(Path(args.file))
    except (OSError, ValueError) as exc:
        print(f"讀不了 {args.file}：{exc}", file=sys.stderr)
        return 1
    checks = rollcheck.verify(load_config(get_settings().config_dir), data)
    print("\n".join(rollcheck.report(checks)))
    return 0 if all(c.consistent for c in checks) else 1


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
    if credentials.is_known(args.name):
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


def cmd_data_model(args) -> int:
    """數據字典：從表定義生成。"""
    from .data_model import render
    text = render()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已寫入 {args.output}")
    else:
        print(text)
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
            for name in credentials.names():
                where = credentials.source(name)
                print(f"  {'✓' if where else '·'} {name:<22} {where or '未設定':<6}  {credentials.describe(name)}")
            print("通知類的密鑰可以加後綴存好幾個群，例如 NOTIFY_WECOM_WEBHOOK_TRADING（見 config/notifications.yaml）")
            return 0
        if not credentials.is_known(args.name):
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
    """刪除一個數據源的全部數據：主表、修訂歷史、按期保存的各期記錄，在同一個事務裡一起刪，記進操作日誌。
    只刪主表會留下各期記錄，按報告期還原（回測）時還會讀到已刪的數據。"""
    from .models import AuditLog, Observation, ObservationRevision, ObservationVintage
    _init()
    columns = {"deleted": Observation.source_code, "deleted_revisions": ObservationRevision.source_code,
               "deleted_issues": ObservationVintage.source_code}            # 三張表，按數據源刪
    with db.session_scope() as session:
        counts = {name: session.scalar(select(func.count()).where(column == args.source)) or 0
                  for name, column in columns.items()}
        if not args.yes:
            print(f"數據源 {args.source} 共有 {counts['deleted']} 個數據點、{counts['deleted_issues']} 條按期記錄、"
                  f"{counts['deleted_revisions']} 條修訂歷史。確認刪除請加 --yes")
            return 1
        for column in columns.values():
            session.execute(delete(column.class_).where(column == args.source))
        session.add(AuditLog(actor="local", action="purge", entity="data_source", entity_id=args.source,
                             detail=counts))
    print(f"已刪除 {counts['deleted']} 個數據點、{counts['deleted_issues']} 條按期記錄、"
          f"{counts['deleted_revisions']} 條修訂歷史")
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
    p.add_argument("--vintage", default=None, help="機構報告的發佈日期（補錄歷史報告時填，例如 2025-03-11）")
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
    p = sub.add_parser("verify-rolls", help="用路透導出的合約對照（reuters_日期_contracts.csv）核對期貨換月規則")
    p.add_argument("file", help="scripts/lseg_export.py export 順便寫出的 _contracts.csv")
    p.set_defaults(func=cmd_verify_rolls)
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
    p = sub.add_parser("data-model", help="生成數據字典（每張表、每一列的含義和約束）")
    p.add_argument("--output", help="寫入這個文件；不給就打印")
    p.set_defaults(func=cmd_data_model)
    p = sub.add_parser("secret", help="密鑰管理（API key 等）")
    p.add_argument("action", choices=["list", "set", "unset"])
    p.add_argument("name", nargs="?")
    p.add_argument("--stdin", action="store_true", help="從標準輸入讀（給腳本用）；預設是不回顯的互動輸入")
    p.set_defaults(func=cmd_secret)
    p = sub.add_parser("watchdog", help="從另一台機器檢查平台是否在運行，掛了發群通知")
    p.add_argument("url", help="平台地址，例如 http://192.168.1.20:8001（後台地址配 --require-scheduler）")
    p.add_argument("--require-scheduler", action="store_true", help="調度器沒有心跳也算有問題（檢查後台時用）")
    p.add_argument("--failures", type=int, default=2, help="連續失敗幾次才告警（預設 2，避免重啟時誤報）")
    p.add_argument("--channel", action="append", help="只發到這個渠道（可重複）；不給就發所有 default 渠道")
    p.add_argument("--loop", type=int, metavar="秒", help="常駐：每隔這麼多秒檢查一次；不給就檢查一次後退出")
    p.add_argument("--no-notify", action="store_true", help="只檢查不發通知（試用時用）")
    p.set_defaults(func=cmd_watchdog)
    p = sub.add_parser("user", help="帳號管理")
    p.add_argument("action", choices=["list", "add", "passwd", "role", "disable", "enable", "unlock"])
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
