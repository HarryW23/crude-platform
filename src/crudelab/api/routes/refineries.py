"""煉廠模型：列表、毛利拆解、情境試算。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...indicators import IndicatorEngine, Scenario
from ...refinery import RefinerySpec, breakdown
from ..deps import DB, AppState, get_base_engine, get_engine, get_state

router = APIRouter(tags=["refineries"])


class ScenarioRequest(BaseModel):
    on: date | None = None
    parameters: dict[str, float] = {}        # 參數 key → 試算值
    shifts: dict[str, float] = {}            # 序列或指標代碼 → 在其值上加的數


def _spec(state: AppState, code: str) -> RefinerySpec:
    if code not in state.cfg.refineries:
        raise HTTPException(404, f"沒有名為 {code} 的煉廠模型")
    return state.cfg.refineries[code]


def _info(spec: RefinerySpec, engine: IndicatorEngine) -> dict:
    d = spec.definition
    return {"code": d.code, "name_zh": d.name_zh, "kind": d.kind, "location": d.location, "unit": d.unit,
            "notes": d.notes, "has_tax": d.tax is not None, "margin": engine.summary(spec.margin)}


def _on(engine: IndicatorEngine, spec: RefinerySpec, on: date | None) -> date | None:
    if on is not None:
        return on
    values = engine.compute(spec.margin)
    return values.index[-1].date() if len(values) else None


@router.get("/refineries")
def list_models(state: AppState = Depends(get_state), engine: IndicatorEngine = Depends(get_engine)):
    return [_info(spec, engine) for spec in state.cfg.refineries.values()]


@router.get("/refineries/{code}")
def model_detail(code: str, on: date | None = None, state: AppState = Depends(get_state),
                 engine: IndicatorEngine = Depends(get_engine)):
    spec = _spec(state, code)
    on = _on(engine, spec, on)
    series, params = engine.dependencies(spec.margin)
    return {**_info(spec, engine),
            "history": [{"date": ts.date(), "value": float(v)} for ts, v in engine.compute(spec.margin).items()],
            "breakdown": breakdown(engine, spec, on) if on else None,
            "inputs": {"series": series, "parameters": params}}


@router.post("/refineries/{code}/scenario")
def model_scenario(code: str, body: ScenarioRequest, session: Session = DB,
                   state: AppState = Depends(get_state), base: IndicatorEngine = Depends(get_base_engine)):
    """用臨時的參數值或價格平移重算毛利，不保存。回傳基準和情境兩套拆解。"""
    spec = _spec(state, code)
    try:
        scenario = IndicatorEngine(session, state.cfg, as_of=base.as_of,
                                   scenario=Scenario(body.parameters, body.shifts))
    except KeyError as exc:
        raise HTTPException(400, str(exc.args[0])) from exc
    on = _on(base, spec, body.on) or _on(scenario, spec, None)
    if on is None:
        raise HTTPException(400, "這個模型目前算不出結果，無法試算")
    return {"date": on, "base": breakdown(base, spec, on), "scenario": breakdown(scenario, spec, on)}
