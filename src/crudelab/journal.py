"""觀點記錄與覆盤：記下自己的判斷，到期後和實際走勢對比，長期統計哪些情況下判斷得準。

記錄（create）
  期限、方向（偏多 / 中性 / 偏空）、可選的目標區間、把握（方向判斷對的機率 50%–100%）、理由的因子標籤和文字。
  同時自動記下：起點價格（推演的錨定價格，當天或之前最近的值）、當時的日波動率、當時系統信號的快照。
  只能記今天的判斷；記下後內容不能改，只能在到期前撤銷（要寫原因），撤銷的不參與統計。
  到期的不能撤銷，否則可以把判錯的撤掉。這是覆盤可信的前提。

評分（evaluate，每次現算）
  到期日 = 判斷日 + 期限的日曆天數。到期前顯示「進行中」和目前的漲跌；到期後：
  - 方向：用換月調整後的價格算收益（避免換月跳動造成誤判）。|收益| 小於 0.5 × 當時日波動率 × √交易日數
    算「沒怎麼動」（中性），大於算漲或跌。判斷的方向和實際一致就算對。
  - 區間：到期日（或之前最近一個交易日）的原始價格是否落在目標區間裡。
  - 把握：Brier 分數 =（把握 − 是否判對）²，越小越好；說 70% 把握的判斷應該大約七成對（校準）。
  - 系統：同一時刻系統綜合信號給的方向（分數 ≥ 0.15 偏多、≤ −0.15 偏空、之間中性），用同樣的規則評分。
  - 基準：同一批判斷裡實際上漲的比例，即「一直看多」能對多少。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import projection, signals
from .config import Config
from .indicators import IndicatorEngine
from .models import AuditLog, MarketView, utcnow

REASONS = {"inventory": "庫存", "supply": "OPEC+ / 供應", "positioning": "資金持倉", "geo": "地緣",
           "curve": "期限結構 / 價差", "refinery": "煉廠 / 需求", "macro": "宏觀 / 美元", "technical": "技術面",
           "other": "其他"}
DIRECTIONS = {1: "偏多", 0: "中性", -1: "偏空"}
NEUTRAL_K = 0.5                 # |收益| < NEUTRAL_K × 日波動率 × √交易日數 算「沒怎麼動」
FALLBACK_VOL = 0.02             # 當時算不出波動率時用的日波動率
SYSTEM_BAND = 0.15              # 和 signals.label 的「中性」範圍一致
END_TOLERANCE_DAYS = 5          # 到期日沒有價格時，最多往前找幾天
CALIBRATION = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.0001))


class JournalError(ValueError):
    pass


def _horizon(cfg: Config, code: str):
    for h in projection.horizons_of(cfg):
        if h.code == code:
            return h
    raise JournalError(f"沒有期限 {code}")


def _anchor(cfg: Config) -> str:
    if cfg.projection is None:
        raise JournalError("config/projection.yaml 不存在，不知道看哪個價格")
    return cfg.projection.anchor


def create(session: Session, cfg: Config, *, horizon: str, direction: int, low: float | None = None,
           high: float | None = None, confidence: float | None = None, reasons: list[str] | None = None,
           rationale: str | None = None, actor: str = "local", today: date | None = None) -> MarketView:
    h = _horizon(cfg, horizon)
    if direction not in DIRECTIONS:
        raise JournalError("方向必須是偏多、中性或偏空")
    if (low is None) != (high is None):
        raise JournalError("目標區間要同時填下限和上限，或都不填")
    if low is not None and high is not None and not (math.isfinite(low) and math.isfinite(high) and 0 < low < high):
        raise JournalError("目標區間的下限必須小於上限，而且都要大於 0")
    if confidence is not None and not 0.5 <= confidence <= 1:
        raise JournalError("把握必須在 50% 到 100% 之間（低於 50% 等於判斷相反的方向）")
    reasons = list(dict.fromkeys(reasons or []))
    unknown = [r for r in reasons if r not in REASONS]
    if unknown:
        raise JournalError(f"不認識的理由標籤：{'、'.join(unknown)}")
    if rationale and len(rationale) > 4000:
        raise JournalError("理由太長（最多 4000 字）")

    made_on = today or date.today()
    anchor = _anchor(cfg)
    engine = IndicatorEngine(session, cfg)
    raw = engine.series_values(anchor)
    raw = raw[raw.index <= pd.Timestamp(made_on)]
    if raw.empty:
        raise JournalError(f"{cfg.name_of(anchor)} 在 {made_on} 之前沒有價格數據，無法記錄起點")
    adjusted = engine.adjusted_series(anchor)[0]
    adjusted = adjusted[adjusted.index <= pd.Timestamp(made_on)]
    settings = cfg.projection
    assert settings is not None                  # _anchor() 已經檢查過 projection.yaml 存在
    vol = projection._volatility(adjusted, settings.volatility_lookback, settings.volatility_min_points)
    system = signals.compute(session, engine)
    snapshot = {"overall": system["overall"]["score"], "label": system["overall"]["label"],
                "modules": {m["module"]: m["score"] for m in system["modules"]}}
    row = MarketView(made_on=made_on, horizon=h.code, target_date=made_on + timedelta(days=h.calendar_days),
                     series_code=anchor, direction=direction, low=low, high=high, confidence=confidence,
                     reasons=reasons, rationale=(rationale or "").strip() or None,
                     start_price=float(raw.iloc[-1]), start_date=raw.index[-1].date(), daily_vol=vol,
                     system=snapshot, created_by=actor)
    session.add(row)
    session.flush()
    session.add(AuditLog(actor=actor, action="view_create", entity="market_view", entity_id=str(row.id),
                         detail={"horizon": h.code, "direction": direction}))
    return row


def cancel(session: Session, view_id: int, reason: str, actor: str = "local", today: date | None = None) -> MarketView:
    """撤銷一條還沒到期的觀點。到期的不能撤銷：否則可以把判錯的撤掉，統計就不可信了。"""
    row = session.get(MarketView, view_id)
    if row is None:
        raise JournalError(f"沒有編號為 {view_id} 的觀點")
    if row.cancelled_at is not None:
        raise JournalError("這條觀點已經撤銷過")
    if (today or date.today()) >= row.target_date:
        raise JournalError("已經到期的觀點不能撤銷：結果要計入統計")
    if not reason or not reason.strip():
        raise JournalError("撤銷要寫原因（例如記錯了）")
    row.cancelled_at, row.cancel_reason = utcnow(), reason.strip()[:500]
    session.add(AuditLog(actor=actor, action="view_cancel", entity="market_view", entity_id=str(row.id),
                         detail={"reason": row.cancel_reason}))
    return row


# ------------------------------ 評分 ------------------------------

def _move(ret: float, band: float) -> int:
    return 1 if ret > band else -1 if ret < -band else 0


def _system_direction(snapshot: dict | None) -> int | None:
    score = (snapshot or {}).get("overall")
    if score is None:
        return None
    return 1 if score >= SYSTEM_BAND else -1 if score <= -SYSTEM_BAND else 0


def _value_at(series: pd.Series, day: date) -> tuple[float | None, date | None]:
    end = pd.Timestamp(day)
    past = series[(series.index <= end) & (series.index >= end - pd.Timedelta(days=END_TOLERANCE_DAYS))]
    return (float(past.iloc[-1]), past.index[-1].date()) if len(past) else (None, None)


def evaluate(cfg: Config, view: MarketView, raw: pd.Series, adjusted: pd.Series, today: date) -> dict:
    h = next((x for x in projection.horizons_of(cfg) if x.code == view.horizon), None)
    trading_days = h.trading_days if h else max(1, round((view.target_date - view.made_on).days * 5 / 7))
    out = {"id": view.id, "made_on": view.made_on.isoformat(), "horizon": view.horizon,
           "horizon_name": h.name_zh if h else view.horizon, "target_date": view.target_date.isoformat(),
           "series_code": view.series_code, "series_name": cfg.name_of(view.series_code) if cfg.has(view.series_code)
           else view.series_code, "direction": view.direction, "direction_name": DIRECTIONS[view.direction],
           "low": view.low, "high": view.high, "confidence": view.confidence, "reasons": view.reasons,
           "reason_names": [REASONS.get(r, r) for r in view.reasons], "rationale": view.rationale,
           "start_price": view.start_price, "start_date": view.start_date.isoformat(), "created_by": view.created_by,
           "system": view.system, "system_direction": _system_direction(view.system),
           "cancelled": view.cancelled_at is not None, "cancel_reason": view.cancel_reason,
           "status": "cancelled" if view.cancelled_at else "open", "band": None, "end_price": None, "end_date": None,
           "return": None, "actual": None, "hit": None, "range_hit": None, "system_hit": None, "brier": None}
    band = NEUTRAL_K * (view.daily_vol or FALLBACK_VOL) * math.sqrt(trading_days)
    out["band"] = band
    start_adj, _ = _value_at(adjusted, view.start_date)
    if view.cancelled_at is not None:
        return out
    if view.target_date > today:                             # 進行中：目前的漲跌
        now_adj, now_day = _value_at(adjusted, today)
        if start_adj and now_adj and now_day:
            out["return"], out["end_date"] = now_adj / start_adj - 1, now_day.isoformat()
            out["end_price"] = _value_at(raw, today)[0]
        return out
    end_raw, end_day = _value_at(raw, view.target_date)
    end_adj, _ = _value_at(adjusted, view.target_date)
    if end_raw is None or end_adj is None or end_day is None or not start_adj:
        out["status"] = "waiting"                          # 到期了，但數據還沒到
        return out
    ret = end_adj / start_adj - 1
    actual = _move(ret, band)
    out.update(status="scored", end_price=end_raw, end_date=end_day.isoformat(), **{"return": ret}, actual=actual,
               hit=view.direction == actual)
    if view.low is not None and view.high is not None:
        out["range_hit"] = view.low <= end_raw <= view.high
    if out["system_direction"] is not None:
        out["system_hit"] = out["system_direction"] == actual
    if view.confidence is not None:
        out["brier"] = (view.confidence - (1.0 if out["hit"] else 0.0)) ** 2
    return out


def _rate(rows: list[dict], key: str) -> dict:
    values = [r[key] for r in rows if r[key] is not None]
    return {"n": len(values), "rate": sum(values) / len(values) if values else None}


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["status"] == "scored"]
    out = {"scored": len(scored), "open": sum(r["status"] == "open" for r in rows),
           "waiting": sum(r["status"] == "waiting" for r in rows),
           "hit": _rate(scored, "hit"), "range": _rate(scored, "range_hit"), "system": _rate(scored, "system_hit"),
           "always_up": {"n": len(scored),
                         "rate": sum(r["actual"] == 1 for r in scored) / len(scored) if scored else None},
           "brier": None, "by_horizon": {}, "by_reason": {}, "calibration": []}
    briers = [r["brier"] for r in scored if r["brier"] is not None]
    out["brier"] = {"n": len(briers), "mean": sum(briers) / len(briers) if briers else None}
    for code in dict.fromkeys(r["horizon"] for r in scored):
        out["by_horizon"][code] = _rate([r for r in scored if r["horizon"] == code], "hit")
    for reason in REASONS:
        tagged = [r for r in scored if reason in r["reasons"]]
        if tagged:
            out["by_reason"][reason] = {**_rate(tagged, "hit"), "name_zh": REASONS[reason]}
    for lo, hi in CALIBRATION:
        bucket = [r for r in scored if r["confidence"] is not None and lo <= r["confidence"] < hi]
        if bucket:
            out["calibration"].append({"from": lo, "to": min(hi, 1.0), "n": len(bucket),
                                       "said": sum(r["confidence"] for r in bucket) / len(bucket),
                                       "actual": sum(r["hit"] for r in bucket) / len(bucket)})
    return out


def overview(session: Session, cfg: Config, today: date | None = None) -> dict:
    today = today or date.today()
    views = list(session.scalars(select(MarketView).order_by(MarketView.id.desc())))
    engine = IndicatorEngine(session, cfg)
    cache: dict[str, tuple[pd.Series, pd.Series]] = {}
    rows = []
    for v in views:
        if v.series_code not in cache:
            known = cfg.has(v.series_code)
            raw = engine.series_values(v.series_code) if known else pd.Series(dtype=float)
            adjusted = engine.adjusted_series(v.series_code)[0] if known else raw
            cache[v.series_code] = (raw, adjusted)
        rows.append(evaluate(cfg, v, *cache[v.series_code], today))
    return {"reasons": REASONS, "directions": DIRECTIONS,
            "horizons": [{"code": h.code, "name_zh": h.name_zh, "calendar_days": h.calendar_days}
                         for h in projection.horizons_of(cfg)],
            "anchor": _anchor(cfg) if cfg.projection else None,
            "anchor_name": cfg.name_of(cfg.projection.anchor) if cfg.projection else None,
            "views": rows, "summary": summarize(rows)}
