"""對單條時間序列做的統計變換：季節性、同比、線性擬合。純函數，不碰數據庫和配置。

「同期」的定義：往前推整數年的同一天；那天沒有數據時取前後 4 天內最近的一個點
（周度數據每年的日期會差幾天；月度數據記在月初，總能精確對上）。
"""
from __future__ import annotations

from dataclasses import dataclass

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


def aligned_pairs(y: pd.Series, x: pd.Series, align_tolerance_days: int = 3, lag_days: int = 0) -> pd.DataFrame:
    """在 x 有值的日期上配對 y（y 當天沒有值時取 3 天內最近的前值）。頁面擬合和樣本外檢驗共用。

    lag_days > 0：x 領先 y，配對的是 y(t) 和 x(t − lag_days)。索引是 y 的日期。
    """
    if x.empty or y.empty:
        return pd.DataFrame({"x": [], "y": []}, index=pd.DatetimeIndex([]), dtype=float)
    if lag_days:
        x = pd.Series(x.to_numpy(), index=x.index + pd.Timedelta(days=lag_days))
    y_on_x = y.reindex(x.index, method="ffill", tolerance=pd.Timedelta(days=align_tolerance_days))
    return pd.DataFrame({"x": x, "y": y_on_x}).dropna()


# ------------------------------ 關係模型（#26） ------------------------------
#
#   shape  linear  y = a + b·x
#          hinge   y = a + b·x + c·max(0, x − k)：轉折點 k 兩側斜率不同（b 和 b + c）。
#                  k 在 x 的 20%–80% 分位數之間找誤差最小的，兩側各至少 15% 的樣本
#   method ols     最小二乘
#          huber   穩健回歸（Huber，c = 1.345，殘差尺度用 MAD）：偏離很遠的點權重變小，少數異常值帶不動整條線

HUBER_C, HINGE_QUANTILES, HINGE_MIN_SHARE = 1.345, np.linspace(0.2, 0.8, 13), 0.15


@dataclass
class RelationModel:
    method: str
    shape: str
    intercept: float
    slope: float
    r2: float
    hinge: float | None = None           # 轉折點（x 的值）
    slope_above: float | None = None     # 轉折點以上的斜率（b + c）

    def predict(self, x):
        out = self.intercept + self.slope * np.asarray(x, dtype=float)
        if self.hinge is not None:
            out = out + (self.slope_above - self.slope) * np.maximum(0.0, np.asarray(x, dtype=float) - self.hinge)
        return out

    def curve(self, lo: float, hi: float) -> list[dict]:
        xs = [lo, *([self.hinge] if self.hinge is not None and lo < self.hinge < hi else []), hi]
        return [{"x": float(v), "y": float(self.predict(v))} for v in xs]


def _solve(design: np.ndarray, y: np.ndarray, method: str) -> np.ndarray:
    beta = np.linalg.lstsq(design, y, rcond=None)[0]
    if method != "huber":
        return beta
    for _ in range(50):                                  # 迭代加權最小二乘
        resid = y - design @ beta
        scale = np.median(np.abs(resid - np.median(resid))) / 0.6745
        if scale <= 0:
            break
        w = np.minimum(1.0, HUBER_C / np.maximum(np.abs(resid) / scale, 1e-12))
        sw = np.sqrt(w)
        new = np.linalg.lstsq(design * sw[:, None], y * sw, rcond=None)[0]
        if np.allclose(new, beta, rtol=1e-8, atol=1e-10):
            beta = new
            break
        beta = new
    return beta


def fit_relation(frame: pd.DataFrame, method: str = "ols", shape: str = "linear") -> RelationModel | None:
    """按 method / shape 擬合 y 對 x。x 沒有變化、或轉折點兩側樣本不夠時回 None（不偷偷退回成直線）。"""
    if len(frame) < 3 or frame["x"].std() == 0:
        return None
    x, y = frame["x"].to_numpy(dtype=float), frame["y"].to_numpy(dtype=float)
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = lambda pred: 1 - float(((y - pred) ** 2).sum()) / sst if sst else 0.0  # noqa: E731
    if shape == "linear":
        a, b = _solve(np.column_stack([np.ones_like(x), x]), y, method)
        model = RelationModel(method, shape, float(a), float(b), 0.0)
        model.r2 = r2(model.predict(x))
        return model
    best = None
    minimum = max(10, int(HINGE_MIN_SHARE * len(x)))
    for k in np.unique(np.quantile(x, HINGE_QUANTILES)):
        if (x <= k).sum() < minimum or (x > k).sum() < minimum:
            continue
        a, b, c = _solve(np.column_stack([np.ones_like(x), x, np.maximum(0.0, x - k)]), y, method)
        model = RelationModel(method, shape, float(a), float(b), 0.0, float(k), float(b + c))
        sse = float(((y - model.predict(x)) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, model)
    if best is None:
        return None
    best[1].r2 = r2(best[1].predict(x))
    return best[1]


def relation_stability(frame: pd.DataFrame, method: str, shape: str, pieces: int = 4) -> dict:
    """把窗口按時間切成幾段各自擬合：斜率方向是否一致、相對波動多大（每段至少 10 個樣本）。"""
    out = {"pieces": 0, "slopes": [], "same_sign": None, "slope_cv": None}
    if len(frame) < 10 * pieces:
        return out
    slopes = []
    for part in np.array_split(np.arange(len(frame)), pieces):
        model = fit_relation(frame.iloc[part], method, "linear" if shape == "hinge" else shape)
        if model is not None:
            slopes.append(model.slope)
    if not slopes:
        return out
    arr, median = np.array(slopes), float(np.median(slopes))
    out.update(pieces=len(slopes), slopes=[float(v) for v in slopes],
               same_sign=float((np.sign(arr) == np.sign(median)).mean()) if median else None,
               slope_cv=float(arr.std() / abs(arr.mean())) if arr.mean() else None)
    return out


def relation_fit(y: pd.Series, x: pd.Series, lookback_days: int, min_points: int, method: str = "ols",
                 shape: str = "linear", lag_days: int = 0, align_tolerance_days: int = 3) -> dict:
    """頁面用：最近 lookback_days 的樣本按 method / shape 擬合，附最新一點的擬合值、殘差和分段穩定性。"""
    result = {"status": "insufficient", "n": 0, "method": method, "shape": shape, "lag_days": lag_days,
              "slope": None, "intercept": None, "r2": None, "hinge": None, "slope_above": None,
              "curve": [], "stability": None, "points": [], "latest": None}
    frame = aligned_pairs(y, x, align_tolerance_days, lag_days)
    if frame.empty:
        return result
    frame = frame[frame.index >= frame.index[-1] - pd.Timedelta(days=lookback_days)]
    result["n"] = len(frame)
    result["points"] = [{"date": ts.date().isoformat(), "x": float(r.x), "y": float(r.y)}
                        for ts, r in frame.iterrows()]
    model = fit_relation(frame, method, shape) if len(frame) >= min_points else None
    if model is None:
        return result
    fitted = model.predict(frame["x"])
    last = frame.iloc[-1]
    result.update(status="ok", slope=model.slope, intercept=model.intercept, r2=model.r2, hinge=model.hinge,
                  slope_above=model.slope_above,
                  curve=model.curve(float(frame["x"].min()), float(frame["x"].max())),
                  stability=relation_stability(frame, method, shape),
                  latest={"date": frame.index[-1].date().isoformat(), "x": float(last.x), "y": float(last.y),
                          "fitted": float(model.predict(last.x)),
                          "residual": float(last.y - model.predict(last.x)),
                          "residual_std": float((frame["y"] - fitted).std())})
    return result
