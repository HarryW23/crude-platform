"""對單條時間序列做的統計變換：季節性、同比、線性擬合。純函數，不碰數據庫和配置。

「同期」的定義：往前推整數年的同一天；那天沒有數據時取前後 4 天內最近的一個點
（周度數據每年的日期會差幾天；月度數據記在月初，總能精確對上）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SAME_PERIOD_TOLERANCE = pd.Timedelta(days=4)


def year_ago(series: pd.Series, years: int = 1) -> pd.Series:
    """每個日期對應的 years 年前同期的值。找不到同期的日期為 NaN。"""
    if series.empty:
        return series.copy()
    shifted = series.copy()
    shifted.index = shifted.index + pd.DateOffset(years=years)
    shifted = shifted[~shifted.index.duplicated(keep="last")].sort_index()     # 閏日會和 2/28 撞在一起
    return shifted.reindex(series.index, method="nearest", tolerance=SAME_PERIOD_TOLERANCE)


def seasonal_stats(series: pd.Series, years: int = 5) -> pd.DataFrame:
    """每個日期對應的前 years 年同期的 min / max / avg。任何一年缺同期值，該日期就全部為 NaN。"""
    history = pd.concat([year_ago(series, k) for k in range(1, years + 1)], axis=1) if years >= 1 and len(series) \
        else pd.DataFrame(index=series.index)
    complete = history.notna().all(axis=1) if history.shape[1] else pd.Series(False, index=series.index)
    out = pd.DataFrame({"min": history.min(axis=1), "max": history.max(axis=1), "avg": history.mean(axis=1)},
                       index=series.index)
    out[~complete] = np.nan
    return out


def aligned_pairs(y: pd.Series, x: pd.Series, align_tolerance_days: int = 3) -> pd.DataFrame:
    """在 x 有值的日期上配對 y（y 當天沒有值時取 3 天內最近的前值）。linear_fit 和樣本外檢驗共用。"""
    if x.empty or y.empty:
        return pd.DataFrame({"x": [], "y": []}, index=pd.DatetimeIndex([]), dtype=float)
    y_on_x = y.reindex(x.index, method="ffill", tolerance=pd.Timedelta(days=align_tolerance_days))
    return pd.DataFrame({"x": x, "y": y_on_x}).dropna()


def ols(frame: pd.DataFrame) -> tuple[float, float, float] | None:
    """y = a + b·x 的最小二乘：（截距, 斜率, R²）。x 沒有變化時回 None。"""
    if len(frame) < 2 or frame["x"].std() == 0:
        return None
    slope, intercept = np.polyfit(frame["x"], frame["y"], 1)
    fitted = intercept + slope * frame["x"]
    total = float(((frame["y"] - frame["y"].mean()) ** 2).sum())
    r2 = 1 - float(((frame["y"] - fitted) ** 2).sum()) / total if total else 0.0
    return float(intercept), float(slope), r2


def linear_fit(y: pd.Series, x: pd.Series, lookback_days: int, min_points: int,
               align_tolerance_days: int = 3) -> dict:
    """在 x 有值的日期上擬合 y = a + b·x。y 當天沒有值時取 3 天內最近的前值（例如 x 是周五、y 是日度）。

    回傳 {status, n, slope, intercept, r2, points, latest}；樣本不足時 status = "insufficient"，不給擬合結果。
    """
    result = {"status": "insufficient", "n": 0, "slope": None, "intercept": None, "r2": None,
              "points": [], "latest": None}
    frame = aligned_pairs(y, x, align_tolerance_days)
    if frame.empty:
        return result
    frame = frame[frame.index >= frame.index[-1] - pd.Timedelta(days=lookback_days)]
    result["n"] = len(frame)
    result["points"] = [{"date": ts.date().isoformat(), "x": float(r.x), "y": float(r.y)}
                        for ts, r in frame.iterrows()]
    fit = ols(frame) if len(frame) >= min_points else None
    if fit is None:
        return result
    intercept, slope, r2 = fit
    fitted = intercept + slope * frame["x"]
    last = frame.iloc[-1]
    result.update(status="ok", slope=float(slope), intercept=float(intercept), r2=r2,
                  latest={"date": frame.index[-1].date().isoformat(), "x": float(last.x), "y": float(last.y),
                          "fitted": float(intercept + slope * last.x),
                          "residual": float(last.y - (intercept + slope * last.x)),
                          "residual_std": float((frame["y"] - fitted).std())})
    return result
