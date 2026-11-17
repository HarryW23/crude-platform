"""定時任務：查看安排和執行記錄、手動立即運行。只在後台。"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import jobs, scheduler
from ...models import AuditLog
from ..deps import AppState, get_actor, get_db, get_state

admin_router = APIRouter(tags=["schedule"])


@admin_router.get("/schedule")
def schedule_status(state: AppState = Depends(get_state)):
    """調度器是否在運行，以及每個任務的時間安排、下次運行時間和最近一次結果。時間按 schedule.yaml 的時區顯示。"""
    return scheduler.status(state.cfg)


@admin_router.get("/schedule/runs")
def runs(limit: int = 100, job: str | None = None, session: Session = Depends(get_db),
         state: AppState = Depends(get_state)):
    tz = ZoneInfo(state.cfg.schedule.timezone)
    return [scheduler.run_view(r, tz) for r in jobs.recent_runs(session, min(limit, 500), job)]


@admin_router.post("/schedule/{code}/run")
def run_now(code: str, session: Session = Depends(get_db), state: AppState = Depends(get_state),
            actor: str = Depends(get_actor)):
    """立即執行一個任務，不管它是否啟用、是否到點。等它執行完才回應。"""
    try:
        result = jobs.run_now(state.cfg, state.settings, code)
    except jobs.JobError as exc:
        raise HTTPException(404 if "沒有名為" in str(exc) else 409, str(exc)) from exc
    session.add(AuditLog(actor=actor, action="job_run", entity="job", entity_id=code,
                         detail={"status": result["status"]}))
    return result
