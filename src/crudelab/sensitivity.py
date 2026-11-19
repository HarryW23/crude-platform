"""從歷史估計推演的核心參數：庫存每多累積 1 單位，價格變多少（projection.yaml 的 sensitivity_parameter）。

做法（設定在 projection.yaml 的 sensitivity_estimate）：
  1. 庫存用月度序列（預設 OECD 商業庫存），只取今天以前的期間（不用預測期）
  2. 價格取推演錨定價格的月均值，用換月調整後的價格（避免換月跳動混進漲跌）
  3. 回歸：價格的月度對數漲跌 = a + g × 庫存的月度變化
     用變化而不是水平：價格和庫存的水平都會長期漂移，直接回歸水平容易得到虛假的關係
  4. 敏感度 = −g × 當前價格（把「每單位庫存的漲跌幅」換成「每單位庫存的美元」，按今天的價格水平）
     95% 區間 = 敏感度 ± 1.96 × 標準誤 × 當前價格

結論分四種：
  ok           係數為負（累庫 → 跌價）且顯著（|t| ≥ 2），樣本足夠：可以作為參考採用
  weak         方向對但不顯著：關係太弱，採用了推演的供需效應也不可靠
  wrong_sign   係數為正（累庫反而漲價）：這段歷史裡價格主導了庫存（例如價格高時補庫），不能用
  insufficient 樣本不夠

這是相關，不是因果：價格也會反過來影響庫存（遠期升水時有人囤油），還有很多別的因素同時在動。
所以它只是給一個有依據的起點，採用與否由人決定；採用時會把估計的依據記在參數的版本說明裡。
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from .config import Config
from .indicators import IndicatorEngine

Z95 = 1.96


def estimate(engine: IndicatorEngine, cfg: Config, today: date | None = None) -> dict:
    settings = cfg.projection
    spec = settings.sensitivity_estimate if settings else None
    out: dict = {"status": "unconfigured", "parameter": settings.sensitivity_parameter if settings else None,
                 "unit": cfg.parameters[settings.sensitivity_parameter].unit if settings else None,
                 "stocks": None, "stocks_name": None, "anchor": settings.anchor if settings else None,
                 "n": 0, "start": None, "end": None, "slope_pct": None, "se_pct": None, "t": None, "r2": None,
                 "price": None, "value": None, "low": None, "high": None, "per_month_1mbd": None,
                 "points": [], "message": ""}
    if spec is None:
        out["message"] = "projection.yaml 裡沒有 sensitivity_estimate 設定"
        return out
    today = pd.Timestamp(today or (engine.as_of.date() if engine.as_of else date.today()))
    out.update(stocks=spec.stocks, stocks_name=cfg.name_of(spec.stocks))
    stocks = engine.series_values(spec.stocks)
    stocks = stocks[(stocks.index <= today) & (stocks.index >= today - pd.Timedelta(days=spec.lookback_days))]
    raw = engine.series_values(settings.anchor)
    raw = raw[raw.index <= today]
    adjusted = engine.adjusted_series(settings.anchor)[0]
    adjusted = adjusted[adjusted.index <= today]
    if raw.empty or stocks.empty:
        out.update(status="insufficient", message="沒有價格或庫存數據")
        return out
    monthly_price = adjusted.resample("MS").mean()
    frame = pd.DataFrame({"stock": stocks.groupby(stocks.index.to_period("M").to_timestamp()).last(),
                          "price": monthly_price}).dropna()
    frame = frame[frame["price"] > 0]
    data = pd.DataFrame({"ds": frame["stock"].diff(), "r": np.log(frame["price"]).diff()}).dropna()
    price = float(raw.iloc[-1])
    out.update(n=len(data), price=price,
               start=data.index[0].date().isoformat() if len(data) else None,
               end=data.index[-1].date().isoformat() if len(data) else None,
               points=[{"date": ts.date().isoformat(), "x": float(r.ds), "y": float(r.r)} for ts, r in data.iterrows()])
    if len(data) < spec.min_points or data["ds"].std() == 0:
        out.update(status="insufficient", message=f"只有 {len(data)} 個月的樣本，少於 {spec.min_points}，不估計")
        return out
    x, y = data["ds"].to_numpy(), data["r"].to_numpy()
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    n = len(x)
    se = math.sqrt(float(resid @ resid) / (n - 2) / float(((x - x.mean()) ** 2).sum()))
    t = slope / se if se > 0 else 0.0
    total = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / total if total else 0.0
    value = -slope * price
    out.update(slope_pct=float(slope), se_pct=se, t=float(t), r2=r2, value=value,
               low=value - Z95 * se * price, high=value + Z95 * se * price, per_month_1mbd=value * 30)
    if slope >= 0:
        out.update(status="wrong_sign", message="這段歷史裡庫存增加時價格反而偏漲（價格主導了庫存），不能用來推演")
    elif abs(t) < 2:
        out.update(status="weak", message=f"方向對（累庫 → 跌價），但不顯著（t = {t:.1f}），關係太弱，不建議採用")
    else:
        out.update(status="ok", message=f"累庫 → 跌價，統計上顯著（t = {t:.1f}），可以作為參考採用")
    return out


def adoption_note(result: dict) -> str:
    return (f"歷史估計：{result['stocks_name']} 月度變化 vs {result['anchor']} 月度漲跌，"
            f"{result['start']} 至 {result['end']}，{result['n']} 個月，"
            f"t = {result['t']:.1f}，R² = {result['r2']:.2f}，"
            f"95% 區間 {result['low']:.3f} – {result['high']:.3f}，按當時價格 {result['price']:.2f} 換算")
