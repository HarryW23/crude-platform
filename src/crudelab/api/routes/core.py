"""總覽、字典、日誌、維護。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import ConfigError
from ...indicators import IndicatorEngine
from ...ingest import latest_market_date
from ...models import AuditLog, ImportBatch, Observation, Parameter
from ..deps import AppState, get_db, get_engine, get_state

router = APIRouter(tags=["core"])                  # 兩個站點都有
admin_router = APIRouter(tags=["admin"])           # 只在後台


@router.get("/overview")
def overview(session: Session = Depends(get_db), state: AppState = Depends(get_state),
             engine: IndicatorEngine = Depends(get_engine)):
    visible = [engine.summary(code) for code, ind in state.cfg.indicators.items() if not ind.hidden]
    statuses = [s["status"] for s in visible]
    params = list(session.scalars(select(Parameter).where(Parameter.active)))
    return {
        "series_total": len(state.cfg.series),
        "series_with_data": session.scalar(select(func.count(func.distinct(Observation.series_code)))) or 0,
        "observations": session.scalar(select(func.count()).select_from(Observation)),
        "last_obs_date": latest_market_date(session, state.cfg.market_series),
        "indicators_total": len(visible),
        "indicators_ok": statuses.count("ok"),
        "indicators_missing_data": statuses.count("missing_data") + statuses.count("no_overlap"),
        "indicators_missing_parameter": statuses.count("missing_parameter"),
        "parameters_total": len(params),
        "parameters_unverified": sum(not p.verified for p in params),
        "parameters_missing": sum(1 for p in params if not engine.parameter_values(p.key).size),
        "refinery_models": len(state.cfg.refineries),
        "balances": len(state.cfg.balances),
        "batches": session.scalar(select(func.count()).select_from(ImportBatch)
                                  .where(ImportBatch.status == "committed")),
    }


@router.get("/meta")
def meta(state: AppState = Depends(get_state)):
    cfg = state.cfg
    return {"commodities": {c.code: c.name_zh for c in cfg.commodities.values()},
            "locations": {loc.code: {"name_zh": loc.name_zh, "region": loc.region} for loc in cfg.locations.values()},
            "units": {u.code: u.name_zh for u in cfg.units.values()}}


@admin_router.get("/audit")
def audit(limit: int = 200, session: Session = Depends(get_db)):
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000)))
    return [{"ts": a.ts, "actor": a.actor, "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "detail": a.detail} for a in rows]


@router.get("/site")
def site(state: AppState = Depends(get_state)):
    """這是哪個站點、另一個站點在哪、配置是否有錯。頁面用它畫導覽和互相連結。"""
    return {"site": state.site, "front_url": state.settings.front_url, "admin_url": state.settings.admin_url,
            "config_error": state.config_error}


@admin_router.post("/admin/reload-config")
def reload_config(state: AppState = Depends(get_state)):
    """立即重新載入 config/ 並回報結果。平時不需要調用：兩個站點都會自動發現配置文件的改動。

    配置有錯時回傳錯誤清單，並保持原配置不變。
    """
    try:
        return state.reload()
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
