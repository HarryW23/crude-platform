"""歷史事件類比（#29）：事實和出處在 config/geo/analogs.yaml，市場反應用平台裡的數據現算。

對每個事件：
  基準日   = 事件日之前（不含當天）最後一個有價格的交易日：事件多在盤中或周末發生，當天收盤已經含了反應
  基準價   = 基準日當天的實際收盤（不是換月調整後的價格：調整後的歷史價格是倒推出來的，曲線長期貼水時
             會比當時的真實價格低很多，例如 2025-06-12 的 Brent 實際 69.36，調整後只有 46.9）
  價格反應 = 基準日之後第 1 / 5 / 21 / 63 個交易日的漲跌（%），用換月調整後的價格（同一合約比，不把換月當漲跌）
  最大波動 = 之後 21 個交易日內，相對基準的最高漲幅和最大跌幅
  月差變化 = Brent M1 − M3 在同樣幾個時點相對基準日的變化（美元/桶）：現貨是否變緊
數據不夠的格子留空，不估算；那段時間完全沒有價格的事件標為 no_data。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .config import Config
from .indicators import IndicatorEngine

STEPS = (("d1", 1, "1 天"), ("w1", 5, "1 周"), ("m1", 21, "1 個月"), ("m3", 63, "3 個月"))
PEAK_WINDOW = 21
SPREAD = "TS_BRENT_M1_M3"


def reaction(prices: pd.Series, spread: pd.Series, day, raw: pd.Series | None = None) -> dict:
    """一個事件日的市場反應。prices 用換月調整後的價格算漲跌；raw 是原始價格，只用來顯示基準日的收盤；
    spread 是月差（可以空）。"""
    out: dict[str, Any] = {"status": "no_data", "base_date": None, "base_price": None, "moves": {}, "spread": {},
           "peak": None, "trough": None}
    prices = prices.dropna().sort_index()
    before = prices[prices.index < pd.Timestamp(day)]
    if before.empty or len(prices[prices.index >= pd.Timestamp(day)]) == 0:
        return out
    base_date, base = before.index[-1], float(before.iloc[-1])
    after = prices[prices.index > base_date]
    shown = raw.get(base_date) if raw is not None else base
    out.update(status="ok", base_date=base_date.date().isoformat(),
               base_price=float(shown) if shown is not None and pd.notna(shown) else None)
    for key, n, _ in STEPS:
        out["moves"][key] = float(after.iloc[n - 1] / base - 1) if len(after) >= n else None
    window = after.iloc[:PEAK_WINDOW]
    if len(window):
        out["peak"], out["trough"] = float(window.max() / base - 1), float(window.min() / base - 1)
    spread = spread.dropna().sort_index()
    if spread.empty:                                       # 沒有月差數據（或沒配置月差指標）：只給價格反應
        if any(v is None for v in out["moves"].values()):
            out["status"] = "partial"
        return out
    s_base = spread[spread.index <= base_date]
    if len(s_base) and s_base.index[-1] == base_date:
        s_after = spread[spread.index > base_date]
        out["spread_base"] = float(s_base.iloc[-1])
        for key, n, _ in STEPS:
            if len(after) >= n and after.index[n - 1] in s_after.index:
                out["spread"][key] = float(s_after[after.index[n - 1]] - s_base.iloc[-1])
            else:
                out["spread"][key] = None
    if any(v is None for v in out["moves"].values()):
        out["status"] = "partial"
    return out


def library(engine: IndicatorEngine, cfg: Config, category: str | None = None) -> dict:
    anchor = cfg.projection.anchor if cfg.projection else "BRENT_M1"
    prices = engine.adjusted_series(anchor)[0]
    raw = engine.series_values(anchor)
    spread = engine.compute(SPREAD) if SPREAD in cfg.indicators else pd.Series(dtype=float)
    rows = []
    for a in sorted(cfg.analogs.values(), key=lambda a: a.date, reverse=True):
        if category and a.category != category:
            continue
        rows.append({"code": a.code, "name_zh": a.name_zh, "date": a.date.isoformat(), "category": a.category,
                     "region": a.region, "supply_loss_mbd": a.supply_loss_mbd, "supply_note": a.supply_note,
                     "description": a.description, "sources": a.sources, **reaction(prices, spread, a.date, raw)})
    return {"anchor": anchor, "anchor_name": cfg.name_of(anchor), "spread_name": cfg.name_of(SPREAD)
            if SPREAD in cfg.indicators else None, "steps": [{"key": k, "days": n, "name_zh": z} for k, n, z in STEPS],
            "analogs": rows}
