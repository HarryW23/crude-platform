"""備份：查看備份、立即備份、校驗。只在後台。恢復只能用命令行（crudelab restore），不從網頁做。"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import backup
from ...models import AuditLog
from ..deps import AppState, get_actor, get_db, get_state

admin_router = APIRouter(tags=["backups"])


@admin_router.get("/backups")
def backup_status(state: AppState = Depends(get_state)):
    cfg, settings = state.cfg, state.settings
    rows = backup.list_backups(settings, cfg)
    good = [r for r in rows if not r["error"]]
    age = ((datetime.now(UTC) - datetime.fromisoformat(good[0]["created_at"])).total_seconds() / 3600
           if good else None)
    jobs = [j for j in cfg.schedule.jobs if j.action == "backup"]
    try:
        kind = backup.database_kind(settings)
    except backup.BackupError:
        kind = None
    return {"directory": str(backup.directory(settings, cfg)), "same_disk": backup.same_disk(settings, cfg),
            "database": kind, "settings": cfg.backup.model_dump(), "timezone": cfg.schedule.timezone,
            "age_hours": age, "stale": age is None or age > cfg.backup.max_age_hours,
            "jobs": [{"code": j.code, "name_zh": j.name_zh} for j in jobs], "backups": rows}


@admin_router.post("/backups")
def backup_now(session: Session = Depends(get_db), state: AppState = Depends(get_state),
               actor: str = Depends(get_actor)):
    """立即做一份手動備份（含校驗）。等做完才回應。"""
    try:
        made = backup.create(state.settings, state.cfg, "manual")
    except backup.BackupError as exc:
        raise HTTPException(409, str(exc)) from None
    session.add(AuditLog(actor=actor, action="backup", entity="backup", entity_id=made["name"],
                         detail={"size": made["size"]}))
    return made


@admin_router.post("/backups/{name}/verify")
def verify_backup(name: str, state: AppState = Depends(get_state)):
    try:
        path = backup.resolve(state.settings, state.cfg, name)
        return backup.verify(path)
    except backup.BackupError as exc:
        return {"ok": False, "name": name, "message": str(exc)}
