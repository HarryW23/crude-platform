"""推演：方向信號、價格推演的設定與結果。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import fair_value
from ... import projection as service
from ... import signals as signal_service
from ...indicators import IndicatorEngine
from ...models import Projection
from ..deps import DB, AppState, get_actor, get_as_of, get_engine, get_state

router = APIRouter(tags=["outlook"])


class NewProjection(BaseModel):
    name_zh: str
    drift_model: str | None = None      # 用哪個公允價值模型給基準漂移；不給 = 不加


class ProjectionPatch(BaseModel):
    drift_model: str | None = None


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
def signals(session: Session = DB, engine: IndicatorEngine = Depends(get_engine)):
    return signal_service.compute(session, engine)


@router.get("/calendar")
def calendar(days_back: int = 7, days_ahead: int = 21, min_star: int = 0, session: Session = DB,
             state: AppState = Depends(get_state)):
    """財經日曆（來自金十）：最近幾天已公佈的和之後幾周要公佈的，附「實際 − 公佈時的預期」。"""
    from ... import credentials, jin10
    rows = jin10.calendar_view(session, max(0, min(days_back, 90)), max(0, min(days_ahead, 90)), min_star)
    rules = [r.match for r in state.cfg.jin10.calendar_rules]
    for r in rows:
        r["tracked"] = any(m in r["title"] for m in rules)
    return {"configured": bool(credentials.get("JIN10_TOKEN")), "events": rows}


@router.get("/signals/track-record")
def track_record(session: Session = DB, state: AppState = Depends(get_state)):
    """最近一次完成的回測裡，各信號、模組、綜合分數在各期限的表現和結論（有效 / 方向相反 / 不顯著 / 樣本不足）。"""
    from ... import backtest
    return backtest.track_record(session, state.cfg)


@router.get("/fair-value")
def fair_values(state: AppState = Depends(get_state), engine: IndicatorEngine = Depends(get_engine)):
    """每個公允價值模型的最新結果：公允價值、偏離、因子拆解、偏離的回歸速度、各期限的漂移。"""
    horizons = service.horizons_of(state.cfg)
    return [fair_value.describe(state.cfg, fair_value.current(engine, state.cfg, code), horizons)
            for code in state.cfg.fair_values]


@router.get("/fair-value/{code}")
def fair_value_detail(code: str, days: int = 500, state: AppState = Depends(get_state),
                      engine: IndicatorEngine = Depends(get_engine)):
    """加上歷史：每一天都只用當時之前的數據擬合。"""
    if code not in state.cfg.fair_values:
        raise HTTPException(404, f"沒有名為 {code} 的公允價值模型")
    out = fair_value.describe(state.cfg, fair_value.current(engine, state.cfg, code), service.horizons_of(state.cfg))
    out["history"] = fair_value.history(engine, state.cfg, code, max(20, min(days, 1500)))
    return out


@router.get("/projection/drift-sources")
def drift_sources(session: Session = DB, state: AppState = Depends(get_state)):
    """推演可以選的漂移來源：公允價值模型和統計漂移模型，都要通過回測才能選，否則 why 說明原因。"""
    sources = [(code, m.name_zh, "fair_value") for code, m in state.cfg.fair_values.items()]
    sources += [(code, m.name_zh, m.kind) for code, m in service.stat_models(state.cfg).items()]
    out = []
    for code, name, kind in sources:
        ok, why = service.drift_verdict(session, code)
        out.append({"code": code, "name_zh": name, "kind": kind, "available": ok, "why": why})
    return out


@router.get("/projections")
def list_projections(session: Session = DB):
    rows = session.scalars(select(Projection).where(Projection.archived_at.is_(None)).order_by(Projection.id))
    return [{"id": p.id, "name_zh": p.name_zh, "created_at": p.created_at, "drift_model": p.drift_model,
             "cases": len(service.cases(session, p.id))} for p in rows]


@router.post("/projections")
def create_projection(body: NewProjection, session: Session = DB, state: AppState = Depends(get_state),
                      actor: str = Depends(get_actor)):
    try:
        row = service.create(session, body.name_zh, actor, body.drift_model, state.cfg)
    except service.ProjectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": row.id, "name_zh": row.name_zh, "drift_model": row.drift_model}


@router.patch("/projections/{projection_id}")
def update_projection(projection_id: int, body: ProjectionPatch, session: Session = DB,
                      state: AppState = Depends(get_state)):
    """選擇漂移來源（公允價值模型或通過回測的統計漂移模型的代碼），null = 不加。"""
    try:
        row = service.set_drift_model(session, state.cfg, projection_id, body.drift_model)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": row.id, "drift_model": row.drift_model}


@router.get("/projections/{projection_id}")
def projection_result(projection_id: int, session: Session = DB,
                      state: AppState = Depends(get_state), as_of: datetime | None = Depends(get_as_of)):
    try:
        return service.compute(session, state.cfg, projection_id, as_of)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc


@router.post("/projections/{projection_id}/cases")
def add_case(projection_id: int, body: NewCase, session: Session = DB,
             state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    try:
        row = service.add_case(session, state.cfg, projection_id, body.label, body.scenario_code,
                               body.probability, body.price_adjustment, body.note, actor)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": row.id}


@router.patch("/projection-cases/{case_id}")
def update_case(case_id: int, body: CasePatch, session: Session = DB):
    try:
        row = service.update_case(session, case_id, body.probability, body.price_adjustment, body.note)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": row.id, "probability": row.probability, "price_adjustment": row.price_adjustment}


@router.post("/projections/{projection_id}/sync-events")
def sync_events(projection_id: int, session: Session = DB, state: AppState = Depends(get_state),
                actor: str = Depends(get_actor)):
    """把有機率的地緣事件同步成這個推演的分支（每個事件一個分支，剩下的機率歸基準）。可以反覆同步。"""
    try:
        return service.sync_events(session, state.cfg, projection_id, actor)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc


@router.delete("/projection-cases/{case_id}")
def remove_case(case_id: int, session: Session = DB):
    try:
        service.remove_case(session, case_id)
    except service.ProjectionError as exc:
        raise _fail(exc) from exc
    return {"id": case_id, "removed": True}
