"""序列：字典、數據、手動錄入、修訂歷史。"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...data_needs import ric_of
from ...ingest import IngestError, Obs, load_points, load_points_cached, write_observations
from ...models import AuditLog, Observation, ObservationRevision
from ..deps import DB, AppState, get_actor, get_state

router = APIRouter(tags=["series"])                # 兩個站點都有：只讀
admin_router = APIRouter(tags=["series-admin"])    # 只在後台：錄入數據


class ManualPoint(BaseModel):
    series_code: str
    obs_date: date
    value: float


class ManualEntry(BaseModel):
    points: list[ManualPoint]


@router.get("/series")
def list_series(session: Session = DB, state: AppState = Depends(get_state)):
    coverage = {code: (n, first, last) for code, n, first, last in session.execute(
        select(Observation.series_code, func.count(func.distinct(Observation.obs_date)),
               func.min(Observation.obs_date), func.max(Observation.obs_date))
        .group_by(Observation.series_code))}
    # 和指標引擎用同一個緩存、同一份序列清單（緩存只存一份，清單不同會互相擠掉）：數據沒變時不再重讀全部數據點
    points = load_points_cached(session, sorted(state.cfg.series))
    latest = points.sort_values("obs_date").groupby("series_code").last() if not points.empty else None
    out = []
    for s in state.cfg.series.values():
        n, first, last = coverage.get(s.code, (0, None, None))
        out.append({"code": s.code, "name_zh": s.name_zh, "commodity": s.commodity, "location": s.location,
                    "kind": s.kind, "unit": s.unit, "frequency": s.frequency, "module": s.module,
                    "benchmark": s.benchmark, "n": n, "first_date": first, "last_date": last,
                    "ric": ric_of(s), "proxy": s.lseg.proxy if s.lseg else None,
                    "last_value": float(latest.loc[s.code, "value"]) if n and latest is not None else None,
                    "last_source": latest.loc[s.code, "source_code"] if n and latest is not None else None})
    return out


@router.get("/series/{code}/data")
def series_data(code: str, start: date | None = None, end: date | None = None, as_of: datetime | None = None,
                session: Session = DB, state: AppState = Depends(get_state)):
    if code not in state.cfg.series:
        raise HTTPException(404, f"沒有名為 {code} 的序列")
    df = load_points(session, [code], start, end, as_of=as_of.replace(tzinfo=None) if as_of else None)
    return [{"date": r.obs_date, "value": r.value, "source": r.source_code, "ref": r.source_ref}
            for r in df.itertuples()]


@router.get("/series/{code}/revisions")
def series_revisions(code: str, session: Session = DB, state: AppState = Depends(get_state)):
    if code not in state.cfg.series:
        raise HTTPException(404, f"沒有名為 {code} 的序列")
    rows = session.scalars(select(ObservationRevision).where(ObservationRevision.series_code == code)
                           .order_by(ObservationRevision.id.desc()).limit(500))
    return [{"date": r.obs_date, "source": r.source_code, "old_value": r.value, "old_ref": r.source_ref,
             "recorded_at": r.recorded_at, "replaced_at": r.replaced_at, "replaced_by": r.replaced_by} for r in rows]


@admin_router.post("/observations")
def manual_entry(body: ManualEntry, session: Session = DB, actor: str = Depends(get_actor)):
    try:
        stats = write_observations(
            session, [Obs(p.series_code, p.obs_date, p.value, "後台手動錄入") for p in body.points],
            "manual", actor=actor)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.add(AuditLog(actor=actor, action="manual_entry", entity="observation",
                         entity_id=",".join(sorted({p.series_code for p in body.points}))[:128], detail=stats))
    return stats
