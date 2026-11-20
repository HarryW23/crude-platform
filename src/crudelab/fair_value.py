"""基本面公允價值：價格按基本面「應該」在哪，現在偏離了多少，偏離通常多快消退。

一、模型（config/fair_value.yaml）
  在每個日期 t，只用 [t − window_days, t] 的數據做最小二乘：
      y = a + Σ bᵢ · xᵢ            y = 對數價格（transform: log）或價格（level），xᵢ = 因子當時已知的值
  公允價值 = 用當天的因子代入擬合結果；偏離 = y − 擬合值（log 時約等於高出公允價值的百分比）。
  每個日期都只用它之前的數據擬合，所以整條歷史曲線上每個點都是當時就能算出的，可以直接回測。

二、拆解
  因子 i 的貢獻 = bᵢ × (xᵢ 當天的值 − xᵢ 在窗口裡的均值)。公允價值 = 窗口均值水平 + 各因子貢獻之和，
  所以能說清楚「哪個因子把公允價值推高 / 壓低了多少」。

三、偏離會不會回歸
  在窗口裡用相鄰交易日的殘差估 AR(1) 係數 φ：殘差_t ≈ φ × 殘差_{t−1}。
  0 < φ < 1 時偏離會回歸，半衰期 = ln 0.5 / ln φ 個交易日；
  h 個交易日後預計還剩 φʰ，所以預計消退的部分 = (1 − φʰ) × 當前偏離。這就是給推演的漂移：
      log 模型：漂移後的價格 = 當前價格 × exp(−(1 − φʰ) × 偏離)
  這裡假設因子在期限內不變：它回答的是「基本面不變的話，價格會往哪邊回」。

四、什麼時候不給漂移（status 不是 ok）
  樣本不足、因子當天沒有數據、擬合優度低於 min_r2、φ 不在 (0, 1) 之間（偏離沒有回歸的跡象）。
  這些時候照樣展示擬合結果，但推演不加漂移，並說明原因。

五、數據可得性
  實時計算時，數據庫裡有的就是已知的；回測時按 backtest.yaml 的發佈延遲對齊（同一個 fit_at，保證一致）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .config import Config
from .indicators import IndicatorEngine
from .schema import FairValueModel, Frequency


@dataclass
class Fit:
    model: str
    date: str | None = None
    status: str = "missing"             # ok / weak / insufficient / missing
    reasons: list[str] = field(default_factory=list)
    n: int = 0
    price: float | None = None
    fair: float | None = None
    deviation: float | None = None      # 模型空間的殘差（log 時是對數差）
    deviation_pct: float | None = None  # 價格高出公允價值的百分比（level 模型是相對公允價值的比例）
    z: float | None = None              # 偏離 ÷ 窗口裡殘差的標準差
    r2: float | None = None
    phi: float | None = None
    half_life: float | None = None      # 交易日
    intercept: float | None = None
    coefficients: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)    # 模型空間
    factor_values: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def aligned_frame(engine: IndicatorEngine, cfg: Config, model: FairValueModel,
                  lags: dict[Frequency, int] | None = None, until: pd.Timestamp | None = None) -> pd.DataFrame:
    """以 target 的交易日為索引：price、y，以及每個因子在那一天已知的最後一個值（扣發佈延遲 lags，按頻率）。"""
    from .backtest import as_available  # 共用同一個「當時已知」的對齊
    prices = engine.values(model.target).dropna()
    if until is not None:
        prices = prices[prices.index <= until]
    prices = prices[prices > 0] if model.transform == "log" else prices
    frame = pd.DataFrame({"price": prices})
    frame["y"] = np.log(prices) if model.transform == "log" else prices
    for factor in model.factors:
        values = engine.values(factor.input).dropna()
        lag = (lags or {}).get(cfg.frequency_of(factor.input), 0)
        if len(frame):
            frame[factor.input] = as_available(values.to_frame("v"), frame.index, lag)["v"].astype(float)
        else:
            frame[factor.input] = pd.Series(dtype=float)
    return frame


def fit_at(frame: pd.DataFrame, t: pd.Timestamp, model: FairValueModel) -> Fit:
    """只用 t 及之前、窗口內的數據擬合，算出 t 當天的公允價值。"""
    fit = Fit(model=model.code, date=t.date().isoformat())
    names = [f.input for f in model.factors]
    if t not in frame.index:
        fit.reasons.append(f"{t.date()} 沒有 {model.target} 的價格")
        return fit
    current = frame.loc[t]
    fit.price = float(current["price"])
    if current[names].isna().any():
        missing = [n for n in names if pd.isna(current[n])]
        fit.reasons.append(f"這一天沒有因子的數據：{'、'.join(missing)}")
        return fit
    window = frame[(frame.index <= t) & (frame.index >= t - pd.Timedelta(days=model.window_days))].dropna()
    fit.n = len(window)
    if fit.n < model.min_points:
        fit.status = "insufficient"
        fit.reasons.append(f"窗口裡只有 {fit.n} 個樣本，至少需要 {model.min_points} 個")
        return fit
    x = window[names].to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    coef, _, rank, _ = np.linalg.lstsq(design, window["y"].to_numpy(dtype=float), rcond=None)
    if rank < design.shape[1]:
        fit.status = "insufficient"
        fit.reasons.append("因子之間完全共線，或某個因子在窗口裡沒有變化")
        return fit
    fitted = design @ coef
    resid = window["y"].to_numpy(dtype=float) - fitted
    total = float(((window["y"] - window["y"].mean()) ** 2).sum())
    fit.r2 = 1 - float((resid ** 2).sum()) / total if total > 0 else 0.0
    dof = max(len(resid) - design.shape[1], 1)
    resid_std = math.sqrt(float((resid ** 2).sum()) / dof)
    fit.intercept, fit.coefficients = float(coef[0]), {n: float(c) for n, c in zip(names, coef[1:], strict=True)}
    means = window[names].mean()
    fit.factor_values = {n: float(current[n]) for n in names}
    fit.contributions = {n: fit.coefficients[n] * (float(current[n]) - float(means[n])) for n in names}
    level = float(coef[0] + sum(fit.coefficients[n] * float(current[n]) for n in names))
    y_now = float(current["y"])
    fit.deviation = y_now - level
    fit.fair = math.exp(level) if model.transform == "log" else level
    if model.transform == "log":
        fit.deviation_pct = math.exp(fit.deviation) - 1
    else:
        fit.deviation_pct = fit.deviation / fit.fair if fit.fair else None
    fit.z = fit.deviation / resid_std if resid_std > 0 else None
    if len(resid) > 20:
        previous, following = resid[:-1], resid[1:]
        denom = float((previous ** 2).sum())
        fit.phi = float((previous * following).sum() / denom) if denom > 0 else None
    if fit.phi is not None and 0 < fit.phi < 1:
        fit.half_life = math.log(0.5) / math.log(fit.phi)
    fit.status = "ok"
    if fit.r2 < model.min_r2:
        fit.status = "weak"
        fit.reasons.append(f"擬合優度 R² = {fit.r2:.2f}，低於門檻 {model.min_r2}，基本面因子解釋不了價格")
    if fit.phi is None or not 0 < fit.phi < 1:
        fit.status = "weak"
        fit.reasons.append("偏離沒有回歸的跡象（殘差的自相關係數不在 0 到 1 之間）")
    return fit


def drift_log(fit: Fit, trading_days: int) -> float | None:
    """h 個交易日裡預計消退的偏離（模型空間，取負號：偏高就往下）。status 不是 ok 時沒有漂移。"""
    if fit.status != "ok" or fit.deviation is None or fit.phi is None:
        return None
    return -(1 - fit.phi ** trading_days) * fit.deviation


def drift_effect(fit: Fit, model: FairValueModel, price: float, trading_days: int) -> float | None:
    """漂移換算成價格的變化。"""
    move = drift_log(fit, trading_days)
    if move is None:
        return None
    return price * (math.exp(move) - 1) if model.transform == "log" else move


def current(engine: IndicatorEngine, cfg: Config, code: str, on: date | None = None) -> Fit:
    """實時：數據庫裡有的就是已知的，不扣發佈延遲。on 不給就用 target 最近的交易日（不晚於今天）。"""
    model = cfg.fair_values[code]
    today = pd.Timestamp(on or (engine.as_of.date() if engine.as_of else date.today()))
    frame = aligned_frame(engine, cfg, model, until=today)
    if frame.empty:
        return Fit(model=code, reasons=[f"{cfg.name_of(model.target)} 沒有數據"])
    return fit_at(frame, frame.index[-1], model)


def history(engine: IndicatorEngine, cfg: Config, code: str, days: int = 750) -> list[dict]:
    """最近 days 個交易日，每一天都只用當時之前的數據擬合（和回測同一個算法）。"""
    model = cfg.fair_values[code]
    frame = aligned_frame(engine, cfg, model)
    today = pd.Timestamp(engine.as_of.date() if engine.as_of else date.today())
    frame = frame[frame.index <= today]
    out = []
    for t in frame.index[-days:]:
        fit = fit_at(frame, t, model)
        out.append({"date": t.date().isoformat(), "price": fit.price, "fair": fit.fair,
                    "deviation_pct": fit.deviation_pct, "z": fit.z, "status": fit.status})
    return out


def describe(cfg: Config, fit: Fit, horizons) -> dict:
    """給頁面和報告用：附上因子名字、各期限的漂移、偏離的歷史分位。"""
    model = cfg.fair_values[fit.model]
    names = {f.input: f.name_zh or cfg.name_of(f.input) for f in model.factors}
    def as_pct(v: float) -> float | None:
        if model.transform == "log":
            return math.exp(v) - 1
        return v / fit.fair if fit.fair else None

    return {**fit.as_dict(), "code": model.code, "name_zh": model.name_zh, "target": model.target,
            "target_name": cfg.name_of(model.target),
            "transform": model.transform, "window_days": model.window_days, "min_r2": model.min_r2,
            "notes": model.notes,
            "unit": cfg.unit_of(model.target),
            "factors": [{"input": n, "name_zh": names[n], "unit": cfg.unit_of(n), "value": fit.factor_values.get(n),
                         "coefficient": fit.coefficients.get(n),
                         "contribution": fit.contributions.get(n),
                         "contribution_pct": None if fit.contributions.get(n) is None else as_pct(fit.contributions[n])}
                        for n in names],
            "drift": {h.code: {"name_zh": h.name_zh, "trading_days": h.trading_days,
                               "effect": None if fit.price is None
                               else drift_effect(fit, model, fit.price, h.trading_days)} for h in horizons}}
