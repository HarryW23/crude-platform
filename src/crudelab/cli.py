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
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

from . import db
from .settings import get_settings


def _init() -> dict:
    from .api.app import load_and_sync
    settings = get_settings()
    db.init_engine(settings)
    return load_and_sync(settings)[1]


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
          f"{len(cfg.indicators)} 個指標，{len(cfg.refineries)} 個煉廠模型")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api.app import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port)
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
    write_sample_workbook(Path(args.output))
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
    p = sub.add_parser("serve", help="啟動後台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
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
