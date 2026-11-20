"""參數：當前值、版本歷史、新增版本、核對。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import formula as fx
from ... import parameters as service
from ...indicators import IndicatorEngine
from ...models import Parameter, ParameterVersion
from ..deps import DB, AppState, get_actor, get_engine, get_state

router = APIRouter(tags=["parameters"])            # 兩個站點都有：只讀
admin_router = APIRouter(tags=["parameters-admin"])  # 只在後台：改參數


class NewVersion(BaseModel):
    value: float
    valid_from: date | None = None      # 不給 = 更正目前生效的版本
    note: str | None = None


class VerifyRequest(BaseModel):
    verified: bool = True


@router.get("/parameters")
def list_parameters(session: Session = DB, state: AppState = Depends(get_state)):
    used: dict[str, list[str]] = {}
    for code, ind in state.cfg.indicators.items():
        for name in fx.names(ind.formula):
            if name in state.cfg.parameters:
                used.setdefault(name, []).append(code)
    today = date.today()
    out = []
    for p in session.scalars(select(Parameter).where(Parameter.active).order_by(Parameter.group, Parameter.key)):
        versions = service.effective_versions(session, p.key)
        current = next((v for v in reversed(versions) if v.valid_from <= today), None)
        out.append({"key": p.key, "name_zh": p.name_zh, "unit": p.unit, "group": p.group, "model": p.model,
                    "description": p.description, "verified": p.verified,
                    "value": current.value if current else None,
                    "valid_from": current.valid_from if current else None,
                    "n_versions": len(versions), "used_by": used.get(p.key, [])})
    return out


@router.get("/parameters/{key}/versions")
def list_versions(key: str, session: Session = DB):
    if session.get(Parameter, key) is None:
        raise HTTPException(404, f"沒有名為 {key} 的參數")
    effective = {v.id for v in service.effective_versions(session, key)}
    rows = session.scalars(select(ParameterVersion).where(ParameterVersion.key == key)
                           .order_by(ParameterVersion.valid_from.desc(), ParameterVersion.id.desc()))
    return [{"valid_from": v.valid_from, "value": v.value, "note": v.note, "created_by": v.created_by,
             "created_at": v.created_at, "effective": v.id in effective} for v in rows]


@admin_router.post("/parameters/{key}/versions")
def add_version(key: str, body: NewVersion, session: Session = DB, actor: str = Depends(get_actor)):
    try:
        version = service.set_value(session, key, body.value, body.valid_from, body.note, actor)
    except service.ParameterError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key": key, "value": version.value, "valid_from": version.valid_from}


@admin_router.post("/parameters/{key}/verify")
def verify(key: str, body: VerifyRequest, session: Session = DB):
    try:
        service.set_verified(session, key, body.verified)
    except service.ParameterError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"key": key, "verified": body.verified}


@router.get("/projection/sensitivity")
def sensitivity_estimate(session: Session = DB, state: AppState = Depends(get_state),
                         engine: IndicatorEngine = Depends(get_engine)):
    """推演敏感度的歷史估計（見 sensitivity.py），以及參數現在的值。兩個站點都能看。"""
    from ... import sensitivity
    result = sensitivity.estimate(engine, state.cfg)
    key = result["parameter"]
    result["current"] = engine.value_at(key, engine.as_of.date() if engine.as_of else date.today()) if key else None
    return result


@admin_router.post("/projection/sensitivity/adopt")
def adopt_sensitivity(session: Session = DB, state: AppState = Depends(get_state),
                      engine: IndicatorEngine = Depends(get_engine), actor: str = Depends(get_actor)):
    """把歷史估計寫成敏感度參數的一個新版本（今天起生效），版本說明裡記下估計的依據。只接受結論為 ok 的估計。"""
    from ... import sensitivity
    result = sensitivity.estimate(engine, state.cfg)
    if result["status"] != "ok":
        raise HTTPException(409, f"這個估計不建議採用：{result['message']}。可以在參數頁自己填一個值")
    try:
        version = service.set_value(session, result["parameter"], round(result["value"], 4), date.today(),
                                    sensitivity.adoption_note(result), actor)
    except service.ParameterError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key": result["parameter"], "value": version.value, "valid_from": version.valid_from}


# ------------------------------ 按回測校準信號權重 ------------------------------

def _latest_calibration(session: Session):
    from ...models import BacktestRun
    rows = session.scalars(select(BacktestRun).where(BacktestRun.status == "done").order_by(BacktestRun.id.desc()))
    return next((r for r in rows if r.results and r.results.get("calibration")), None)


def _calibration_changes(run, engine: IndicatorEngine) -> list[dict]:
    """建議和現在不同的權重。「現在」按今天生效的參數重新取，不用回測當時的快照。"""
    cal = (run.results or {})["calibration"]
    out = []
    for row in [*cal["signals"], *cal["modules"]]:
        key = row.get("parameter")
        if not key or key not in engine.cfg.parameters:
            continue
        now = engine.value_at(key, date.today())
        if now is None or abs(float(now) - float(row["proposed"])) > 1e-9:
            out.append({"parameter": key, "name_zh": engine.cfg.parameters[key].name_zh, "current": now,
                        "proposed": row["proposed"], "reason": row["reason"], "verdict": row["verdict"],
                        "ic": row["stats"].get("ic"), "ic_t": row["stats"].get("ic_t")})
    return out


@admin_router.get("/calibration/latest")
def latest_calibration(session: Session = DB, engine: IndicatorEngine = Depends(get_engine)):
    """最近一次完成的回測給出的權重建議，和它在逐步前推檢驗裡的表現。"""
    run = _latest_calibration(session)
    if run is None:
        return {"run_id": None}
    cal = run.results["calibration"]
    return {"run_id": run.id, "finished_at": run.finished_at, "mode": run.mode, "horizon": cal["horizon"],
            "horizon_name": cal["horizon_name"], "walk_forward": cal["walk_forward"],
            "adoptable": _adoptable(cal)[0], "why": _adoptable(cal)[1], "changes": _calibration_changes(run, engine)}


def _adoptable(cal: dict) -> tuple[bool, str]:
    walk = cal.get("walk_forward")
    if not walk:
        return False, "回測區間太短，沒有做逐步前推檢驗"
    if walk["calibrated"].get("status") != "ok" or walk["current"].get("status") != "ok":
        return False, "逐步前推的有效樣本不足，不能判斷校準後是否更好"
    if not walk.get("better"):
        return False, "逐步前推檢驗裡，按校準權重合成的分數沒有比現在的權重更好（IC 沒有提高），不建議採用"
    return True, "逐步前推檢驗裡，按校準權重合成的分數比現在的權重更好"


@admin_router.post("/calibration/{run_id}/adopt")
def adopt_calibration(run_id: int, session: Session = DB, engine: IndicatorEngine = Depends(get_engine),
                      actor: str = Depends(get_actor)):
    """把回測的權重建議寫成參數的新版本（今天起生效），版本說明裡記下依據。逐步前推沒有變好就拒絕。"""
    from ...models import BacktestRun
    run = session.get(BacktestRun, run_id)
    if run is None or run.status != "done" or not (run.results or {}).get("calibration"):
        raise HTTPException(404, f"編號 {run_id} 的回測沒有權重建議")
    cal = (run.results or {})["calibration"]
    ok, why = _adoptable(cal)
    if not ok:
        raise HTTPException(409, why + "。可以在參數頁逐個手動修改")
    walk = cal["walk_forward"]
    written = []
    for change in _calibration_changes(run, engine):
        note = (f"按回測 #{run_id} 校準（{cal['horizon_name']}，逐步前推 IC {walk['current']['ic']:.3f} → "
                f"{walk['calibrated']['ic']:.3f}）：{change['reason']}")
        try:
            version = service.set_value(session, change["parameter"], change["proposed"], date.today(), note, actor)
        except service.ParameterError as exc:
            raise HTTPException(400, str(exc)) from exc
        written.append({"parameter": change["parameter"], "value": version.value})
    return {"run_id": run_id, "written": written}
