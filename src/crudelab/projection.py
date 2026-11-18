"""價格推演：把若干個情境分支各自折成一個價格區間，再按機率加權。

對每個分支、每個期限（1 周 / 1 個月 / 3 個月）：
    基本面回歸 = 推演選了公允價值模型時，預計在期限內消退的偏離（見 fair_value.py）；沒選則為 0。各分支相同
    平衡變化   = 期限內各期「情境下的平衡 − 基準平衡」的平均          （來自平衡表和情境調整）
    供需效應   = −敏感度 × 平衡變化                                    （敏感度是參數：每多 1 單位過剩，價格跌多少）
    中樞       = 當前價格 + 基本面回歸 + 供需效應 + 分支的直接價格調整（直接調整 = 人給的判斷，如風險溢價）
    區間       = 中樞 ± 當前價格 × 日波動率 × √期限交易日數            （一倍標準差，來自歷史已實現波動）
    機率加權價 = Σ 機率 × 中樞                                          （各分支機率之和必須等於 1）

每一項都能指回來源：當前價格和波動率來自行情，平衡變化來自情境調整，敏感度和機率是人設的。
這是把假設攤開來算的工具，不是黑盒預測：
  - 不選公允價值模型時沒有「基準漂移」，基準分支的中樞就是當前價格
  - 敏感度是線性的、不隨期限變化，這是一個粗略的經驗設定，必須由使用者自己給
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

from . import fair_value, scenarios
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


# ------------------------------ 計算 ------------------------------

def _volatility(prices: pd.Series, lookback: int, min_points: int) -> float | None:
    returns = np.log(prices[prices > 0]).diff().dropna().iloc[-lookback:]
    return float(returns.std()) if len(returns) >= min_points else None


def _balance_change(base: pd.Series, scenario: pd.Series, today: pd.Timestamp, end: pd.Timestamp,
                    period_days: int) -> tuple[float | None, int]:
    """期限內各期的平均平衡變化。期間與 [今天, 期限末] 有重疊就算在內。回傳（變化, 用到的期數）。"""
    frame = pd.DataFrame({"base": base, "scenario": scenario}).dropna()
    window = frame[(frame.index <= end) & (frame.index + pd.Timedelta(days=period_days) > today)]
    if window.empty:
        return None, 0
    return float((window["scenario"] - window["base"]).mean()), len(window)


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
    volatility = (_volatility(prices, settings.volatility_lookback, settings.volatility_min_points)
                  if len(prices) else None)
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
        cell = {"balance_change": 0.0, "periods": 0, "balance_effect": 0.0, "drift_effect": drift_effect,
                "central": None, "low": None, "high": None, "reason": None}
        if scenario_balance is not None:
            change, n = _balance_change(ctx.base_balance, scenario_balance, ctx.today, end, ctx.period_days)
            cell["balance_change"], cell["periods"] = change, n
            if change is None:
                cell["balance_effect"], cell["reason"] = None, "平衡表在這段期間沒有數據，算不出情境的影響"
            elif change == 0:
                cell["balance_effect"] = 0.0
            elif ctx.sensitivity is None:
                cell["balance_effect"] = None
                cell["reason"] = f"缺參數 {ctx.settings.sensitivity_parameter}（平衡變化對價格的敏感度）"
            else:
                cell["balance_effect"] = -ctx.sensitivity * change
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

    drift, drift_info = _drift(cfg, base, row.drift_model, ctx, problems)
    out_cases = []
    for c in case_rows:
        scenario_balance = (IndicatorEngine(session, cfg, as_of=as_of,
                                            scenario=scenarios.load(session, cfg, c.scenario_code, as_of))
                            .compute(settings.balance_indicator) if c.scenario_code else None)
        out_cases.append({"id": c.id, "label": c.label, "scenario_code": c.scenario_code,
                          "probability": c.probability, "price_adjustment": c.price_adjustment, "note": c.note,
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
