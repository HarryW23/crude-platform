"""推演用的統計模型（#27）：只用價格本身，每個日期只用當時已知的數據。實時推演和回測調用同一個函數。

波動率（給區間）
  historical  最近 volatility_lookback 個交易日日收益的標準差（原來的做法）
  ewma        指數加權（RiskMetrics）：σ²_t = λ·σ²_{t−1} + (1 − λ)·r_t²，近期的波動權重更高，
              市場突然變劇烈時區間跟得更快。前 volatility_min_points 天用樣本方差起步，之前沒有值

漂移（給中樞）：動量
  x_t = 過去 lookback 個交易日的對數漲跌，y_t = 之後 h 個交易日的對數漲跌。
  在每個日期 t，只用「之後 h 天的收益在 t 之前已經實現」的樣本（s + h ≤ t），且在最近 window 個交易日內，
  做不帶截距的回歸 y = b·x（不帶截距：不把樣本期的平均漲跌當成趨勢外推）。
  樣本的 h 日收益互相重疊，標準誤按 √h 放大；|t| < t_min 時不給漂移（0），寧缺勿濫。
  漂移 = b·x_t（對數收益）。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .schema import ProjectionSettings, StatDriftModel


def log_returns(prices: pd.Series) -> pd.Series:
    prices = prices[prices > 0].sort_index()
    return np.log(prices).diff().dropna()


def historical_vol(prices: pd.Series, lookback: int, min_points: int) -> pd.Series:
    """每個日期的歷史波動率（滾動標準差）。"""
    return log_returns(prices).rolling(lookback, min_periods=min_points).std()


def ewma_vol(prices: pd.Series, lam: float, min_points: int) -> pd.Series:
    """每個日期的 EWMA 波動率。前 min_points 個收益用樣本方差起步，之前沒有值。"""
    r = log_returns(prices)
    out = pd.Series(np.nan, index=r.index)
    if len(r) < min_points:
        return out
    values = r.to_numpy()
    var = float(np.var(values[:min_points], ddof=1))
    out.iloc[min_points - 1] = math.sqrt(var)
    for i in range(min_points, len(values)):
        var = lam * var + (1 - lam) * values[i] ** 2
        out.iloc[i] = math.sqrt(var)
    return out


def volatility_series(prices: pd.Series, settings: ProjectionSettings, method: str | None = None) -> pd.Series:
    method = method or settings.volatility_method
    if method == "ewma":
        return ewma_vol(prices, settings.ewma_lambda, settings.volatility_min_points)
    return historical_vol(prices, settings.volatility_lookback, settings.volatility_min_points)


def latest_volatility(prices: pd.Series, settings: ProjectionSettings) -> float | None:
    vol = volatility_series(prices, settings).dropna()
    return float(vol.iloc[-1]) if len(vol) else None


def momentum_drift(prices: pd.Series, model: StatDriftModel, horizon: int) -> pd.DataFrame:
    """每個日期的動量漂移（對數收益）和當時的估計：列 drift / slope / t / n。只用當時已實現的樣本。"""
    prices = prices[prices > 0].sort_index()
    logp = np.log(prices.to_numpy(dtype=float))
    n_all = len(logp)
    cols = ["drift", "slope", "t", "n"]
    if n_all <= model.lookback_days + horizon + 2:
        return pd.DataFrame(np.nan, index=prices.index, columns=cols)
    x = np.full(n_all, np.nan)
    y = np.full(n_all, np.nan)
    x[model.lookback_days:] = logp[model.lookback_days:] - logp[:-model.lookback_days]
    y[:-horizon] = logp[horizon:] - logp[:-horizon]
    ok = ~np.isnan(x) & ~np.isnan(y)
    cum = lambda v: np.concatenate([[0.0], np.cumsum(np.where(ok, v, 0.0))])    # noqa: E731
    sxx, sxy, syy, cnt = cum(x * x), cum(x * y), cum(y * y), cum(np.ones(n_all))
    drift = np.full(n_all, np.nan)
    slope = np.full(n_all, np.nan)
    tstat = np.full(n_all, np.nan)
    count = np.zeros(n_all)
    for i in range(n_all):
        hi = i - horizon                    # 樣本 s 的收益到 s + h 才實現：s ≤ i − h
        lo = max(0, i - model.window_days)
        if hi < lo:
            continue
        n = cnt[hi + 1] - cnt[lo]
        xx, xy, yy = sxx[hi + 1] - sxx[lo], sxy[hi + 1] - sxy[lo], syy[hi + 1] - syy[lo]
        count[i] = n
        if n < model.min_points or xx <= 0 or np.isnan(x[i]):
            continue
        b = xy / xx
        sse = max(yy - 2 * b * xy + b * b * xx, 0.0)
        se = math.sqrt(sse / max(n - 1, 1) / xx) * math.sqrt(horizon)      # 重疊樣本：標準誤按 √h 放大
        t = b / se if se > 0 else 0.0
        slope[i], tstat[i] = b, t
        drift[i] = b * x[i] if abs(t) >= model.t_min else 0.0
    return pd.DataFrame({"drift": drift, "slope": slope, "t": tstat, "n": count}, index=prices.index)


def gaussian_score(realized: pd.Series, sigma: pd.Series) -> float | None:
    """區間的評分：實際收益在 N(0, σ²) 下的平均負對數似然，越小越好（區間太窄、太寬都會變差）。"""
    data = pd.DataFrame({"r": realized, "s": sigma}).dropna()
    data = data[data["s"] > 0]
    if data.empty:
        return None
    return float((0.5 * np.log(2 * math.pi * data["s"] ** 2) + data["r"] ** 2 / (2 * data["s"] ** 2)).mean())
