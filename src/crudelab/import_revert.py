"""撤銷導入：一批導入整批撤回（見 revert_batch）。後台導入記錄的「撤銷」按鈕和 crudelab revert-import 用。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from .imports import ImportFlowError
from .models import AuditLog, ImportBatch

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
    # 這一批寫入時覆蓋掉的舊值：修訂記錄裡在這一批寫入的那一刻被取代的那條（不是撤銷時記的）。
    # 寫入時刻用批次記下的；之前的版本沒記，用各點的更新時間（沒被撤銷恢復過時就是寫入時刻）。一次查完
    written = datetime.fromisoformat(undo["written_at"]) if undo.get("written_at") else None
    moments = {written} if written else {o.updated_at for o in current}
    overwritten: dict[tuple, ObservationRevision] = {}
    for rev in session.scalars(select(ObservationRevision).where(
            ObservationRevision.replaced_at.in_(moments), ObservationRevision.replaced_by.not_like("revert:%"),
            ObservationRevision.series_code.in_({o.series_code for o in current}))) if current else []:
        overwritten.setdefault((rev.series_code, rev.obs_date, rev.source_code, rev.replaced_at), rev)
    previous: dict[int, ObservationRevision | None] = {}
    for obs in current:
        previous[obs.id] = overwritten.get((obs.series_code, obs.obs_date, obs.source_code, written or obs.updated_at))
        counts["restored" if previous[obs.id] is not None else "removed"] += 1
    rows = list(session.scalars(select(ObservationVintage).where(ObservationVintage.id.in_(added_ids)))) \
        if added_ids else []
    counts["vintage_removed"] = len(rows)
    plan: dict = {**counts, "series": sorted({o.series_code for o in current})}
    if dry_run:
        return {"batch_id": batch_id, "filename": batch.filename, "dry_run": True, **plan}

    now = utcnow()
    who = REVERT_ACTOR.format(id=batch_id)
    if current:             # 被撤銷的值記進修訂記錄（批量寫：一批可能有二十多萬個點）
        session.execute(insert(ObservationRevision), [
            {"series_code": o.series_code, "obs_date": o.obs_date, "source_code": o.source_code, "value": o.value,
             "batch_id": o.batch_id, "source_ref": o.source_ref, "recorded_at": o.updated_at, "replaced_at": now,
             "replaced_by": who} for o in current])
    gone = [obs.id for obs in current if previous[obs.id] is None]           # 這一批新加的點
    for obs in current:
        old = previous[obs.id]
        if old is not None:                                  # 這一批覆蓋掉的舊值
            obs.value, obs.batch_id, obs.source_ref, obs.updated_at = old.value, old.batch_id, old.source_ref, now
    session.flush()
    for i in range(0, len(gone), 5000):
        session.execute(delete(Observation).where(Observation.id.in_(gone[i:i + 5000])),
                        execution_options={"synchronize_session": False})
    vintage_gone = [r.id for r in rows]
    for i in range(0, len(vintage_gone), 5000):
        session.execute(delete(ObservationVintage).where(ObservationVintage.id.in_(vintage_gone[i:i + 5000])),
                        execution_options={"synchronize_session": False})
    session.expire_all()
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
