"""方向信號：把四個模組各自的狀態壓成一個 −1（利空）到 +1（利多）的分數，並保留每一步的依據。

三個數據模組（地域價差、供需、煉廠）的信號在 config/signals/ 裡聲明：
    z     = (最新值 − 回看期均值) / 回看期標準差
    score = clip(z / 2, −1, 1) × direction          —— 偏離兩個標準差即記滿分
地緣模組的信號來自事件庫：每個已確認、仍在進行的事件有一個人給的方向判斷 bias（−2 … +2），
    模組分數 = clip(Σ bias / 4, −1, 1)              —— 兩個「強利多」事件即記滿分
模組分數 = 模組內各信號按 weight 加權平均（只算有結果的信號）。
總分 = 各模組分數按模組權重（參數）加權平均（只算有結果的模組）。

這是把「現在處在歷史什麼位置」標準化的工具，不是預測。樣本不夠的信號不出分。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .indicators import IndicatorEngine
from .models import Event

MODULE_NAMES = {"spread": "地域價差", "balance": "供需", "refinery": "煉廠", "geo": "地緣"}
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


def data_signal(engine: IndicatorEngine, code: str, today: pd.Timestamp) -> dict:
    s = engine.cfg.signals[code]
    out = {"code": s.code, "name_zh": s.name_zh, "module": s.module, "input": s.input,
           "input_name": engine.cfg.name_of(s.input), "unit": engine.cfg.unit_of(s.input),
           "direction": s.direction, "weight": s.weight, "notes": s.notes,
           "status": "missing", "n": 0, "date": None, "value": None, "mean": None, "std": None, "z": None,
           "score": None}
    values = engine.values(s.input)
    values = values[values.index <= today]
    if values.empty:
        return out
    window = values[values.index >= values.index[-1] - pd.Timedelta(days=s.lookback_days)]
    out.update(n=len(window), date=values.index[-1].date().isoformat(), value=float(values.iloc[-1]))
    std = float(window.std()) if len(window) > 1 else 0.0
    if len(window) < s.min_points or std == 0:
        out["status"] = "insufficient"
        return out
    z = (float(values.iloc[-1]) - float(window.mean())) / std
    out.update(status="ok", mean=float(window.mean()), std=std, z=z, score=_clip(z / 2) * s.direction)
    return out


def active_events(session: Session) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.draft.is_(False),
                                                    Event.status.in_(ACTIVE_EVENT_STATUSES)).order_by(Event.id)))


def compute(session: Session, engine: IndicatorEngine) -> dict:
    cfg = engine.cfg
    today = pd.Timestamp(engine.as_of.date() if engine.as_of else date.today())
    weights = cfg.projection.module_weights if cfg.projection else {}
    modules = []
    for module in ("spread", "balance", "refinery"):
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
