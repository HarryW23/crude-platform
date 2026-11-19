"""價格推演：把若干個情境分支各自折成一個價格區間，再按機率加權。

對每個分支、每個期限（1 周 / 1 個月 / 3 個月）：
    基本面回歸 = 推演選了公允價值模型時，預計在期限內消退的偏離（見 fair_value.py）；沒選則為 0。各分支相同
    平衡變化   = 期限內各期「情境下的平衡 − 基準平衡」的平均          （來自平衡表和情境調整；只用來展示）
    庫存變化   = Σ 各期的平衡變化 × 這一期落在 [今天, 期限末] 內的天數  （過剩持續越久，累庫越多）
    供需效應   = −敏感度 × 庫存變化                                    （敏感度是參數：庫存每多累積 1 單位，價格跌多少）
    中樞       = 當前價格 + 基本面回歸 + 供需效應 + 分支的直接價格調整（直接調整 = 人給的判斷，如風險溢價）
    區間       = 中樞 ± 當前價格 × 日波動率 × √期限交易日數            （一倍標準差，來自歷史已實現波動）
    機率加權價 = Σ 機率 × 中樞                                          （各分支機率之和必須等於 1）

每一項都能指回來源：當前價格和波動率來自行情，平衡變化來自情境調整，敏感度和機率是人設的。
這是把假設攤開來算的工具，不是黑盒預測：
  - 不選公允價值模型時沒有「基準漂移」，基準分支的中樞就是當前價格
  - 敏感度是線性的：價格對累積庫存變化的反應。同樣的過剩，持續 3 個月的累庫是 1 周的十幾倍，影響也大得多。
    可以用 sensitivity.py 從歷史估計（庫存月度變化對價格月度變化的回歸），但採用與否由使用者決定
  - 區間只反映歷史波動的大小，不是置信區間的保證
缺任何一項輸入，對應的結果就留空並說明原因。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import events, fair_value, scenarios
from .config import Config
from .indicators import IndicatorEngine, Scenario
from .models import Projection, ProjectionCase, utcnow
from .schema import ProjectionSettings

PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 31, "quarterly": 92}


class ProjectionError(ValueError):
    pass


def horizons_of(cfg: Config) -> list:
    from .backtest import DEFAULT_HORIZONS
    return list(cfg.projection.horizons) if cfg.projection else DEFAULT_HORIZONS


# ------------------------------ 設定的增刪 ------------------------------

def _check_drift_model(cfg: Config | None, drift_model: str | None) -> None:
    if drift_model and (cfg is None or drift_model not in cfg.fair_values):
        raise ProjectionError(f"沒有名為 {drift_model} 的公允價值模型")


def create(session: Session, name_zh: str, actor: str = "local", drift_model: str | None = None,
           cfg: Config | None = None) -> Projection:
    _check_drift_model(cfg, drift_model)
    row = Projection(name_zh=name_zh.strip() or "未命名推演", created_by=actor, drift_model=drift_model or None)
    session.add(row)
    session.flush()
    session.add(ProjectionCase(projection_id=row.id, label="基準", scenario_code=None, probability=1.0,
                               note="只看數據，不加任何調整", created_by=actor))
    session.flush()
    return row


def require(session: Session, projection_id: int) -> Projection:
    row = session.get(Projection, projection_id)
    if row is None or row.archived_at is not None:
        raise ProjectionError(f"沒有編號為 {projection_id} 的推演")
    return row


def set_drift_model(session: Session, cfg: Config, projection_id: int, drift_model: str | None) -> Projection:
    row = require(session, projection_id)
    _check_drift_model(cfg, drift_model)
    row.drift_model = drift_model or None
    return row


def cases(session: Session, projection_id: int) -> list[ProjectionCase]:
    return list(session.scalars(select(ProjectionCase).where(
        ProjectionCase.projection_id == projection_id, ProjectionCase.removed_at.is_(None))
        .order_by(ProjectionCase.id)))


def _check(probability: float, price_adjustment: float) -> None:
    if not (isinstance(probability, (int, float)) and 0 <= probability <= 1):
        raise ProjectionError("機率必須在 0 到 1 之間")
    if not (isinstance(price_adjustment, (int, float)) and math.isfinite(price_adjustment)):
        raise ProjectionError("價格調整必須是有效數字")


def add_case(session: Session, cfg: Config, projection_id: int, label: str, scenario_code: str | None,
             probability: float, price_adjustment: float = 0.0, note: str | None = None,
             actor: str = "local") -> ProjectionCase:
    require(session, projection_id)
    _check(probability, price_adjustment)
    if scenario_code:
        try:
            scenarios.load(session, cfg, scenario_code)
        except scenarios.ScenarioError as exc:
            raise ProjectionError(str(exc)) from exc
    row = ProjectionCase(projection_id=projection_id, label=label.strip() or "未命名分支",
                         scenario_code=scenario_code or None, probability=float(probability),
                         price_adjustment=float(price_adjustment), note=note, created_by=actor)
    session.add(row)
    session.flush()
    return row


def update_case(session: Session, case_id: int, probability: float | None = None,
                price_adjustment: float | None = None, note: str | None = None) -> ProjectionCase:
    row = session.get(ProjectionCase, case_id)
    if row is None or row.removed_at is not None:
        raise ProjectionError(f"沒有編號為 {case_id} 的分支")
    _check(row.probability if probability is None else probability,
           row.price_adjustment if price_adjustment is None else price_adjustment)
    if probability is not None:
        row.probability = float(probability)
    if price_adjustment is not None:
        row.price_adjustment = float(price_adjustment)
    if note is not None:
        row.note = note
    return row


def remove_case(session: Session, case_id: int) -> None:
    row = session.get(ProjectionCase, case_id)
    if row is None or row.removed_at is not None:
        raise ProjectionError(f"沒有編號為 {case_id} 的分支")
    row.removed_at = utcnow()


# ------------------------------ 從地緣事件同步分支 ------------------------------

def sync_events(session: Session, cfg: Config, projection_id: int, actor: str = "local") -> dict:
    """把「有機率的、仍在進行或可能發生的」地緣事件變成推演分支。

    每個事件一個分支：機率 = 事件的機率，情境 = 事件套用預案生成的情境，價格調整 = 事件的風險溢價。
    剩下的機率歸基準分支（沒有情境、不是事件生成的那個；沒有就新建一個）。
    這是「各事件互斥」的近似：事件之間同時發生的組合不單獨列出，機率都不大時誤差很小；
    機率之和超過 100% 時說明這個近似不成立，拒絕同步，請先調低。
    已有的事件分支先移除再按事件的現狀重建，所以可以反覆同步。手動建的情境分支不動，它們的機率也算在總和裡。
    """
    from .models import Event
    require(session, projection_id)
    live = list(session.scalars(select(Event).where(
        Event.draft.is_(False), Event.status.in_(events.LIVE_STATUSES), Event.probability.is_not(None),
        Event.probability > 0).order_by(Event.id)))
    usable = [e for e in live if e.scenario_code or e.price_premium]
    skipped = [e.title for e in live if e not in usable]
    rows = cases(session, projection_id)
    for c in rows:
        if c.event_id is not None:
            c.removed_at = utcnow()
    manual = [c for c in rows if c.event_id is None and c.scenario_code is not None]
    base = next((c for c in rows if c.event_id is None and c.scenario_code is None), None)
    taken = sum(e.probability for e in usable) + sum(c.probability for c in manual)
    if taken > 1 + 1e-9:
        raise ProjectionError(f"事件和手動分支的機率加起來是 {taken:.0%}，超過 100%："
                              "這種情況下不能把它們當作互斥的分支。請先調低事件的機率，或移除部分手動分支")
    for e in usable:
        session.add(ProjectionCase(projection_id=projection_id, label=f"地緣：{e.title}"[:128],
                                   scenario_code=e.scenario_code, probability=float(e.probability),
                                   price_adjustment=float(e.price_premium or 0.0), event_id=e.id,
                                   event_probability=float(e.probability),
                                   note=f"由事件 #{e.id} 同步", created_by=actor))
    rest = max(0.0, 1.0 - taken)
    if base is None:
        session.add(ProjectionCase(projection_id=projection_id, label="基準", scenario_code=None, probability=rest,
                                   price_adjustment=0.0, note="同步地緣事件時建立", created_by=actor))
    else:
        base.probability = rest
    session.flush()
    return {"events": len(usable), "baseline_probability": rest,
            "skipped": skipped}


def event_impacts(session: Session, cfg: Config, as_of: datetime | None = None) -> dict:
    """每個仍在進行或可能發生的事件：如果發生，各期限的價格中樞比基準高 / 低多少；乘上機率就是期望影響。

    「如果發生」= 事件的情境（平衡表調整）+ 風險溢價。沒有情境也沒有溢價的事件沒有量化影響。
    """
    from .models import Event
    live = list(session.scalars(select(Event).where(Event.draft.is_(False),
                                                    Event.status.in_(events.LIVE_STATUSES)).order_by(Event.id)))
    out: dict = {"horizons": [h.model_dump() for h in horizons_of(cfg)], "events": {}}
    if not live or cfg.projection is None:
        return out
    base = adhoc(session, cfg, Scenario(), 0.0, as_of)["horizons"]
    for e in live:
        if not (e.scenario_code or e.price_premium):
            continue
        scenario = scenarios.load(session, cfg, e.scenario_code, as_of) if e.scenario_code else Scenario()
        hit = adhoc(session, cfg, scenario, float(e.price_premium or 0.0), as_of)["horizons"]
        row = {}
        for code, cell in hit.items():
            b = base[code]["central"]
            delta = None if cell["central"] is None or b is None else cell["central"] - b
            row[code] = {"if_happens": delta,
                         "expected": None if delta is None or e.probability is None else delta * e.probability,
                         "reason": cell.get("reason")}
        out["events"][e.id] = row
    return out


# ------------------------------ 計算 ------------------------------

def _volatility(prices: pd.Series, lookback: int, min_points: int) -> float | None:
    returns = np.log(prices[prices > 0]).diff().dropna().iloc[-lookback:]
    return float(returns.std()) if len(returns) >= min_points else None


def _balance_change(base: pd.Series, scenario: pd.Series, today: pd.Timestamp, end: pd.Timestamp,
                    period_days: int) -> tuple[float | None, float | None, int]:
    """期限內的平衡變化。期間與 [今天, 期限末] 有重疊就算在內。

    回傳（各期平衡變化的平均, 累積的庫存變化, 用到的期數）。庫存變化 = Σ 平衡變化 × 該期落在窗口內的天數：
    平衡的單位是「每日」（例如 百萬桶/日），乘上天數就是庫存（百萬桶）。
    """
    frame = pd.DataFrame({"base": base, "scenario": scenario}).dropna()
    window = frame[(frame.index <= end) & (frame.index + pd.Timedelta(days=period_days) > today)]
    if window.empty:
        return None, None, 0
    diff = window["scenario"] - window["base"]
    starts = list(frame.index)
    stock = 0.0
    for start, value in diff.items():
        # 這一期到下一期開始為止（月度數據每期長短不一）；最後一期或中間缺期時最多 period_days 天
        later = [d for d in starts if d > start]
        period_end = min(later[0], start + pd.Timedelta(days=period_days)) if later \
            else start + pd.Timedelta(days=period_days)
        overlap = (min(period_end, end) - max(start, today)).days
        stock += float(value) * max(overlap, 0)
    return float(diff.mean()), stock, len(window)


@dataclass(frozen=True)
class _Context:
    """算一個分支需要的公共輸入：全部來自基準（不套情境）的數據。"""
    settings: ProjectionSettings
    today: pd.Timestamp
    price: float | None
    volatility: float | None
    sensitivity: float | None
    base_balance: pd.Series
    period_days: int


def _context(session: Session, cfg: Config, as_of: datetime | None) -> tuple[_Context, IndicatorEngine, pd.Series]:
    settings = cfg.projection
    if settings is None:
        raise ProjectionError("config/projection.yaml 不存在，無法推演")
    base = IndicatorEngine(session, cfg, as_of=as_of)
    today = pd.Timestamp(as_of.date() if as_of else date.today())
    prices = base.series_values(settings.anchor)
    prices = prices[prices.index <= today]
    adjusted = base.adjusted_series(settings.anchor)[0]        # 波動率用換月調整後的價格，換月跳動不算波動
    adjusted = adjusted[adjusted.index <= today]
    volatility = (_volatility(adjusted, settings.volatility_lookback, settings.volatility_min_points)
                  if len(adjusted) else None)
    return (_Context(settings, today, float(prices.iloc[-1]) if len(prices) else None, volatility,
                     base.value_at(settings.sensitivity_parameter, today.date()),
                     base.compute(settings.balance_indicator),
                     PERIOD_DAYS[cfg.frequency_of(settings.balance_indicator)]), base, prices)


def _case_cells(ctx: _Context, scenario_balance: pd.Series | None, price_adjustment: float,
                drift: dict[str, float] | None = None) -> dict:
    """一個分支在各期限的結果。

    scenario_balance 為 None = 基準分支（平衡不變）。drift：各期限的基本面回歸（價格單位），各分支相同。
    """
    horizons = {}
    for h in ctx.settings.horizons:
        end = ctx.today + pd.Timedelta(days=h.calendar_days)
        drift_effect = (drift or {}).get(h.code) or 0.0
        cell = {"balance_change": 0.0, "stock_change": 0.0, "periods": 0, "balance_effect": 0.0,
                "drift_effect": drift_effect, "central": None, "low": None, "high": None, "reason": None}
        if scenario_balance is not None:
            change, stock, n = _balance_change(ctx.base_balance, scenario_balance, ctx.today, end, ctx.period_days)
            cell["balance_change"], cell["stock_change"], cell["periods"] = change, stock, n
            if change is None:
                cell["balance_effect"], cell["reason"] = None, "平衡表在這段期間沒有數據，算不出情境的影響"
            elif stock == 0:
                cell["balance_effect"] = 0.0
            elif ctx.sensitivity is None:
                cell["balance_effect"] = None
                cell["reason"] = f"缺參數 {ctx.settings.sensitivity_parameter}（庫存變化對價格的敏感度）"
            else:
                cell["balance_effect"] = -ctx.sensitivity * stock
        if ctx.price is not None and cell["balance_effect"] is not None:
            cell["central"] = ctx.price + drift_effect + cell["balance_effect"] + price_adjustment
            if ctx.volatility is not None:
                band = ctx.price * ctx.volatility * math.sqrt(h.trading_days)
                cell["low"], cell["high"], cell["band"] = cell["central"] - band, cell["central"] + band, band
        horizons[h.code] = cell
    return horizons


def adhoc(session: Session, cfg: Config, scenario: Scenario, price_adjustment: float = 0.0,
          as_of: datetime | None = None) -> dict:
    """不保存、臨時算一個分支：給一個情境，回傳它在各期限的價格中樞與區間。問答的「如果…會怎樣」用它。"""
    ctx, _, prices = _context(session, cfg, as_of)
    balance = IndicatorEngine(session, cfg, as_of=as_of, scenario=scenario).compute(ctx.settings.balance_indicator)
    return {"anchor": ctx.settings.anchor, "unit": cfg.series[ctx.settings.anchor].unit, "price": ctx.price,
            "price_date": prices.index[-1].date().isoformat() if len(prices) else None,
            "sensitivity": ctx.sensitivity, "balance_indicator": ctx.settings.balance_indicator,
            "horizons": {h.code: {"name_zh": h.name_zh, **cell}
                         for h, cell in zip(ctx.settings.horizons,
                                            _case_cells(ctx, balance, price_adjustment).values(), strict=True)}}


def _drift(cfg: Config, engine: IndicatorEngine, code: str | None, ctx: _Context,
           problems: list[str]) -> tuple[dict[str, float] | None, dict | None]:
    """推演選了公允價值模型時，各期限的基本面回歸（價格單位）。模型不可用時不加，並在 problems 裡說明。"""
    if not code:
        return None, None
    if code not in cfg.fair_values:
        problems.append(f"這個推演選的公允價值模型 {code} 已不存在，沒有加基本面回歸")
        return None, {"model": code, "status": "missing", "reasons": ["模型已不存在"]}
    fit = fair_value.current(engine, cfg, code, ctx.today.date())
    model = cfg.fair_values[code]
    info = {"model": code, "name_zh": model.name_zh, "status": fit.status, "reasons": fit.reasons,
            "fair": fit.fair, "deviation_pct": fit.deviation_pct, "half_life": fit.half_life, "date": fit.date,
            "effects": {}}
    if fit.status != "ok" or ctx.price is None:
        problems.append(f"公允價值模型目前不給漂移（{'；'.join(fit.reasons) or '沒有結果'}），所以沒有加基本面回歸")
        return None, info
    effects = {}
    for h in ctx.settings.horizons:
        move = fair_value.drift_log(fit, h.trading_days)
        effects[h.code] = ctx.price * (math.exp(move) - 1) if model.transform == "log" else move
    info["effects"] = effects
    return effects, info


def compute(session: Session, cfg: Config, projection_id: int, as_of: datetime | None = None) -> dict:
    row = require(session, projection_id)
    ctx, base, prices = _context(session, cfg, as_of)
    settings, price, volatility, sensitivity = ctx.settings, ctx.price, ctx.volatility, ctx.sensitivity
    unit = cfg.series[settings.anchor].unit
    balance_unit = cfg.indicators[settings.balance_indicator].unit

    problems = []
    if price is None:
        problems.append(f"{settings.anchor} 沒有數據，無法推演")
    if volatility is None and price is not None:
        problems.append(f"{settings.anchor} 的歷史不足 {settings.volatility_min_points} 個交易日，算不出波動區間")
    case_rows = cases(session, projection_id)
    total_probability = sum(c.probability for c in case_rows)
    probabilities_ok = abs(total_probability - 1) < 1e-6
    if not probabilities_ok:
        problems.append(f"各分支機率之和是 {total_probability:.0%}，不等於 100%，所以不計算機率加權價")

    from .models import Event
    for c in case_rows:                       # 事件在同步之後改過機率或結束了：提醒重新同步
        if c.event_id is None:
            continue
        event = session.get(Event, c.event_id)
        if event is None or event.status not in events.LIVE_STATUSES or event.draft:
            problems.append(f"分支「{c.label}」對應的事件已經結束或不存在，請重新同步地緣事件")
        elif event.probability != c.event_probability:
            problems.append(f"事件「{event.title}」的機率改成了 {event.probability or 0:.0%}"
                            f"（同步時是 {c.event_probability or 0:.0%}），請重新同步地緣事件")

    drift, drift_info = _drift(cfg, base, row.drift_model, ctx, problems)
    out_cases = []
    for c in case_rows:
        scenario_balance = (IndicatorEngine(session, cfg, as_of=as_of,
                                            scenario=scenarios.load(session, cfg, c.scenario_code, as_of))
                            .compute(settings.balance_indicator) if c.scenario_code else None)
        out_cases.append({"id": c.id, "label": c.label, "scenario_code": c.scenario_code,
                          "probability": c.probability, "price_adjustment": c.price_adjustment, "note": c.note,
                          "event_id": c.event_id,
                          "horizons": _case_cells(ctx, scenario_balance, c.price_adjustment, drift)})

    expected = {}
    for h in settings.horizons:
        centrals = [c["horizons"][h.code]["central"] for c in out_cases]
        expected[h.code] = (sum(c["probability"] * v for c, v in zip(out_cases, centrals, strict=True))
                            if probabilities_ok and out_cases and all(v is not None for v in centrals) else None)

    return {"id": row.id, "name_zh": row.name_zh, "drift_model": row.drift_model,
            "anchor": {"code": settings.anchor, "name_zh": cfg.series[settings.anchor].name_zh, "unit": unit,
                       "price": price, "date": prices.index[-1].date().isoformat() if len(prices) else None},
            "volatility": {"daily": volatility, "annualized": volatility * math.sqrt(252) if volatility else None,
                           "lookback": settings.volatility_lookback},
            "sensitivity": {"parameter": settings.sensitivity_parameter, "value": sensitivity,
                            "unit": cfg.parameters[settings.sensitivity_parameter].unit},
            "balance": {"indicator": settings.balance_indicator, "unit": balance_unit},
            "horizons": [h.model_dump() for h in settings.horizons], "drift": drift_info,
            "total_probability": total_probability, "cases": out_cases, "expected": expected, "problems": problems}
