"""文件導入服務：保存原文件 → 預覽 → 確認後入庫。後台和命令行共用。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import normalize_alias
from .connectors.excel import SheetPreview, parse_workbook
from .ingest import write_observations
from .models import AuditLog, DataSource, ImportBatch, Series, SeriesAlias
from .settings import Settings


class ImportFlowError(ValueError):
    pass


def make_resolver(session: Session):
    table = {a.alias_norm: a.series_code for a in session.scalars(select(SeriesAlias))}
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


def preview_file(session: Session, settings: Settings, content: bytes, filename: str,
                 source_code: str, actor: str = "local") -> dict:
    source = session.get(DataSource, source_code)
    if source is None or source.kind != "excel":
        raise ImportFlowError(f"數據源 {source_code} 不能用於文件導入")
    digest = hashlib.sha256(content).hexdigest()
    safe_name = re.sub(r"[^\w.\-]+", "_", Path(filename).name)[-120:] or "upload"
    stored = settings.raw_dir / digest[:2] / f"{digest[:16]}_{safe_name}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    if not stored.exists():
        stored.write_bytes(content)
    try:
        previews, _ = parse_workbook(stored, make_resolver(session))
    except ValueError as exc:
        raise ImportFlowError(str(exc)) from exc
    batch = ImportBatch(source_code=source_code, filename=filename, sha256=digest,
                        stored_path=str(stored), status="previewed", created_by=actor)
    session.add(batch)
    session.flush()
    earlier = session.scalars(select(ImportBatch.id).where(
        ImportBatch.sha256 == digest, ImportBatch.status == "committed")).all()
    warnings = _duplicates(previews, None)
    warnings += [f"{p.name}：{p.note}" for p in previews if p.note and "沒有讀入" in p.note]
    if earlier:
        warnings.append(f"同一個文件之前已導入過（批次 {', '.join(map(str, earlier))}），重複導入不會產生重複數據")
    return {"batch_id": batch.id, "filename": filename, "source_code": source_code,
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
    stats = write_observations(session, observations, batch.source_code, batch_id=batch.id, actor=actor)

    learned = 0
    if learn:  # 把人工指定的對應記成別名，下次同樣表頭自動識別
        existing = {a.alias_norm: a for a in session.scalars(select(SeriesAlias))}
        for sheet in previews:
            for col in sheet.columns:
                target = mapping.get(col.key)
                norm = normalize_alias(col.label)
                if not target or target == col.series_code or not col.candidates or not norm:
                    continue
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
    batch.stats = {**stats, "learned_aliases": learned, "unmapped_columns": len(unmapped)}
    session.add(AuditLog(actor=actor, action="import", entity="import_batch", entity_id=str(batch.id),
                         detail={"filename": batch.filename, **batch.stats}))
    return {"batch_id": batch.id, **batch.stats, "unmapped": unmapped,
            "warnings": _duplicates(previews, mapping)}
