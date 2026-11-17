"""推演：方向信號、價格推演的設定與結果。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import projection as service
from ... import signals as signal_service
from ...indicators import IndicatorEngine
from ...models import Projection
from ..deps import AppState, get_actor, get_as_of, get_db, get_engine, get_state

router = APIRouter(tags=["outlook"])


class NewProjection(BaseModel):
    name_zh: str


class NewCase(BaseModel):
    label: str
    scenario_code: str | None = None       # 不給 = 基準
    probability: float
    price_adjustment: float = 0.0
    note: str | None = None


class CasePatch(BaseModel):
    probability: float | None = None
    price_adjustment: float | None = None
    note: str | None = None


def _fail(exc: service.ProjectionError):
    return HTTPException(404 if "沒有編號" in str(exc) else 400, str(exc))


@router.get("/signals")
def signals(session: Session = Depends(get_db), engine: IndicatorEngine = Depends(get_engine)):
    return signal_service.compute(session, engine)


@router.get("/projections")
def list_projections(session: Session = Depends(get_db)):
    rows = session.scalars(select(Projection).where(Projection.archived_at.is_(None)).order_by(Projection.id))
    return [{"id": p.id, "name_zh": p.name_zh, "created_at": p.created_at,
             "cases": len(service.cases(session, p.id))} for p in rows]


@router.post("/projections")
def create_projection(body: NewProjection, session: Session = Depends(get_db), actor: str = Depends(get_actor)):
    row = service.create(session, body.name_zh, actor)
    return {"id": row.id, "name_zh": row.name_zh}


@router.get("/projections/{projection_id}")
def projection_result(projection_id: int, session: Session = Depends(get_db),
                      state: AppState = Depends(get_state), as_of: datetime | None = Depends(get_as_of)):
    try:
        return service.compute(session, state.cfg, projection_id, as_of)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc


@router.post("/projections/{projection_id}/cases")
def add_case(projection_id: int, body: NewCase, session: Session = Depends(get_db),
             state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    try:
        row = service.add_case(session, state.cfg, projection_id, body.label, body.scenario_code,
                               body.probability, body.price_adjustment, body.note, actor)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": row.id}


@router.patch("/projection-cases/{case_id}")
def update_case(case_id: int, body: CasePatch, session: Session = Depends(get_db)):
    try:
        row = service.update_case(session, case_id, body.probability, body.price_adjustment, body.note)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": row.id, "probability": row.probability, "price_adjustment": row.price_adjustment}


@router.delete("/projection-cases/{case_id}")
def remove_case(case_id: int, session: Session = Depends(get_db)):
    try:
        service.remove_case(session, case_id)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": case_id, "removed": True}
