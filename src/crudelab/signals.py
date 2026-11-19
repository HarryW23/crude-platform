"""方向信號：把四個模組各自的狀態壓成一個 −1（利空）到 +1（利多）的分數，並保留每一步的依據。

三個數據模組（地域價差、供需、煉廠）的信號在 config/signals/ 裡聲明：
    z     = (最新值 − 回看期均值) / 回看期標準差
    score = clip(z / 2, −1, 1) × direction          —— 偏離兩個標準差即記滿分
地緣模組的信號來自事件庫：每個已確認、仍在進行的事件有一個人給的方向判斷 bias（−2 … +2），
    模組分數 = clip(Σ bias / 4, −1, 1)              —— 兩個「強利多」事件即記滿分
模組分數 = 模組內各信號按 weight 加權平均（只算有結果的信號）。
總分 = 各模組分數按模組權重（參數）加權平均（只算有結果的模組）。

這是把「現在處在歷史什麼位置」標準化的工具，不是預測。樣本不夠的信號不出分。
數據太舊的信號也不出分（status = stale）：最新值的日期離今天超過 STALE_DAYS（按輸入的頻率），
否則一條斷更了幾個月的序列會拿舊值繼續影響總分，頁面上卻看不出來。回測用同一條規則（backtest._observation_scores）。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .indicators import IndicatorEngine
from .models import Event

MODULE_NAMES = {"spread": "地域價差", "balance": "供需", "refinery": "煉廠", "positioning": "資金持倉", "geo": "地緣"}
DATA_MODULES = ("spread", "balance", "refinery", "positioning")     # 由數據算信號的模組（地緣來自事件庫）。只在這裡列
# 最新值的日期離今天超過這麼多天就不出分。比正常的發佈間隔寬鬆：周度數據 21 天 = 錯過兩次發佈
STALE_DAYS = {"daily": 10, "weekly": 21, "monthly": 70, "quarterly": 200}


def stale_days(engine_or_cfg, name: str) -> int:
    cfg = getattr(engine_or_cfg, "cfg", engine_or_cfg)
    return STALE_DAYS[cfg.frequency_of(name)]
ACTIVE_EVENT_STATUSES = ("confirmed", "ongoing")


def label(score: float | None) -> str:
    if score is None:
        return "無法判斷"
    if score >= 0.5:
        return "強偏多"
    if score >= 0.15:
        return "偏多"
    if score <= -0.5:
        return "強偏空"
    if score <= -0.15:
        return "偏空"
    return "中性"


def _clip(value: float) -> float:
    return max(-1.0, min(1.0, value))


def rolling_scores(values: pd.Series, lookback_days: int, min_points: int, direction: int,
                   detrend: str = "none") -> pd.DataFrame:
    """每個數據日期上的信號分數。signals.data_signal 也用它（取最後一行），兩邊的算法只有這一份。

    回看窗口是 [當天 − lookback_days, 當天]，兩端都含（closed="both"）。
      detrend = none    z = (值 − 窗口均值) / 窗口標準差
      detrend = linear  在窗口內對時間做最小二乘直線，z = 值偏離直線的幅度 / 殘差標準差（自由度 n − 2）
    trend_t：窗口內直線斜率的 t 值（兩種方式都算，用來判斷要不要去趨勢：|t| > 2 說明趨勢明顯）。
    """
    columns = ["value", "mean", "std", "n", "z", "score", "trend_t"]
    if values.empty:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([]))
    s = values.sort_index().astype(float)
    x = pd.Series((s.index - s.index[0]).days.astype(float), index=s.index)
    roll = lambda v: v.rolling(f"{lookback_days}D", closed="both").sum()      # noqa: E731
    n = s.rolling(f"{lookback_days}D", closed="both").count()
    sx, sy, sxx, sxy, syy = roll(x), roll(s), roll(x * x), roll(x * s), roll(s * s)
    cxx = sxx - sx * sx / n
    cxy = sxy - sx * sy / n
    cyy = (syy - sy * sy / n).clip(lower=0)
    slope = (cxy / cxx).where(cxx > 0)
    sse = (cyy - slope * cxy).clip(lower=0)
    resid_std = np.sqrt(sse / (n - 2)).where(n > 2)
    trend_t = (slope / (resid_std / np.sqrt(cxx))).where((resid_std > 0) & (cxx > 0))
    if detrend == "linear":
        center = sy / n + slope * (x - sx / n)                # 直線在當天的值
        std = resid_std
    else:
        window = s.rolling(f"{lookback_days}D", closed="both")
        center, std = window.mean(), window.std()
    usable = (n >= min_points) & (std > 0)
    z = ((s - center) / std).where(usable)
    score = (z / 2).clip(-1, 1) * direction
    return pd.DataFrame({"value": s, "mean": center, "std": std, "n": n, "z": z, "score": score,
                         "trend_t": trend_t})


def data_signal(engine: IndicatorEngine, code: str, today: pd.Timestamp) -> dict:
    s = engine.cfg.signals[code]
    out = {"code": s.code, "name_zh": s.name_zh, "module": s.module, "input": s.input,
           "input_name": engine.cfg.name_of(s.input), "unit": engine.cfg.unit_of(s.input),
           "direction": s.direction, "weight": _weight(engine, s, today), "weight_parameter": s.weight_parameter,
           "detrend": s.detrend, "notes": s.notes,
           "status": "missing", "n": 0, "date": None, "value": None, "mean": None, "std": None, "z": None,
           "score": None, "trend_t": None}
    values = engine.values(s.input)
    values = values[values.index <= today]
    if values.empty:
        return out
    last = rolling_scores(values, s.lookback_days, s.min_points, s.direction, s.detrend).iloc[-1]
    out.update(n=int(last["n"]), date=values.index[-1].date().isoformat(), value=float(values.iloc[-1]),
               trend_t=_num(last["trend_t"]))
    if (today - values.index[-1]).days > stale_days(engine, s.input):
        out["status"] = "stale"
        return out
    if pd.isna(last["z"]):
        out["status"] = "insufficient"
        return out
    out.update(status="ok", mean=float(last["mean"]), std=float(last["std"]), z=float(last["z"]),
               score=float(last["score"]))
    return out


def _num(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _weight(engine: IndicatorEngine, s, today: pd.Timestamp) -> float:
    """信號權重：參數 W_{code} 當天生效的版本；參數還沒有版本時用配置裡的初始值。"""
    value = engine.value_at(s.weight_parameter, today.date()) if s.weight_parameter in engine.cfg.parameters else None
    return s.weight if value is None else float(value)


def active_events(session: Session) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.draft.is_(False),
                                                    Event.status.in_(ACTIVE_EVENT_STATUSES)).order_by(Event.id)))


def compute(session: Session, engine: IndicatorEngine) -> dict:
    cfg = engine.cfg
    today = pd.Timestamp(engine.as_of.date() if engine.as_of else date.today())
    weights = cfg.projection.module_weights if cfg.projection else {}
    modules = []
    for module in DATA_MODULES:
        items = [data_signal(engine, code, today) for code, s in cfg.signals.items() if s.module == module]
        ok = [i for i in items if i["status"] == "ok"]
        total = sum(i["weight"] for i in ok)
        score = sum(i["score"] * i["weight"] for i in ok) / total if total else None
        modules.append({"module": module, "name_zh": MODULE_NAMES[module], "score": score, "label": label(score),
                        "signals_ok": len(ok), "signals_total": len(items), "signals": items})

    events = active_events(session)
    geo_score = _clip(sum(e.bias for e in events) / 4) if events else None
    modules.append({"module": "geo", "name_zh": MODULE_NAMES["geo"], "score": geo_score, "label": label(geo_score),
                    "signals_ok": len(events), "signals_total": len(events),
                    "signals": [{"code": f"EVENT_{e.id}", "name_zh": e.title, "module": "geo", "status": "ok",
                                 "bias": e.bias, "score": e.bias / 2, "weight": 1.0, "date": str(e.started_on or "")}
                                for e in events]})

    weighted, weight_sum = 0.0, 0.0
    for m in modules:
        key = weights.get(m["module"])
        weight = engine.value_at(key, today.date()) if key else 1.0
        m["weight"], m["weight_parameter"] = weight, key
        if m["score"] is not None and weight:
            weighted += m["score"] * weight
            weight_sum += weight
    overall = weighted / weight_sum if weight_sum else None
    return {"overall": {"score": overall, "label": label(overall),
                        "modules_ok": sum(m["score"] is not None for m in modules), "modules_total": len(modules)},
            "modules": modules}
