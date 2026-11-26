"""文件導入服務：保存原文件 → 預覽 → 確認後入庫。後台和命令行共用。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import normalize_alias
from .connectors.excel import SheetPreview, parse_workbook
from .import_checks import consistency_warnings, quality_warnings, scale_warnings, screen
from .ingest import write_observations
from .models import AuditLog, DataSource, ImportBatch, Series, SeriesAlias
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
    warnings += screened + hold + quality_warnings(settings, observations)
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

