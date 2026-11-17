"""數據源：狀態、手動觸發取數。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...connectors import ConnectorError, ConnectorNotReady, get_connector, list_connectors
from ...ingest import write_observations
from ...models import AuditLog, DataSource, Observation, Series
from ..deps import get_actor, get_db

admin_router = APIRouter(tags=["sources"])         # 只在後台


class PullRequest(BaseModel):
    start: date | None = None
    end: date | None = None


@admin_router.get("/sources")
def list_sources(session: Session = Depends(get_db)):
    connectors = list_connectors()
    counts = dict(session.execute(select(Observation.source_code, func.count())
                                  .group_by(Observation.source_code)).all())
    active_series = list(session.scalars(select(Series).where(Series.active)))
    out = []
    for s in session.scalars(select(DataSource).order_by(DataSource.priority)):
        connector = connectors.get(s.code)
        status = connector.status() if connector else None
        out.append({"code": s.code, "name": s.name, "kind": s.kind, "status": s.status, "priority": s.priority,
                    "notes": s.notes, "observations": counts.get(s.code, 0), "pullable": connector is not None,
                    "ready": status.ready if status else None, "message": status.message if status else None,
                    "series_supported": len(connector.supported(active_series)) if connector else None})
    return out


@admin_router.post("/sources/{code}/pull")
def pull(code: str, body: PullRequest, session: Session = Depends(get_db), actor: str = Depends(get_actor)):
    try:
        connector = get_connector(code)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    try:
        observations = connector.fetch(list(session.scalars(select(Series).where(Series.active))),
                                       body.start, body.end)
    except ConnectorNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    except ConnectorError as exc:      # connector 自己寫的說明，保證不含網址和密鑰
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:           # 意料之外的錯誤：不把原文給使用者（可能含請求網址），只給類型
        raise HTTPException(502, f"從 {code} 取數時發生未預期的錯誤（{type(exc).__name__}），詳情見服務器日誌") from exc
    stats = write_observations(session, observations, code, actor=actor)
    session.add(AuditLog(actor=actor, action="pull", entity="data_source", entity_id=code, detail=stats))
    return stats
