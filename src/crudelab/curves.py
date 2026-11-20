"""期貨曲線：把一條曲線上各個月份的價格排在一起，並和過去某天的曲線對比。

曲線來自 config/master/series/ 裡的 curves 模板（每條曲線展開成 M1 … M12 的連續合約序列）。
對比的「1 周前」「1 個月前」按 M1 自己的交易日往回數（5、21 個交易日），不按日曆天，避開周末和假期。
某個月份在某天沒有數據時，那一點留空，不插值。

每個點標出它當天是哪個具體合約（按 roll 規則推算，見 contracts.py）。對比日以來換過月的話，
同一個位置（例如 M1）在兩天對應的是不同合約，snapshot 裡的 rolls_since 標出換了幾次。
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from . import contracts
from .config import Config
from .indicators import IndicatorEngine

LOOKBACKS = ((0, "今天"), (5, "1 周前"), (21, "1 個月前"))
FLAT_BAND = 0.005             # 近遠月價差的絕對值小於 M1 價格的 0.5% 算「平坦」
TOLERANCE = pd.Timedelta(days=3)   # 某個月份當天沒有值時，最多往前找幾天


def months(cfg: Config, code: str) -> list[tuple[int, str]]:
    curve = cfg.curves[code]
    return [(n, f"{curve.code}_M{n}") for n in sorted(curve.months)]


def _value_on(values: pd.Series, day: pd.Timestamp) -> float | None:
    past = values[(values.index <= day) & (values.index >= day - TOLERANCE)]
    return float(past.iloc[-1]) if len(past) else None


def shape(points: list[dict]) -> dict:
    """一條曲線的形態：近遠月價差、結構（近強遠弱 / 近弱遠強 / 平坦）、曲率。"""
    have = [p for p in points if p["value"] is not None]
    out: dict[str, Any] = {"front": None, "back": None, "back_month": None, "spread": None, "structure": None,
                           "fly": None}
    if len(have) < 2:
        return out
    front, back = have[0], have[-1]
    spread = front["value"] - back["value"]
    out.update(front=front["value"], back=back["value"], back_month=back["month"], spread=spread)
    if abs(spread) <= FLAT_BAND * abs(front["value"]):
        out["structure"] = "平坦"
    else:
        out["structure"] = "近強遠弱（backwardation）" if spread > 0 else "近弱遠強（contango）"
    by_month = {p["month"]: p["value"] for p in have}
    if all(m in by_month for m in (1, 6, 12)):
        out["fly"] = by_month[1] - 2 * by_month[6] + by_month[12]
    return out


def snapshot(engine: IndicatorEngine, cfg: Config, code: str) -> dict:
    curve = cfg.curves[code]
    columns = months(cfg, code)
    values = {series: engine.series_values(series).dropna() for _, series in columns}
    today = pd.Timestamp(engine.as_of.date() if engine.as_of else date.today())
    front = values[columns[0][1]]
    front = front[front.index <= today]
    out: dict[str, Any] = {"code": code, "name_zh": curve.name_zh, "unit": curve.unit,
                           "months": [n for n, _ in columns],
           "snapshots": [], "status": "ok", "roll_rule": curve.roll.note if curve.roll else None,
           "roll_verified": bool(curve.roll and curve.roll.verified)}
    if front.empty:
        out["status"] = "missing_data"
        return out
    for back, label in LOOKBACKS:
        if len(front) <= back:
            continue
        day = front.index[-1 - back]
        labels = {c["month"]: c["contract"] for c in contracts.contracts_on(cfg, code, day.date())}
        points = [{"month": n, "series": series, "contract": labels[n], "value": _value_on(values[series], day)}
                  for n, series in columns]
        rolls = (contracts.rolls_between(curve.roll, day.date(), front.index[-1].date())
                 if curve.roll is not None else None)
        out["snapshots"].append({"label": label, "date": day.date().isoformat(), "points": points,
                                 "shape": shape(points), "rolls_since": rolls})
    latest = out["snapshots"][0]["points"]
    out["months_with_data"] = sum(p["value"] is not None for p in latest)
    return out


def all_snapshots(engine: IndicatorEngine, cfg: Config) -> list[dict]:
    return [snapshot(engine, cfg, code) for code in cfg.curves]
