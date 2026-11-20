"""情境與調整：建立、列出、歸檔情境；在情境裡加、撤調整。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import scenarios as service
from ...models import ScenarioRow
from ..deps import DB, AppState, get_actor, get_state

router = APIRouter(tags=["scenarios"])


class NewScenario(BaseModel):
    code: str
    name_zh: str
    description: str | None = None


class NewAdjustment(BaseModel):
    target: str                    # 序列或指標代碼
    date_from: date
    date_to: date | None = None    # 不給 = 一直持續
    delta: float
    note: str | None = None        # 依據


def _adjustment(a, state: AppState) -> dict:
    cfg = state.cfg
    name = (cfg.indicators[a.target].name_zh if a.target in cfg.indicators
            else cfg.series[a.target].name_zh if a.target in cfg.series else None)
    return {"id": a.id, "target": a.target, "target_name": name, "unit": cfg.unit_of(a.target) if name else None,
            "date_from": a.date_from, "date_to": a.date_to, "delta": a.delta, "note": a.note,
            "created_by": a.created_by, "created_at": a.created_at, "orphaned": name is None}


@router.get("/scenarios")
def list_scenarios(session: Session = DB):
    rows = session.scalars(select(ScenarioRow).where(ScenarioRow.archived_at.is_(None))
                           .order_by(ScenarioRow.created_at))
    return [{"code": s.code, "name_zh": s.name_zh, "description": s.description, "created_at": s.created_at,
             "adjustments": len(service.adjustments(session, s.code))} for s in rows]


@router.post("/scenarios")
def create_scenario(body: NewScenario, session: Session = DB, actor: str = Depends(get_actor)):
    try:
        row = service.create(session, body.code, body.name_zh, body.description, actor)
    except service.ScenarioError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": row.code, "name_zh": row.name_zh}


@router.delete("/scenarios/{code}")
def archive_scenario(code: str, session: Session = DB):
    try:
        service.archive(session, code)
    except service.ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"code": code, "archived": True}


@router.get("/scenarios/{code}/adjustments")
def list_adjustments(code: str, session: Session = DB, state: AppState = Depends(get_state)):
    if session.get(ScenarioRow, code) is None:
        raise HTTPException(404, f"沒有名為 {code} 的情境")
    return [_adjustment(a, state) for a in service.adjustments(session, code)]


@router.post("/scenarios/{code}/adjustments")
def add_adjustment(code: str, body: NewAdjustment, session: Session = DB,
                   state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    try:
        row = service.add_adjustment(session, state.cfg, code, body.target, body.date_from, body.date_to,
                                     body.delta, body.note, actor)
    except service.ScenarioError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _adjustment(row, state)


@router.delete("/adjustments/{adjustment_id}")
def remove_adjustment(adjustment_id: int, session: Session = DB):
    try:
        service.remove_adjustment(session, adjustment_id)
    except service.ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": adjustment_id, "removed": True}
