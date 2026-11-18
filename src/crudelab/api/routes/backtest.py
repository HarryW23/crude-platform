"""回測（前台）：建立、查看。回測在後台線程裡跑，頁面輪詢。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import backtest as service
from ...models import BacktestRun
from ..deps import AppState, get_actor, get_db, get_state

router = APIRouter(tags=["backtest"])


class NewBacktest(BaseModel):
    mode: str = "observation"
    start: date | None = None
    end: date | None = None
    step: int | None = None


def _head(row: BacktestRun) -> dict:
    head = {"id": row.id, "status": row.status, "mode": row.mode, "mode_name": service.MODES.get(row.mode, row.mode),
            "start": row.start_date, "end": row.end_date, "step": row.step, "created_by": row.created_by,
            "created_at": row.created_at, "finished_at": row.finished_at, "message": row.message, "summary": None}
    if row.results:
        by = row.results["overall"]["by_horizon"]
        month = by.get("M1") or next(iter(by.values()))
        head["summary"] = {"start": row.results["start"], "end": row.results["end"], "dates": row.results["dates"],
                           "hit_rate": month["hit_rate"], "ic": month["ic"], "status": month["status"]}
    return head


@router.get("/backtests")
def list_backtests(session: Session = Depends(get_db)):
    service.mark_stale(session)
    return [_head(r) for r in session.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(50))]


@router.post("/backtests")
def create_backtest(body: NewBacktest, session: Session = Depends(get_db), state: AppState = Depends(get_state),
                    actor: str = Depends(get_actor)):
    if body.step is not None and not 1 <= body.step <= 60:
        raise HTTPException(400, "評估間隔必須在 1 到 60 個交易日之間")
    if body.start and body.end and body.start >= body.end:
        raise HTTPException(400, "開始日期必須早於結束日期")
    try:
        row = service.create(session, service.Options(body.mode, body.start, body.end, body.step), actor)
    except service.BacktestError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    service.start_in_background(state.cfg, row.id)
    return {"id": row.id, "status": row.status}


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int, session: Session = Depends(get_db)):
    row = session.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(404, f"沒有編號為 {run_id} 的回測")
    return {**_head(row), "results": row.results}
