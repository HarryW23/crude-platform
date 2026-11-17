"""參數：當前值、版本歷史、新增版本、核對。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import formula as fx
from ... import parameters as service
from ...models import Parameter, ParameterVersion
from ..deps import AppState, get_actor, get_db, get_state

router = APIRouter(tags=["parameters"])            # 兩個站點都有：只讀
admin_router = APIRouter(tags=["parameters-admin"])  # 只在後台：改參數


class NewVersion(BaseModel):
    value: float
    valid_from: date | None = None      # 不給 = 更正目前生效的版本
    note: str | None = None


class VerifyRequest(BaseModel):
    verified: bool = True


@router.get("/parameters")
def list_parameters(session: Session = Depends(get_db), state: AppState = Depends(get_state)):
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
def list_versions(key: str, session: Session = Depends(get_db)):
    if session.get(Parameter, key) is None:
        raise HTTPException(404, f"沒有名為 {key} 的參數")
    effective = {v.id for v in service.effective_versions(session, key)}
    rows = session.scalars(select(ParameterVersion).where(ParameterVersion.key == key)
                           .order_by(ParameterVersion.valid_from.desc(), ParameterVersion.id.desc()))
    return [{"valid_from": v.valid_from, "value": v.value, "note": v.note, "created_by": v.created_by,
             "created_at": v.created_at, "effective": v.id in effective} for v in rows]


@admin_router.post("/parameters/{key}/versions")
def add_version(key: str, body: NewVersion, session: Session = Depends(get_db), actor: str = Depends(get_actor)):
    try:
        version = service.set_value(session, key, body.value, body.valid_from, body.note, actor)
    except service.ParameterError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key": key, "value": version.value, "valid_from": version.valid_from}


@admin_router.post("/parameters/{key}/verify")
def verify(key: str, body: VerifyRequest, session: Session = Depends(get_db)):
    try:
        service.set_verified(session, key, body.verified)
    except service.ParameterError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"key": key, "verified": body.verified}
