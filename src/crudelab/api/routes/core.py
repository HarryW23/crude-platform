"""總覽、字典、日誌、維護。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import db
from ...auth import ROLE_NAMES
from ...config import ConfigError
from ...indicators import IndicatorEngine
from ...ingest import latest_market_date
from ...models import AuditLog, ImportBatch, Observation, Parameter
from ..deps import AppState, get_db, get_engine, get_state
from ..security import current_user

router = APIRouter(tags=["core"])                  # 兩個站點都有
admin_router = APIRouter(tags=["admin"])           # 只在後台
health_router = APIRouter(tags=["health"])         # 兩個站點都有，不經過訪問控制（它要在數據庫壞掉時也能回答）


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


@router.get("/freshness")
def data_freshness(session: Session = Depends(get_db), state: AppState = Depends(get_state)):
    """每條被檢查的序列有沒有超過期限沒更新。規則在 config/freshness.yaml。"""
    from ...freshness import check
    return check(session, state.cfg)


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
def site(request: Request, state: AppState = Depends(get_state)):
    """這是哪個站點、另一個站點在哪、要不要登錄、現在是誰。頁面用它畫導覽、登錄框和互相連結。

    這個地址不用登錄就能訪問，所以沒登錄時不回傳配置錯誤這類內部信息。
    """
    user = current_user(request)
    enabled = state.settings.auth_enabled
    return {"site": state.site, "front_url": state.settings.front_url, "admin_url": state.settings.admin_url,
            "auth": {"enabled": enabled, "user": None if user is None else {
                "username": user.username, "role": user.role, "role_name": ROLE_NAMES[user.role]}},
            "config_error": state.config_error if (user is not None or not enabled) else None}


@health_router.get("/health")
def health(require: str | None = None, state: AppState = Depends(get_state)):
    """健康檢查（#33），給外部監控或 crudelab watchdog 定時調用。不用登錄，只回粗粒度的狀態，不含任何內部細節。

    全部正常回 200，否則 503。檢查：
      database   能連上數據庫並查詢
      config     配置文件沒有錯（有錯時程序照用舊配置，但要有人去改）
      scheduler  調度器兩分鐘內寫過心跳（每 30 秒寫一次）。只有 ?require=scheduler 時才算進總狀態：
                 只開前台、或用 --no-scheduler 啟動時調度器本來就不跑
    """
    from ...models import SchedulerHeartbeat, utcnow
    from ...scheduler import HEARTBEAT_FRESH
    checks: dict[str, str] = {}
    try:
        with db.session_scope() as session:
            session.execute(select(1))
            beat = session.get(SchedulerHeartbeat, 1)
        checks["database"] = "ok"
        checks["scheduler"] = ("ok" if beat is not None and utcnow() - beat.at <= HEARTBEAT_FRESH
                               else "stale" if beat is not None else "never_ran")
    except Exception:                           # 不回傳異常原文：裡面可能有連接串
        checks["database"], checks["scheduler"] = "error", "unknown"
    checks["config"] = "error" if state.config_error else "ok"
    required = ["database", "config", *(["scheduler"] if require == "scheduler" else [])]
    ok = all(checks[k] == "ok" for k in required)
    return JSONResponse({"status": "ok" if ok else "fail", "site": state.site, "checks": checks,
                         "required": required}, status_code=200 if ok else 503)


@admin_router.post("/admin/reload-config")
def reload_config(state: AppState = Depends(get_state)):
    """立即重新載入 config/ 並回報結果。平時不需要調用：兩個站點都會自動發現配置文件的改動。

    配置有錯時回傳錯誤清單，並保持原配置不變。
    """
    try:
        return state.reload()
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
