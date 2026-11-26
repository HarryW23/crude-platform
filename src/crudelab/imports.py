"""文件導入服務：保存原文件 → 預覽 → 確認後入庫。後台和命令行共用。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import normalize_alias
from .connectors.excel import SheetPreview, parse_workbook
from .ingest import Obs, write_observations
from .models import AuditLog, DataSource, ImportBatch, Series, SeriesAlias
from .schema import PRICE_KINDS
from .settings import Settings


class ImportFlowError(ValueError):
    pass


CONTRACTS_HEADER = "日期,序列代碼,RIC,到期日,合約月"      # scripts/lseg_export.py 寫的合約對照文件的表頭


def make_resolver(session: Session):
    """表頭 → 序列。指向已停用序列的別名不算數（否則整個文件會因為一列而入不了庫）。"""
    active = set(session.scalars(select(Series.code).where(Series.active)))
    table = {a.alias_norm: a.series_code for a in session.scalars(select(SeriesAlias)) if a.series_code in active}
    return lambda label: table.get(normalize_alias(label))


def _duplicates(previews: list[SheetPreview], mapping: dict[str, str | None] | None) -> list[str]:
    seen: dict[str, list[str]] = {}
    for sheet in previews:
        for col in sheet.columns:
            code = mapping.get(col.key, col.series_code) if mapping is not None else col.series_code
            if code:
                seen.setdefault(code, []).append(f"{sheet.name} {col.ref}")
    return [f"{code} 被多處對應：{'；'.join(refs)}（日期重疊時以後面的為準）"
            for code, refs in seen.items() if len(refs) > 1]


def screen(session: Session, observations: list[Obs]) -> tuple[list[Obs], list[str]]:
    """去掉文件裡明顯不能用的數據點，回傳（留下的，說明）。預覽和入庫用同一個規則，預覽裡會寫明去掉了什麼：
    - 序列的 data_from 之前的（合約改過規格，見 SeriesDef.data_from）
    - 序列的 bad_days 裡的：核對過是錯的日子
    - 絕對價格正好是 0 的：路透在沒有報價的日子會填 0（例如阿曼期貨 2022 年 6 月連續十幾天是 0）。
      升貼水、官價、比率是 0 是正常的，不去掉；WTI 2020-04-20 的負價也不受影響"""
    codes = {o.series_code for o in observations}
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(codes)))} if codes else {}
    early: dict[str, int] = {}
    zero: dict[str, list[date]] = {}
    bad: dict[str, list[date]] = {}
    kept = []
    for o in observations:
        row = rows.get(o.series_code)
        if row is not None and row.data_from is not None and o.obs_date < row.data_from:
            early[o.series_code] = early.get(o.series_code, 0) + 1
        elif row is not None and row.bad_days and o.obs_date.isoformat() in row.bad_days:
            bad.setdefault(o.series_code, []).append(o.obs_date)
        elif row is not None and row.kind in PRICE_KINDS and o.value == 0:
            zero.setdefault(o.series_code, []).append(o.obs_date)
        else:
            kept.append(o)
    notes = [f"{code}：{rows[code].data_from} 之前的 {n} 個數據點不導入"
             "（這條序列只用這天之後的數據，見配置裡的 data_from）" for code, n in sorted(early.items())]
    notes += [f"{code}：{'、'.join(str(d) for d in sorted(days))} 核對過是錯價，不導入（見配置裡的 bad_days）"
              for code, days in sorted(bad.items())]
    for code, days in sorted(zero.items()):
        shown = "、".join(str(d) for d in sorted(days)[:5]) + (f" 等 {len(days)} 天" if len(days) > 5 else "")
        notes.append(f"{code}（{rows[code].name_zh}）有 {len(days)} 個值正好是 0，不導入：{shown}。"
                     "價格不會是 0，這是數據商沒有報價的日子；那幾天當作沒有數據")
    return kept, notes


LEVEL_KINDS = ("futures", "swap", "spot", "freight", "fx", "stock", "flow", "position")   # 數值本身是量級的類型
SCALE_FACTOR = 20             # 新數據的中位數和庫裡已有數據差這麼多倍以上，多半是單位錯了


CONSISTENCY_LIMIT = 0.10      # 和庫裡同一批日子的數普遍相差超過 10%：多半是篩選條件不一樣，不是修訂
RECENT_PERIODS = 6            # 最近幾期本來就會修訂（Kpler 流量、EIA、月報），不算進一致性核對


def consistency_warnings(session: Session, observations: list[Obs]) -> list[str]:
    """重新導入同一段歷史時，舊的日子應該和庫裡一樣（修訂只動最近幾期）。普遍差了 10% 以上，
    多半是下載時篩選條件不一樣：商品選了全部油品而不是原油、區域不對、單位是千噸不是千桶、浮倉天數門檻改了……
    這種錯單位核對（差 20 倍才提醒）攔不住。比的是平台實際採用的值，最近 RECENT_PERIODS 期不比。"""
    from collections import defaultdict
    from statistics import median

    from .ingest import load_points
    new: dict[str, dict] = defaultdict(dict)
    for o in observations:
        new[o.series_code][o.obs_date] = o.value
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(list(new))))} if new else {}
    out = []
    for code, points in sorted(new.items()):
        row = rows.get(code)
        if row is None or row.kind not in LEVEL_KINDS:
            continue
        effective = load_points(session, [code])
        old = dict(zip(effective["obs_date"], effective["value"], strict=True))
        common = sorted(d for d in points if d in old and old[d] != 0)[:-RECENT_PERIODS]
        if len(common) < 6:
            continue
        gap = median(abs(points[d] / old[d] - 1) for d in common)
        if gap > CONSISTENCY_LIMIT:
            out.append(f"{code}（{row.name_zh}）：和庫裡已有的 {len(common)} 個相同日子比，數字普遍相差 {gap:.0%}"
                       f"（{common[0]} 到 {common[-1]}；最近 {RECENT_PERIODS} 期本來會修訂，沒算）。"
                       "舊的日子不該變這麼多，多半是下載時的篩選條件和上次不一樣（商品、區域、單位、拆分方式）。"
                       "確認之前不要導入")
    return out


def _amount(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:.4g}"


def scale_warnings(session: Session, observations: list[Obs]) -> list[str]:
    """單位核對：新數據和庫裡這條序列已有的數據（其他來源的也算）差了幾十倍以上時提醒。
    典型是單位錯了：Kpler 插件給的是桶而不是千桶、噸而不是千噸，差 1000 倍；美分和美元差 100 倍。
    比的是平台實際採用的值（同一天有幾個來源時取優先級最高的，和查價一致）：低優先級的演示數據等不能當比較基準。
    比的是同一批日子：兩邊都有的日期上逐日相比取中位數；沒有重疊（例如只導新的一段）時，
    比兩邊各自最近 60 個點。不能拿一大段歷史去比最近的水平：運費這種一年裡能漲十幾倍的，會誤報。
    升貼水、官價、比率會在 0 附近正負來回，不查。"""
    from collections import defaultdict
    from statistics import median

    from .ingest import load_points
    new: dict[str, dict] = defaultdict(dict)
    for o in observations:
        new[o.series_code][o.obs_date] = abs(o.value)
    rows = {r.code: r for r in session.scalars(select(Series).where(Series.code.in_(list(new))))} if new else {}
    out = []
    for code, points in sorted(new.items()):
        row = rows.get(code)
        if row is None or row.kind not in LEVEL_KINDS or len(points) < 3:
            continue
        effective = load_points(session, [code])               # 每天一個值：優先級最高的來源
        old = {d: abs(v) for d, v in zip(effective["obs_date"], effective["value"], strict=True)}
        common = [d for d in points if d in old and old[d] > 0 and points[d] > 0]
        if len(common) >= 3:
            ratio = median(points[d] / old[d] for d in common)
            a, b = median(points[d] for d in common), median(old[d] for d in common)
        else:
            recent = [old[d] for d in sorted(old)[-60:]]
            if len(recent) < 3:
                continue
            a, b = median(points[d] for d in sorted(points)[-60:]), median(recent)
            if a <= 0 or b <= 0:
                continue
            ratio = a / b
        if ratio >= SCALE_FACTOR or ratio <= 1 / SCALE_FACTOR:
            times = f"{ratio:,.0f} 倍" if ratio >= 1 else f"1/{1 / ratio:,.0f}"
            out.append(f"{code}（{row.name_zh}）這批數據的中位數是 {_amount(a)}，庫裡已有的是 {_amount(b)}，"
                       f"差了約 {times}：單位多半不對（這條序列的單位是 {row.unit_code}）。確認之前不要導入")
    return out


def _quality_warnings(settings: Settings, observations: list[Obs]) -> list[str]:
    """文件裡的數據質量提醒（不擋導入）：
    - 很長一段相同的值：數據商在沿用舊值（repeats.py，規則見 config/freshness.yaml 的 repeats）
    - 單日錯價：跳上去又回來、同一天其他序列沒一起跳（spikes.py）"""
    if not observations:
        return []
    from . import repeats, spikes
    from .config import load_config
    cfg = load_config(settings.config_dir)
    by_series: dict[str, list[tuple[date, float]]] = {}
    for o in observations:
        by_series.setdefault(o.series_code, []).append((o.obs_date, o.value))
    daily = {c: p for c, p in by_series.items() if c in cfg.series and cfg.series[c].frequency == "daily"}
    kinds = {c: cfg.series[c].kind for c in daily}
    names = {c: cfg.series[c].name_zh for c in daily}
    commodity = {c: cfg.series[c].commodity for c in daily}
    return repeats.import_warnings(cfg, by_series) + spikes.import_warnings(
        kinds, names, daily, commodity, cfg.freshness.spikes.confirmed)


ALL_SCREENED = "文件裡認出的 {n} 個數據點全部被排除（價格為 0、data_from 之前或 bad_days，見提醒），沒有能入庫的數據"


def nothing_importable(preview: dict) -> str | None:
    """整個文件沒有任何能入庫的數據時，回傳原因（給人看）；有就回傳 None。
    這種文件不能算「導入成功」：格式變了、每天都「成功」卻沒有數據，要很久才會被發現。
    認出了數據點、但全部被篩掉的（例如全是 0）也一樣。"""
    usable = [c for sheet in preview["sheets"] for c in sheet["columns"] if c.get("series_code") and c.get("n_values")]
    if usable and preview.get("importable", 1) == 0:
        return ALL_SCREENED.format(n=sum(c["n_values"] for c in usable))
    if usable:
        return None
    reasons = [f"{sheet['name']}：{sheet['note'] or sheet['layout']}" for sheet in preview["sheets"]]
    return "文件裡沒有能自動入庫的數據" + (f"（{'；'.join(reasons)}）" if reasons else "")


def preview_file(session: Session, settings: Settings, content: bytes, filename: str,
                 source_code: str, actor: str = "local", vintage: date | None = None) -> dict:
    """vintage：機構報告的發佈日期。補錄歷史報告（例如 2025 年 3 月那一期 STEO）時填，
    按期保存的序列會記成那一期；不填就是今天。"""
    source = session.get(DataSource, source_code)
    if source is None or source.kind != "excel":
        raise ImportFlowError(f"數據源 {source_code} 不能用於文件導入")
    if content.lstrip(b"\xef\xbb\xbf").startswith(CONTRACTS_HEADER.encode()):
        raise ImportFlowError("這是路透導出時順便寫的合約對照文件（_contracts.csv），不是行情數據，不用導入；"
                              "用 crudelab verify-rolls 文件名 核對換月規則")
    digest = hashlib.sha256(content).hexdigest()
    safe_name = re.sub(r"[^\w.\-]+", "_", Path(filename).name)[-120:] or "upload"
    stored = settings.raw_dir / digest[:2] / f"{digest[:16]}_{safe_name}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    if not stored.exists():
        stored.write_bytes(content)
    try:
        previews, observations = parse_workbook(stored, make_resolver(session), {})   # {}：按自動識別取出數據點
    except ValueError as exc:
        raise ImportFlowError(str(exc)) from exc
    cover_note = None
    if vintage is None and stored.suffix.lower() == ".pdf":          # OPEC 月報：報告期取封面上的發佈日期
        from .connectors.momr import publication_date
        vintage = publication_date(stored)
        if vintage is not None:
            cover_note = f"報告期按月報封面上的發佈日期記為 {vintage}（補錄舊月報也不用另填）"
    if vintage is not None and vintage > date.today():
        raise ImportFlowError(f"報告期 {vintage} 晚於今天：期號是報告的發佈日期")
    batch = ImportBatch(source_code=source_code, filename=filename, sha256=digest,
                        stored_path=str(stored), status="previewed", created_by=actor, vintage=vintage)
    session.add(batch)
    session.flush()
    earlier = session.scalars(select(ImportBatch.id).where(
        ImportBatch.sha256 == digest, ImportBatch.status == "committed")).all()
    warnings = _duplicates(previews, None)
    # OPEC 月報 PDF 的表裡每期都有年度、季度匯總列，跳過是正常的，不提醒
    pdf = stored.suffix.lower() == ".pdf"
    warnings += [f"{p.name}：{p.note}" for p in previews
                 if p.note and "沒有讀入" in p.note and not (pdf and "季度匯總" in p.note)]
    from .connectors.kpler import SHEET as KPLER_SHEET
    kpler_file = any(KPLER_SHEET.match(p.name.strip()) for p in previews)
    if kpler_file != (source_code == "kpler_xls"):
        raise ImportFlowError("這是 Kpler 終端下載的表，數據源要選「Kpler（終端下載）」" if kpler_file else
                              "數據源選了「Kpler（終端下載）」，但這個文件不是 Kpler 終端下載的表（工作表名應該像 "
                              "World monthly exports (Crude-Co…，文件名不要改）")
    observations, screened = screen(session, observations)
    hold = scale_warnings(session, observations) + consistency_warnings(session, observations)
    warnings += screened + hold + _quality_warnings(settings, observations)
    if cover_note:
        warnings.append(cover_note)
    if earlier:
        warnings.append(f"同一個文件之前已導入過（批次 {', '.join(map(str, earlier))}），重複導入不會產生重複數據")
    if vintage is not None:
        vintaged = {c for c, in session.execute(select(Series.code).where(Series.vintaged))}
        hit = {col.series_code for p in previews for col in p.columns if col.series_code}
        if hit and not hit & vintaged:
            warnings.append(f"填了報告期 {vintage}，但這個文件裡認出的序列都不是按期保存的，報告期不會起作用")
    return {"batch_id": batch.id, "filename": filename, "source_code": source_code,
            "vintage": vintage.isoformat() if vintage else None, "importable": len(observations),
            "hold": hold,           # 疑似條件或單位錯了：收件夾自動導入時不收，人看過預覽才能確認
            "sheets": [p.to_dict() for p in previews], "warnings": warnings}


def commit_batch(session: Session, batch_id: int, mapping: dict[str, str | None] | None = None,
                 learn: bool = True, actor: str = "local") -> dict:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise ImportFlowError(f"找不到導入批次 {batch_id}")
    if batch.status == "committed":
        raise ImportFlowError(f"批次 {batch_id} 已經入庫過")
    mapping = dict(mapping or {})
    known = set(session.scalars(select(Series.code).where(Series.active)))
    bad = sorted({c for c in mapping.values() if c and c not in known})
    if bad:
        raise ImportFlowError(f"未知的序列代碼：{', '.join(bad)}")

    previews, observations = parse_workbook(Path(batch.stored_path), make_resolver(session), mapping)
    parsed = len(observations)
    observations, _ = screen(session, observations)
    if parsed and not observations:
        raise ImportFlowError(ALL_SCREENED.format(n=parsed))
    if batch.vintage is not None:
        observations = [replace(o, vintage=batch.vintage) for o in observations]
    from .ingest import utcnow
    written = utcnow()
    undo: dict = {"written_at": written.isoformat()}       # 撤銷時找「這一批覆蓋掉的舊值」：修訂記錄裡同一時刻被取代的
    stats = write_observations(session, observations, batch.source_code, batch_id=batch.id, actor=actor,
                               now=written, undo=undo)
    batch.undo = undo

    learned = 0
    if learn:  # 把人工指定的對應記成別名，下次同樣表頭自動識別
        existing = {a.alias_norm: a for a in session.scalars(select(SeriesAlias))}
        for sheet in previews:
            for col in sheet.columns:
                target = mapping.get(col.key)
                norm = normalize_alias(col.label)
                if not target or target == col.series_code or not col.candidates or not norm or len(norm) > 128:
                    continue                                 # 太長的表頭不學（存不下），下次照樣手動指定
                if norm in existing:
                    existing[norm].series_code, existing[norm].origin = target, "learned"
                else:
                    alias = SeriesAlias(alias_norm=norm, alias=col.label[:128], series_code=target, origin="learned")
                    session.add(alias)
                    existing[norm] = alias
                learned += 1

    final = {}
    unmapped = []
    for sheet in previews:
        for col in sheet.columns:
            code = mapping.get(col.key, col.series_code)
            final[col.key] = code
            if not code:
                unmapped.append(f"{sheet.name} {col.ref}：{col.label}")
    batch.status, batch.mapping = "committed", final
    batch.stats = {**stats, "skipped": parsed - len(observations), "learned_aliases": learned,
                   "unmapped_columns": len(unmapped)}
    session.add(AuditLog(actor=actor, action="import", entity="import_batch", entity_id=str(batch.id),
                         detail={"filename": batch.filename, **batch.stats}))
    return {"batch_id": batch.id, **batch.stats, "unmapped": unmapped,
            "warnings": _duplicates(previews, mapping)}


# ------------------------------ 撤銷導入 ------------------------------

REVERT_ACTOR = "revert:{id}"


def _later_changes(session: Session, batch: ImportBatch) -> list[str]:
    """之後有沒有別的批次（或定時取數）改過這一批寫的數據點。有的話不能直接撤銷：
    要先撤銷之後的那一批（後進先出），否則不知道該恢復成哪個數。"""
    from .models import Observation, ObservationRevision
    replaced = session.execute(select(ObservationRevision.series_code, ObservationRevision.obs_date,
                                      ObservationRevision.source_code, ObservationRevision.replaced_by).where(
        ObservationRevision.batch_id == batch.id)).all()
    blockers: dict[str, str] = {}
    for code, day, source, by in replaced:
        if by == "config" or by.startswith("revert:"):         # 按配置清理掉的、之前撤銷時記的：不算之後的改動
            continue
        row = session.scalars(select(Observation).where(
            Observation.series_code == code, Observation.obs_date == day, Observation.source_code == source)).first()
        if row is not None and row.batch_id == batch.id:        # 改過、但那一批已經撤銷，又回到這一批的數
            continue
        if row is None or row.batch_id is None:
            label = "定時取數，或之後被刪除"
        else:
            later = session.get(ImportBatch, row.batch_id)
            label = f"#{row.batch_id}（{later.filename}）" if later else f"#{row.batch_id}"
        blockers.setdefault(label, f"{code} {day}")
    mine = set((batch.undo or {}).get("vintage_added", [])) | {m["id"] for m in (batch.undo or {}).get(
        "vintage_modified", [])}
    if mine:
        for later in session.scalars(select(ImportBatch).where(ImportBatch.id > batch.id,
                                                               ImportBatch.status == "committed")):
            touched = {m["id"] for m in (later.undo or {}).get("vintage_modified", [])}
            if touched & mine:
                blockers.setdefault(f"#{later.id}（{later.filename}）", "按期保存的數據")
    return [f"{who}（例如 {what}）" for who, what in blockers.items()]


def revert_batch(session: Session, batch_id: int, actor: str = "local", dry_run: bool = False) -> dict:
    """撤銷一次導入：這一批新加的數據點刪掉，它覆蓋掉的舊值恢復回來，按期保存的記錄也還原。

    全程留記錄、可以追溯：被撤銷的值記進修訂記錄（replaced_by = revert:批次號），按時點（as_of）查詢
    撤銷之前的那段時間，仍然看得到當時用的數；操作日誌記一條 revert_import；批次標成 reverted，原文件保留。
    之後有別的批次改過同樣的數據點時拒絕（要先撤銷之後的那一批），不猜該恢復成哪個數。
    dry_run：只回傳會改動多少，不改。"""
    from .ingest import utcnow
    from .models import Observation, ObservationRevision, ObservationVintage
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise ImportFlowError(f"找不到導入批次 {batch_id}")
    if batch.status == "reverted":
        raise ImportFlowError(f"批次 {batch_id} 已經在 {batch.reverted_at:%Y-%m-%d %H:%M} 撤銷過")
    if batch.status != "committed":
        raise ImportFlowError(f"批次 {batch_id} 還沒有入庫（只預覽過），不用撤銷")
    vintage_rows = session.scalar(select(func.count()).select_from(ObservationVintage).where(
        ObservationVintage.batch_id == batch_id))
    if batch.undo is None and vintage_rows:
        raise ImportFlowError(f"批次 {batch_id} 是在有撤銷功能之前導入的，按期保存的數據（{vintage_rows} 行）"
                              "沒有撤銷記錄，沒辦法準確還原；請用正確的文件重新導入來改正")
    blockers = _later_changes(session, batch)
    if blockers:
        raise ImportFlowError(f"批次 {batch_id} 寫的數據之後又被改過：{'；'.join(blockers[:3])}"
                              f"{' 等' if len(blockers) > 3 else ''}。要先撤銷之後的那一批（後進先出），"
                              "否則不知道該恢復成哪個數")
    current = list(session.scalars(select(Observation).where(Observation.batch_id == batch_id)))
    undo = batch.undo or {}
    added_ids = undo.get("vintage_added", [])
    modified = undo.get("vintage_modified", [])
    counts = {"restored": 0, "removed": 0, "vintage_removed": 0, "vintage_restored": len(modified)}
    previous: dict[int, ObservationRevision | None] = {}
    written = datetime.fromisoformat(undo["written_at"]) if undo.get("written_at") else None
    for obs in current:
        # 這一批寫入時覆蓋掉的舊值：修訂記錄裡在這一批寫入的那一刻被取代的那條（不是撤銷時記的）。
        # 寫入時刻用批次記下的；之前的版本沒記，用這個點的更新時間（沒被撤銷恢復過時就是寫入時刻）
        previous[obs.id] = session.scalars(select(ObservationRevision).where(
            ObservationRevision.series_code == obs.series_code, ObservationRevision.obs_date == obs.obs_date,
            ObservationRevision.source_code == obs.source_code,
            ObservationRevision.replaced_at == (written or obs.updated_at),
            ObservationRevision.replaced_by.not_like("revert:%"))).first()
        counts["restored" if previous[obs.id] is not None else "removed"] += 1
    rows = list(session.scalars(select(ObservationVintage).where(ObservationVintage.id.in_(added_ids)))) \
        if added_ids else []
    counts["vintage_removed"] = len(rows)
    plan: dict = {**counts, "series": sorted({o.series_code for o in current})}
    if dry_run:
        return {"batch_id": batch_id, "filename": batch.filename, "dry_run": True, **plan}

    now = utcnow()
    who = REVERT_ACTOR.format(id=batch_id)
    for obs in current:
        session.add(ObservationRevision(
            series_code=obs.series_code, obs_date=obs.obs_date, source_code=obs.source_code, value=obs.value,
            batch_id=obs.batch_id, source_ref=obs.source_ref, recorded_at=obs.updated_at,
            replaced_at=now, replaced_by=who))
        old = previous[obs.id]
        if old is None:                                      # 這一批新加的點
            session.delete(obs)
        else:                                                # 這一批覆蓋掉的舊值
            obs.value, obs.batch_id, obs.source_ref, obs.updated_at = old.value, old.batch_id, old.source_ref, now
    for row in rows:
        session.delete(row)
    for m in modified:
        kept = session.get(ObservationVintage, m["id"])
        if kept is not None:
            kept.value, kept.batch_id, kept.source_ref = m["value"], m["batch_id"], m["source_ref"]
            kept.confirmed_at = date.fromisoformat(m["confirmed_at"]) if m["confirmed_at"] else None
    batch.status, batch.reverted_at, batch.reverted_by = "reverted", now, actor
    batch.stats = {**(batch.stats or {}), "reverted": counts}
    session.add(AuditLog(actor=actor, action="revert_import", entity="import_batch", entity_id=str(batch_id),
                         detail={"filename": batch.filename, **plan}))
    session.flush()
    return {"batch_id": batch_id, "filename": batch.filename, "dry_run": False, **plan}
