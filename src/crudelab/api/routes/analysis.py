"""分析視圖：季節性區間、關係模型。都是對序列或指標的統計變換，底層數字仍來自指標引擎。"""
from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from ... import analytics
from ...indicators import IndicatorEngine
from ..deps import AppState, get_engine, get_state

router = APIRouter(tags=["analysis"])


def _values(engine: IndicatorEngine, name: str) -> pd.Series:
    if name not in engine.cfg.series and name not in engine.cfg.indicators:
        raise HTTPException(404, f"{name} 既不是序列也不是指標")
    return engine.values(name)


def _name(engine: IndicatorEngine, name: str) -> str:
    return engine.cfg.indicators[name].name_zh if name in engine.cfg.indicators else engine.cfg.series[name].name_zh


@router.get("/curves")
def curves(engine: IndicatorEngine = Depends(get_engine)):
    """每條期貨曲線今天、1 周前、1 個月前的形狀，以及近遠月結構。"""
    from ...curves import all_snapshots
    return all_snapshots(engine, engine.cfg)


@router.get("/seasonal/{name}")
def seasonal(name: str, years: int = Query(5, ge=1, le=10), window_days: int = Query(365, ge=30, le=1830),
             engine: IndicatorEngine = Depends(get_engine)):
    """最近 window_days 天的值，以及每個日期對應的前 years 年同期的 min / max / avg。

    某個日期湊不齊 years 年的同期值時，那個日期的 min / max / avg 為 null。
    """
    values = _values(engine, name)
    if values.empty:
        return {"name": name, "name_zh": _name(engine, name), "unit": engine.cfg.unit_of(name), "years": years,
                "points": []}
    stats = analytics.seasonal_stats(values, years)
    recent = values.index >= values.index[-1] - pd.Timedelta(days=window_days)
    frame = pd.concat([values.rename("value"), stats], axis=1)[recent]
    clean = lambda v: None if pd.isna(v) else float(v)  # noqa: E731
    return {"name": name, "name_zh": _name(engine, name), "unit": engine.cfg.unit_of(name), "years": years,
            "points": [{"date": ts.date().isoformat(), **{k: clean(r[k]) for k in ("value", "min", "max", "avg")}}
                       for ts, r in frame.iterrows()]}


def _relation(engine: IndicatorEngine, code: str, with_points: bool) -> dict:
    r = engine.cfg.relations[code]
    fit = analytics.relation_fit(_values(engine, r.y), _values(engine, r.x), r.lookback_days, r.min_points,
                                 r.method, r.shape, r.lag_days)
    if not with_points:
        fit.pop("points")
    return {"code": r.code, "name_zh": r.name_zh, "notes": r.notes, "lookback_days": r.lookback_days,
            "min_points": r.min_points,
            "y": {"code": r.y, "name_zh": _name(engine, r.y), "unit": engine.cfg.unit_of(r.y)},
            "x": {"code": r.x, "name_zh": _name(engine, r.x), "unit": engine.cfg.unit_of(r.x)}, **fit}


@router.get("/relations")
def list_relations(state: AppState = Depends(get_state), engine: IndicatorEngine = Depends(get_engine)):
    return [_relation(engine, code, with_points=False) for code in state.cfg.relations]


@router.get("/relations/{code}")
def relation_detail(code: str, state: AppState = Depends(get_state), engine: IndicatorEngine = Depends(get_engine)):
    if code not in state.cfg.relations:
        raise HTTPException(404, f"沒有名為 {code} 的關係模型")
    return _relation(engine, code, with_points=True)
