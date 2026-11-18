"""地緣：事件庫、事件預案、從文字抽取事件（大模型）。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import events as service
from ... import scenarios
from ...llm import LLMError, LLMNotConfigured, get_provider
from ...llm.tasks import extract_events
from ..deps import AppState, get_actor, get_db, get_state

router = APIRouter(tags=["geo"])


class EventIn(BaseModel):
    title: str
    category: str = "other"
    status: str = "confirmed"
    bias: int = 0
    location_code: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    summary: str | None = None
    source: str | None = None


class EventPatch(BaseModel):
    title: str | None = None
    category: str | None = None
    status: str | None = None
    bias: int | None = None
    location_code: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    summary: str | None = None
    source: str | None = None
    draft: bool | None = None      # 傳 false = 確認草稿


class ImpactIn(BaseModel):
    target: str
    delta: float
    duration_days: int | None = Field(default=None, ge=1)
    note: str | None = None


class ApplyPlaybook(BaseModel):
    playbook: str
    start: date
    impacts: list[ImpactIn] | None = None      # 不給 = 用預案裡的示意值


class ExtractRequest(BaseModel):
    text: str = Field(min_length=10, max_length=20000)


def _event(e, session: Session, state: AppState) -> dict:
    location = state.cfg.locations.get(e.location_code) if e.location_code else None
    return {"id": e.id, "title": e.title, "category": e.category, "status": e.status, "bias": e.bias,
            "location_code": e.location_code, "location_name": location.name_zh if location else None,
            "started_on": e.started_on, "ended_on": e.ended_on, "summary": e.summary, "source": e.source,
            "scenario_code": e.scenario_code, "origin": e.origin, "draft": e.draft,
            "adjustments": len(scenarios.adjustments(session, e.scenario_code)) if e.scenario_code else 0,
            "created_at": e.created_at, "updated_at": e.updated_at}


@router.get("/events")
def list_events(include_closed: bool = True, session: Session = Depends(get_db),
                state: AppState = Depends(get_state)):
    return [_event(e, session, state) for e in service.list_events(session, include_closed)]


@router.post("/events")
def create_event(body: EventIn, session: Session = Depends(get_db), state: AppState = Depends(get_state),
                 actor: str = Depends(get_actor)):
    try:
        return _event(service.create(session, state.cfg, **body.model_dump(), actor=actor), session, state)
    except service.EventError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/events/{event_id}")
def update_event(event_id: int, body: EventPatch, session: Session = Depends(get_db),
                 state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    try:
        row = service.update(session, state.cfg, event_id, body.model_dump(exclude_unset=True), actor)
    except service.EventError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _event(row, session, state)


@router.get("/playbooks")
def list_playbooks(state: AppState = Depends(get_state)):
    cfg = state.cfg
    return [{"code": p.code, "name_zh": p.name_zh, "category": p.category, "description": p.description,
             "impacts": [{**i.model_dump(), "target_name": cfg.name_of(i.target), "unit": cfg.unit_of(i.target)}
                         for i in p.impacts]} for p in cfg.playbooks.values()]


@router.post("/events/{event_id}/apply-playbook")
def apply_playbook(event_id: int, body: ApplyPlaybook, session: Session = Depends(get_db),
                   state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    """把預案套用到事件上，生成這個事件的情境。之後可以在「供需平衡」裡選這個情境查看和修改。"""
    try:
        scenario = service.apply_playbook(
            session, state.cfg, event_id, body.playbook, body.start,
            [i.model_dump() for i in body.impacts] if body.impacts is not None else None, actor)
    except service.EventError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"scenario_code": scenario.code, "adjustments": len(scenarios.adjustments(session, scenario.code))}


@router.post("/events/extract")
def extract(body: ExtractRequest, session: Session = Depends(get_db), state: AppState = Depends(get_state),
            actor: str = Depends(get_actor)):
    """讓大模型從一段新聞或筆記裡抽取事件，存成草稿。草稿不參與任何計算，要人確認後才生效。"""
    try:
        provider = get_provider("extract", state.cfg.llm)
        found, dropped = extract_events(provider, body.text, state.cfg)
    except LLMNotConfigured as exc:
        raise HTTPException(409, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    excerpt = body.text.strip()[:1000]
    rows = [service.create(session, state.cfg, **e.model_dump(), source=excerpt, origin="llm", draft=True,
                           actor=f"{actor}+{provider.name}") for e in found]
    return {"provider": provider.name, "model": provider.model, "dropped": dropped,
            "drafts": [_event(r, session, state) for r in rows]}
