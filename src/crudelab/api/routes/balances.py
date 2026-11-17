"""供需平衡表：列表、按期間展開的表（基準與情境對照）。"""
from __future__ import annotations

from datetime import date

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from ...balance import BalanceSpec
from ...indicators import IndicatorEngine
from ..deps import AppState, get_base_engine, get_engine, get_state

router = APIRouter(tags=["balances"])


def _info(spec: BalanceSpec, engine: IndicatorEngine) -> dict:
    d = spec.definition
    return {"code": d.code, "name_zh": d.name_zh, "unit": d.unit, "frequency": d.frequency, "notes": d.notes,
            "balance": engine.summary(spec.balance)}


@router.get("/balances")
def list_balances(state: AppState = Depends(get_state), engine: IndicatorEngine = Depends(get_engine)):
    return [_info(spec, engine) for spec in state.cfg.balances.values()]


@router.get("/balances/{code}")
def balance_table(code: str, past: int = Query(9, ge=1, le=120), future: int = Query(9, ge=0, le=120),
                  state: AppState = Depends(get_state), engine: IndicatorEngine = Depends(get_engine),
                  base: IndicatorEngine = Depends(get_base_engine)):
    """按期間展開的平衡表：最近 past 個已發生的期間 + 之後 future 個預測期間。

    values 是當前視圖（套了情境就是情境值），base_values 是不套情境的值；兩者不同的格子就是被調整過的。
    某一項在某個期間沒有數據時值為 null，合計和平衡在那個期間也為 null。
    """
    if code not in state.cfg.balances:
        raise HTTPException(404, f"沒有名為 {code} 的平衡表")
    spec = state.cfg.balances[code]
    index = pd.DatetimeIndex([])
    for row in spec.rows:
        index = index.union(engine.compute(row.indicator).index).union(base.compute(row.indicator).index)
    today = pd.Timestamp(date.today())
    periods = index[index <= today][-past:].append(index[index > today][:future])

    def cells(source: IndicatorEngine, indicator: str) -> list[float | None]:
        values = source.compute(indicator).reindex(periods)
        return [None if pd.isna(v) else float(v) for v in values]

    return {**_info(spec, engine),
            "periods": [{"date": ts.date().isoformat(), "forecast": bool(ts > today)} for ts in periods],
            "rows": [{"label": row.label, "kind": row.kind, "indicator": row.indicator, "adjustable": row.adjustable,
                      "values": cells(engine, row.indicator), "base_values": cells(base, row.indicator)}
                     for row in spec.rows]}
