"""觀點記錄與覆盤。只在前台。記下的觀點不能改，只能撤銷。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import journal as service
from ..deps import DB, AppState, get_actor, get_state

router = APIRouter(tags=["journal"])


class NewView(BaseModel):
    horizon: str
    direction: int
    low: float | None = None
    high: float | None = None
    confidence: float | None = Field(default=None, ge=0.5, le=1)
    reasons: list[str] = Field(default=[], max_length=10)
    rationale: str | None = Field(default=None, max_length=4000)


class Cancel(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.get("/views")
def views(session: Session = DB, state: AppState = Depends(get_state)):
    """全部觀點（含到期後的評分）和匯總統計。"""
    return service.overview(session, state.cfg)


@router.post("/views")
def create_view(body: NewView, session: Session = DB, state: AppState = Depends(get_state),
                actor: str = Depends(get_actor)):
    try:
        row = service.create(session, state.cfg, actor=actor, **body.model_dump())
    except service.JournalError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"id": row.id, "target_date": row.target_date, "start_price": row.start_price}


@router.post("/views/{view_id}/cancel")
def cancel_view(view_id: int, body: Cancel, session: Session = DB, actor: str = Depends(get_actor)):
    try:
        service.cancel(session, view_id, body.reason, actor)
    except service.JournalError as exc:
        raise HTTPException(404 if "沒有編號" in str(exc) else 400, str(exc)) from None
    return {"id": view_id, "cancelled": True}
