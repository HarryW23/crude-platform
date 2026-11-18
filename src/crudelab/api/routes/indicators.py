"""指標：列表、數據、計算追溯。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from ...indicators import IndicatorEngine
from ..deps import get_engine

router = APIRouter(tags=["indicators"])


def _require(engine: IndicatorEngine, code: str) -> None:
    if code not in engine.cfg.indicators:
        raise HTTPException(404, f"沒有名為 {code} 的指標")


@router.get("/indicators")
def list_indicators(module: str | None = None, include_hidden: bool = False,
                    engine: IndicatorEngine = Depends(get_engine)):
    return [engine.summary(code) for code, ind in engine.cfg.indicators.items()
            if (include_hidden or not ind.hidden) and (module is None or ind.module == module)]


@router.get("/indicators/{code}/data")
def indicator_data(code: str, engine: IndicatorEngine = Depends(get_engine)):
    _require(engine, code)
    return [{"date": ts.date(), "value": float(v)} for ts, v in engine.compute(code).items()]


@router.get("/indicators/{code}/rolls")
def indicator_rolls(code: str, engine: IndicatorEngine = Depends(get_engine)):
    """指標依賴的期貨曲線在它的數據區間裡的換月日。走勢圖用來標出換月。"""
    _require(engine, code)
    from ...contracts import roll_dates
    values = engine.compute(code)
    if values.empty:
        return []
    start, end = values.index[0].date(), values.index[-1].date()
    return [{"date": d, "curve": c, "curve_name": engine.cfg.curves[c].name_zh}
            for c in engine.rolled_curves(code) for d in roll_dates(engine.cfg.curves[c].roll, start, end)]


@router.get("/indicators/{code}/trace")
def indicator_trace(code: str, on: date | None = None, engine: IndicatorEngine = Depends(get_engine)):
    _require(engine, code)
    if on is None:
        values = engine.compute(code)
        if values.empty:
            raise HTTPException(400, "這個指標目前沒有任何結果可以追溯")
        on = values.index[-1].date()
    return {"date": on, "tree": engine.trace(code, on)}
