"""回測：拿歷史檢驗方向信號和推演的價格區間。

一、評估什麼
  方向信號   每個評估日 t 算出信號分數，對比 anchor 價格從 t 到 t + h 個交易日的對數收益（h = 各推演期限的交易日數）。
             指標：
               命中率     分數絕對值 ≥ neutral_band 的日子裡，方向判斷對的比例；同時給出這些日子裡上漲的比例作為對照，
                          命中率要明顯高於「一直看多」或「一直看空」才算有用
               IC         分數與之後收益的秩相關（Spearman）
               有效樣本   n × min(1, step / h)。每天評估、看 21 天後的收益時，相鄰樣本高度重疊，
                          真正獨立的樣本只有約 n / 21。顯著性和置信區間都按有效樣本算
             信號、模組分數、綜合分數都評估。地緣模組不參與：事件的方向判斷沒有歷史版本，還原不出當時的狀態。
  價格區間   推演基準分支的一倍標準差區間（當前價格 ± 當前價格 × 日波動率 × √h），看實際價格落在區間內的比例，
             理想值約 68.3%。情境分支是人的假設，無法回測。

二、怎麼避免偷看未來（兩種模式）
  observation  按數據日期。假設每個數據在「它的日期 + 發佈延遲」那天就已知道（延遲按頻率，見 backtest.yaml）。
               指標的計算都只用過去的值，所以先在全部數據上算一次，再在每個評估日取「當時已知的最後一個值」，很快。
               局限：用的是今天看到的數值。月度、季度數據常被修訂，帶預測期的數據也還原不出當時的預測，
               用到它們的信號標為「近似」。
  vintage      按入庫時間。每個評估日都用 as_of 重建一次引擎，只看當時已經入庫的數據，完全沒有偷看。
               但只有系統持續收集了一段時間之後才有意義：事後一次性導入的歷史，在這個模式下當時什麼都還沒有。
  兩種模式的信號算法和 signals.py 完全一致（有測試保證）；之後的收益一律用今天的價格數據。
  anchor 和公允價值的目標若是期貨曲線上的連續合約，收益和波動率都用換月調整後的價格（contracts.py），
  換月當天兩個合約之間的價差不算作漲跌。

三、其他已知的局限
  - 換月日按規則推算（只認周一到周五），和數據商實際換月日不一致的那幾天仍可能有小的跳動。
  - 參數（模組權重等）按生效日期取值，但不按錄入時間還原。
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analytics, credentials, db, fair_value, projection, signals, stats_models
from .config import Config
from .indicators import IndicatorEngine
from .models import BacktestRun, Observation, iso_utc, utcnow
from .schema import HorizonDef

EXPECTED_COVERAGE = math.erf(1 / math.sqrt(2))          # 正態分佈一倍標準差內的概率，約 68.3%
DEFAULT_HORIZONS = [HorizonDef(code="W1", name_zh="1 周", calendar_days=7, trading_days=5),
                    HorizonDef(code="M1", name_zh="1 個月", calendar_days=30, trading_days=21),
                    HorizonDef(code="M3", name_zh="3 個月", calendar_days=91, trading_days=63)]
MODES = {"observation": "按數據日期", "vintage": "按入庫時間（嚴格）"}
DATA_MODULES = signals.DATA_MODULES
MAX_POINTS = 400                                        # 每個散點圖最多帶多少個點
STALE_AFTER = timedelta(hours=2)
log = logging.getLogger("crudelab.backtest")


class BacktestError(ValueError):
    """回測的設定不合理或數據不足。訊息可以直接給人看。"""


@dataclass(frozen=True)
class Options:
    mode: str = "observation"
    start: date | None = None
    end: date | None = None
    step: int | None = None          # 每隔幾個交易日評估一次；不給：按數據日期 1，按入庫時間 5

    def resolved_step(self) -> int:
        if self.step is not None:                     # 0 是非法值，要報錯，不能當成「沒填」
            return self.step
        return 1 if self.mode == "observation" else 5


def anchor_of(cfg: Config) -> str:
    return cfg.backtest.anchor or (cfg.projection.anchor if cfg.projection else "BRENT_M1")


def horizons_of(cfg: Config) -> list[HorizonDef]:
    return list(cfg.projection.horizons) if cfg.projection else DEFAULT_HORIZONS


# ------------------------------ 信號分數（向量化） ------------------------------

rolling_scores = signals.rolling_scores          # 算法只有一份，在 signals.py


def as_available(frame: pd.DataFrame, dates: pd.DatetimeIndex, lag_days: int) -> pd.DataFrame:
    """每個評估日 t 當時已知的最後一行：數據日期 ≤ t − lag_days。回傳以 t 為索引，另加 data_date 列。"""
    left = pd.DataFrame({"t": dates, "key": (dates - pd.Timedelta(days=lag_days)).astype("datetime64[ns]")})
    if frame.empty:
        return pd.DataFrame(index=dates, columns=[*frame.columns, "data_date"], dtype=float)
    right = frame.copy()
    right.index.name = "key"
    right = right.reset_index()
    right["key"] = right["key"].astype("datetime64[ns]")      # 兩邊的日期精度可能不同（秒 / 微秒），統一
    right["data_date"] = right["key"]
    merged = pd.merge_asof(left, right, on="key", direction="backward")
    return merged.drop(columns="key").set_index("t")


def _known_scores(cfg: Config, s, values: pd.Series, dates: pd.DatetimeIndex, lag: int) -> pd.Series:
    """每個評估日當時已知的分數：數據日期 ≤ 評估日 − lag 的最後一行；太舊不出分（和 signals.data_signal 一樣）。"""
    frame = rolling_scores(values, s.lookback_days, s.min_points, s.direction, s.detrend)
    known = as_available(frame, dates, lag)
    age = (known.index - pd.to_datetime(known["data_date"])).dt.days        # 沒有數據時是 NaN，比較結果為 False
    too_old = age > signals.stale_days(cfg, s.input)
    return known["score"].astype(float).where(~too_old)


def _observation_scores(engine: IndicatorEngine, cfg: Config, dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, set]:
    """按數據日期回測的分數。回傳（分數表, 完全按報告期還原的信號）。

    用到按期保存序列（機構預測）的信號：每個評估日用「當時已發佈的最新一期」重算輸入，
    不會用到後來的修訂（報告期就是發佈日期，不另加發佈延遲）。在這些序列第一期之前的評估日，
    沒有當時的數字，退回原來的近似算法；整個回測區間都有期記錄的信號才不算「近似」。
    """
    from .ingest import vintage_dates, vintaged_with_history
    out, exact = {}, set()
    engines: dict = {}
    for code, s in cfg.signals.items():
        if s.source == "fair_value":                 # 在 run() 裡按每個日期的擬合結果填（fair_value_scores）
            out[code] = pd.Series(np.nan, index=dates)
            continue
        lag = cfg.backtest.publication_lag_days[cfg.frequency_of(s.input)]
        approx = _known_scores(cfg, s, engine.values(s.input), dates, lag)
        deps = engine.dependencies(s.input)[0] if s.input in cfg.indicators else [s.input]
        vintaged = [d for d in deps if d in cfg.series and cfg.series[d].vintaged]
        history = vintaged_with_history(engine.session, vintaged)
        if not vintaged or set(history) != set(vintaged):
            out[code] = approx
            continue
        issues = pd.DatetimeIndex([pd.Timestamp(v) for v in vintage_dates(engine.session, vintaged)])
        first_covered = max(pd.Timestamp(engine.vintage_first(d)) for d in vintaged)
        scores = approx.copy()
        position = issues.searchsorted(dates, side="right") - 1             # 每個評估日當時最新的一期
        for k in sorted(set(position[(position >= 0) & (dates >= first_covered)])):
            v = issues[k].date()
            if v not in engines:
                engines[v] = IndicatorEngine(engine.session, cfg, vintage=v)
            mask = (position == k) & (dates >= first_covered)
            scores[mask] = _known_scores(cfg, s, engines[v].values(s.input), dates[mask], 0).to_numpy()
        out[code] = scores
        if dates[0] >= first_covered:
            exact.add(code)
    return pd.DataFrame(out, index=dates), exact


def fair_value_scores(cfg: Config, fits: dict[str, dict], dates: pd.DatetimeIndex) -> dict[str, pd.Series]:
    """公允價值信號在每個評估日的分數：用那一天的擬合（只用當時已知的數據），公式同 signals.fair_value_signal。"""
    out: dict[str, Any] = {}
    for code, s in cfg.signals.items():
        if s.source != "fair_value":
            continue
        by_date = fits.get(s.input, {})
        z = pd.Series({t: (fit.z if (fit := by_date.get(t)) else None) for t in dates}, dtype=float).reindex(dates)
        out[code] = (z * s.direction / 2).clip(-1, 1)
    return out


def _observation_volatility(prices: pd.Series, cfg: Config, dates: pd.DatetimeIndex,
                            method: str | None = None) -> pd.Series:
    """每個評估日當時算得出的日波動率：和推演在那一天算出的一樣（按 projection.yaml 的方法，或指定 method）。"""
    if cfg.projection is None:
        returns = np.log(prices[prices > 0]).diff().dropna()
        return returns.rolling(250, min_periods=60).std().reindex(dates, method="ffill")
    return stats_models.volatility_series(prices, cfg.projection, method).reindex(dates, method="ffill")


# ------------------------------ 信號分數（按入庫時間） ------------------------------

def _vintage(session: Session, cfg: Config, dates: pd.DatetimeIndex,
             anchor: str) -> tuple[pd.DataFrame, pd.Series, dict[str, dict]]:
    first_recorded = session.scalar(select(func.min(Observation.created_at)))
    if first_recorded is None or first_recorded.date() > dates[-1].date():
        raise BacktestError("按入庫時間回測需要系統已經持續收集了一段時間的數據。目前所選區間結束時還沒有任何數據入庫"
                            f"（最早的數據是 {first_recorded.date() if first_recorded else '—'} 才入庫的）。"
                            "回測事後導入的歷史，請改用「按數據日期」")
    if len(dates) > cfg.backtest.vintage_max_dates:
        raise BacktestError(f"按入庫時間回測要在每個評估日重算一遍，所選區間有 {len(dates)} 個評估日，"
                            f"超過上限 {cfg.backtest.vintage_max_dates}。請縮短區間或加大間隔")
    rows, vols = {}, {}
    fits: dict[str, dict] = {code: {} for code in cfg.fair_values}
    for t in dates:
        engine = IndicatorEngine(session, cfg, as_of=datetime.combine(t.date(), time(23, 59, 59)))
        rows[t] = {code: signals.data_signal(engine, code, t)["score"] for code in cfg.signals}
        for code, model in cfg.fair_values.items():         # 按入庫時間：數據庫裡當時有的就是已知的，不再扣延遲
            fits[code][t] = fair_value.fit_at(fair_value.aligned_frame(engine, cfg, model, until=t), t, model)
        prices = engine.adjusted_series(anchor)[0]
        prices = prices[prices.index <= t]
        settings = cfg.projection
        vols[t] = (stats_models.latest_volatility(prices, settings) if settings else
                   projection._volatility(prices, 250, 60)) if len(prices) else None
    scores = pd.DataFrame.from_dict(rows, orient="index").reindex(dates).astype(float)
    return scores, pd.Series(vols, dtype=float).reindex(dates), fits


# ------------------------------ 匯總：模組與綜合 ------------------------------

def aggregate(cfg: Config, engine: IndicatorEngine, scores: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """和 signals.compute 一樣的加權：模組內按信號 weight，模組間按參數權重（按生效日期）。地緣不參與。"""
    modules = {}
    for module in DATA_MODULES:
        codes = [c for c, s in cfg.signals.items() if s.module == module]
        if not codes:
            modules[module] = pd.Series(np.nan, index=scores.index)
            continue
        weights = signal_weights(cfg, engine, codes, scores.index)
        modules[module] = weighted_mean(scores[codes], weights)
    modules = pd.DataFrame(modules)
    weight_cols = {}
    for module in DATA_MODULES:
        key = cfg.projection.module_weights.get(module) if cfg.projection else None
        if key:
            values = engine.parameter_values(key)
            weight_cols[module] = (values.reindex(scores.index.union(values.index)).ffill().reindex(scores.index)
                                   if len(values) else pd.Series(np.nan, index=scores.index))
        else:
            weight_cols[module] = pd.Series(1.0, index=scores.index)
    weights = pd.DataFrame(weight_cols).fillna(0)
    present = modules.notna() & (weights > 0)
    total = weights.where(present, 0).sum(axis=1)
    overall = (modules.fillna(0) * weights.where(present, 0)).sum(axis=1).div(total.where(total > 0))
    return modules, overall


def _stepwise(engine: IndicatorEngine, key: str, index: pd.DatetimeIndex, default: float) -> pd.Series:
    """參數在每個日期當時生效的值（按生效日期的階梯函數）。"""
    values = engine.parameter_values(key)
    if not len(values):
        return pd.Series(default, index=index)
    return values.reindex(index.union(values.index)).ffill().reindex(index).fillna(default)


def signal_weights(cfg: Config, engine: IndicatorEngine, codes: list[str], index: pd.DatetimeIndex) -> pd.DataFrame:
    """每個信號在每個日期當時生效的權重（參數 W_{code}）。"""
    return pd.DataFrame({c: _stepwise(engine, cfg.signals[c].weight_parameter, index, cfg.signals[c].weight)
                         for c in codes}, index=index)


def weighted_mean(part: pd.DataFrame, weights: pd.DataFrame) -> pd.Series:
    """逐日加權平均，只算有分數的列；權重合計為 0 的日子沒有結果。"""
    w = weights.where(part.notna(), 0)
    total = w.sum(axis=1)
    return (part.fillna(0) * w).sum(axis=1).div(total.where(total > 0))


# ------------------------------ 評估 ------------------------------

def forward_returns(prices: pd.Series, dates: pd.DatetimeIndex, horizons: list[HorizonDef]) -> pd.DataFrame:
    """從 t 到 t 之後第 h 個交易日（按 anchor 自己的交易日）的對數收益。之後的數據不夠時為 NaN。"""
    prices = prices.sort_index()
    out: dict[str, Any] = {}
    for h in horizons:
        ahead = prices.shift(-h.trading_days)
        out[h.code] = np.log(ahead / prices).reindex(dates)
    return pd.DataFrame(out, index=dates)


def evaluate(scores: pd.Series, returns: pd.Series, horizon_days: int, step: int, band: float,
             min_effective: int) -> dict:
    data = pd.DataFrame({"s": scores, "r": returns}).dropna()
    n = len(data)
    n_eff = n * min(1.0, step / horizon_days)
    out: dict[str, Any] = {
           "n": n, "n_eff": round(n_eff, 1), "hit_rate": None, "hit_low": None, "hit_high": None, "calls": 0,
           "up_share": None, "ic": None, "ic_t": None, "mean_bull": None, "mean_bear": None,
           "significant": False, "status": "no_data" if n == 0 else ("insufficient" if n_eff < min_effective else "ok")}
    if n < 3:
        return out
    if data["s"].nunique() > 1 and data["r"].nunique() > 1:
        ic = float(data["s"].rank().corr(data["r"].rank()))
        out["ic"] = ic
        if n_eff > 2 and abs(ic) < 1:
            out["ic_t"] = ic * math.sqrt((n_eff - 2) / (1 - ic * ic))
    calls = data[(data["s"].abs() >= band) & (data["r"] != 0)]
    out["calls"] = len(calls)
    if len(calls):
        hit = float((np.sign(calls["s"]) == np.sign(calls["r"])).mean())
        calls_eff = len(calls) * min(1.0, step / horizon_days)
        half = 1.96 * math.sqrt(hit * (1 - hit) / calls_eff) if calls_eff > 0 else None
        out.update(hit_rate=hit, up_share=float((calls["r"] > 0).mean()),
                   hit_low=None if half is None else max(0.0, hit - half),
                   hit_high=None if half is None else min(1.0, hit + half))
    bull, bear = data[data["s"] >= band]["r"], data[data["s"] <= -band]["r"]
    out["mean_bull"] = float(bull.mean()) if len(bull) else None
    out["mean_bear"] = float(bear.mean()) if len(bear) else None
    out["significant"] = bool(out["status"] == "ok" and out["ic_t"] is not None and abs(out["ic_t"]) > 2)
    return out


def _band(prices: pd.Series, vol: pd.Series, dates: pd.DatetimeIndex, horizons: list[HorizonDef]) -> dict:
    prices = prices.sort_index()
    now = prices.reindex(dates)
    out: dict[str, Any] = {}
    for h in horizons:
        later = prices.shift(-h.trading_days).reindex(dates)
        half = now * vol * math.sqrt(h.trading_days)
        data = pd.DataFrame({"now": now, "later": later, "half": half}).dropna()
        n = len(data)
        if n == 0:
            out[h.code] = {"n": 0, "coverage": None, "above": None, "below": None}
            continue
        above = data["later"] > data["now"] + data["half"]
        below = data["later"] < data["now"] - data["half"]
        out[h.code] = {"n": n, "coverage": float((~above & ~below).mean()), "above": float(above.mean()),
                       "below": float(below.mean())}
    return out


def evaluate_drift(predicted: pd.Series, realized: pd.Series, horizon_days: int, step: int,
                   min_effective: int) -> dict:
    """推演的漂移準不準：和「不加漂移」（預測變化為 0）比，對數收益的平均絕對誤差減少了多少。

    改善的顯著性用兩種預測的誤差差（|實際| − |實際 − 漂移|）的均值除以標準誤，標準誤按有效樣本算。
    """
    data = pd.DataFrame({"d": predicted, "r": realized}).dropna()
    n = len(data)
    n_eff = n * min(1.0, step / horizon_days)
    out: dict[str, Any] = {"n": n, "n_eff": round(n_eff, 1), "hit_rate": None, "mae_drift": None, "mae_zero": None,
           "improvement": None, "t": None, "significant": False,
           "status": "no_data" if n == 0 else ("insufficient" if n_eff < min_effective else "ok")}
    if n < 3:
        return out
    moving = data[(data["d"] != 0) & (data["r"] != 0)]
    if len(moving):
        out["hit_rate"] = float((np.sign(moving["d"]) == np.sign(moving["r"])).mean())
    mae_drift, mae_zero = float((data["r"] - data["d"]).abs().mean()), float(data["r"].abs().mean())
    out.update(mae_drift=mae_drift, mae_zero=mae_zero, improvement=1 - mae_drift / mae_zero if mae_zero else None)
    gain = data["r"].abs() - (data["r"] - data["d"]).abs()
    if gain.std() > 0 and n_eff > 1:
        out["t"] = float(gain.mean() / (gain.std() / math.sqrt(n_eff)))
    out["significant"] = bool(out["status"] == "ok" and out["t"] is not None and out["t"] > 2)
    return out


def _evaluate_fair_value(cfg: Config, model, fits: dict, dates: pd.DatetimeIndex, fwd: pd.DataFrame,
                         horizons: list[HorizonDef], step: int, mode: str) -> tuple[dict, pd.Series]:
    """兩件事：偏離作為信號（偏高看空）準不準；推演的漂移有沒有讓中樞更準。"""
    settings = cfg.backtest
    z = pd.Series({t: fits[t].z if fits.get(t) else None for t in dates}, dtype=float).reindex(dates)
    score = (-z / 2).clip(-1, 1)
    status = pd.Series({t: fits[t].status if fits.get(t) else "missing" for t in dates}).reindex(dates)
    drift: dict[str, Any] = {}
    for h in horizons:
        predicted: dict[str, Any] = {}
        for t in dates:
            fit = fits.get(t)
            if fit is None or not fit.price:
                predicted[t] = None
                continue
            effect = fair_value.drift_effect(fit, model, fit.price, h.trading_days)
            predicted[t] = math.log((fit.price + effect) / fit.price) if effect is not None else None
        drift[h.code] = evaluate_drift(pd.Series(predicted, dtype=float).reindex(dates), fwd[h.code], h.trading_days,
                                       step, settings.min_effective_samples)
    counts = status.value_counts()
    return {"code": model.code, "name_zh": model.name_zh, "target": model.target,
            "approx": mode == "observation" and any(cfg.frequency_of(f.input) in ("monthly", "quarterly")
                                                    for f in model.factors),
            "coverage": {"ok": int(counts.get("ok", 0)), "weak": int(counts.get("weak", 0)),
                         "unavailable": int(len(dates) - counts.get("ok", 0) - counts.get("weak", 0))},
            "by_horizon": {h.code: evaluate(score, fwd[h.code], h.trading_days, step, settings.neutral_band,
                                            settings.min_effective_samples) for h in horizons},
            "drift": drift}, score


# ------------------------------ 關係模型的樣本外檢驗（#50） ------------------------------
#
# 關係模型回答「按現在的 x，y 通常在什麼水平」。頁面上的擬合優度是全樣本的，看不出拿過去擬合、預測之後準不準。
# 這裡在每個評估日 t：只用 t − 發佈延遲 之前、回看窗口內的樣本擬合（和頁面同一個 OLS），
# 再看 t 之後 h 天內最後一個配對點 (x', y')：
#   預測誤差   y' − (a + b·x')          （知道 x' 時，關係能不能說出 y'）
#   兩個基準   y' − 窗口均值、y' − 當時的 y（「沒有關係」和「不會變」）
#   偏離回歸   當時 y 高於擬合值的幅度，和之後 y 的變化方向是否相反（頁面上「偏高 / 偏低」的含義是否成立）
# 穩定性看每次重擬合的斜率：方向是否一致、相對波動多大。

RELATION_SPECS = (("ols", "linear"), ("huber", "linear"), ("ols", "hinge"))   # 回測裡並排比較的寫法
SPEC_NAMES = {("ols", "linear"): "直線", ("huber", "linear"): "直線（穩健）", ("ols", "hinge"): "帶轉折點",
              ("huber", "hinge"): "帶轉折點（穩健）"}


def evaluate_relation(cfg: Config, engine: IndicatorEngine, code: str, dates: pd.DatetimeIndex,
                      horizons: list[HorizonDef], step: int, mode: str, spec: tuple[str, str] | None = None) -> dict:
    """spec = (method, shape)：不給就用配置裡的寫法；給了就換成這種寫法檢驗（用來並排比較）。"""
    rel = cfg.relations[code]
    method, shape = spec or (rel.method, rel.shape)
    settings = cfg.backtest
    pairs = analytics.aligned_pairs(engine.values(rel.y), engine.values(rel.x), lag_days=rel.lag_days)
    freqs = {cfg.frequency_of(rel.x), cfg.frequency_of(rel.y)}
    lag = max(settings.publication_lag_days[f] for f in freqs)
    rows: dict[str, list[tuple]] = {h.code: [] for h in horizons}
    refits = []
    index = pairs.index
    for t in dates:
        known = pairs[index <= t - pd.Timedelta(days=lag)]
        if known.empty:
            continue
        window = known[known.index >= known.index[-1] - pd.Timedelta(days=rel.lookback_days)]
        model = analytics.fit_relation(window, method, shape) if len(window) >= rel.min_points else None
        if model is None:
            continue
        refits.append((model.slope, model.r2))
        y_now = float(window["y"].iloc[-1])
        residual = y_now - float(model.predict(window["x"].iloc[-1]))
        for h in horizons:
            ahead = pairs[(index > window.index[-1]) & (index > t) & (index <= t + pd.Timedelta(days=h.calendar_days))]
            if ahead.empty:
                continue
            x1, y1 = float(ahead["x"].iloc[-1]), float(ahead["y"].iloc[-1])
            rows[h.code].append((y1 - float(model.predict(x1)), y1 - float(window["y"].mean()), y1 - y_now,
                                 -residual, y1 - y_now))

    def summarize(h: HorizonDef) -> dict:
        data = np.array(rows[h.code], dtype=float).reshape(-1, 5)
        n = len(data)
        n_eff = n * min(1.0, step / h.trading_days)
        out: dict[str, Any] = {"n": n, "n_eff": round(n_eff, 1), "rmse": None, "rmse_mean": None, "rmse_persist": None,
               "skill_vs_mean": None, "skill_vs_persist": None, "reversion_ic": None, "reversion_t": None,
               "status": "no_data" if n == 0 else ("insufficient" if n_eff < settings.min_effective_samples else "ok")}
        if n < 3:
            return out
        sse = (data[:, :3] ** 2).sum(axis=0)
        out.update(rmse=float(np.sqrt(sse[0] / n)), rmse_mean=float(np.sqrt(sse[1] / n)),
                   rmse_persist=float(np.sqrt(sse[2] / n)),
                   skill_vs_mean=float(1 - sse[0] / sse[1]) if sse[1] > 0 else None,
                   skill_vs_persist=float(1 - sse[0] / sse[2]) if sse[2] > 0 else None)
        signal, change = pd.Series(data[:, 3]), pd.Series(data[:, 4])
        if signal.nunique() > 1 and change.nunique() > 1:
            ic = float(signal.rank().corr(change.rank()))
            out["reversion_ic"] = ic
            if n_eff > 2 and abs(ic) < 1:
                out["reversion_t"] = ic * math.sqrt((n_eff - 2) / (1 - ic * ic))
        return out

    slopes = np.array([b for b, _ in refits])
    stability: dict[str, Any] = {"refits": len(refits), "slope_median": None, "same_sign": None, "slope_cv": None,
                                 "r2_median": None}
    if len(slopes):
        median = float(np.median(slopes))
        stability.update(slope_median=median, r2_median=float(np.median([r for _, r in refits])),
                         same_sign=float((np.sign(slopes) == np.sign(median)).mean()) if median else None,
                         slope_cv=float(slopes.std() / abs(slopes.mean())) if slopes.mean() else None)
    return {"code": code, "name_zh": rel.name_zh, "y_name": cfg.name_of(rel.y), "x_name": cfg.name_of(rel.x),
            "y_unit": cfg.unit_of(rel.y), "lag_days": lag, "x_lead_days": rel.lag_days,
            "method": method, "shape": shape, "spec_name": SPEC_NAMES[(method, shape)],
            "approx": mode == "observation" and bool(freqs & {"monthly", "quarterly"}),
            "by_horizon": {h.code: summarize(h) for h in horizons}, "stability": stability}


def _relation_with_alternatives(cfg: Config, engine: IndicatorEngine, code: str, dates: pd.DatetimeIndex,
                                horizons: list[HorizonDef], step: int, mode: str) -> dict:
    """配置的寫法，加上其他幾種寫法的樣本外結果並排：換寫法要有依據。"""
    rel = cfg.relations[code]
    main = evaluate_relation(cfg, engine, code, dates, horizons, step, mode)
    main["alternatives"] = [
        {k: v for k, v in evaluate_relation(cfg, engine, code, dates, horizons, step, mode, spec).items()
         if k in ("method", "shape", "spec_name", "by_horizon", "stability")}
        for spec in RELATION_SPECS if spec != (rel.method, rel.shape)]
    return main


# ------------------------------ 推演的統計模型（#27） ------------------------------

def _band_compare(cfg: Config, prices: pd.Series, dates: pd.DatetimeIndex, horizons: list[HorizonDef]) -> dict:
    """兩種波動率給出的區間並排：覆蓋率（理想約 68.3%）和評分（負對數似然，越小越好）。"""
    if cfg.projection is None:
        return {}
    prices = prices.sort_index()
    out: dict[str, Any] = {"current": cfg.projection.volatility_method, "methods": {}}
    for method in ("historical", "ewma"):
        vol = _observation_volatility(prices, cfg, dates, method)
        band = _band(prices, vol, dates, horizons)
        now = prices.reindex(dates)
        for h in horizons:
            realized = np.log(prices.shift(-h.trading_days).reindex(dates) / now)
            band[h.code]["score"] = stats_models.gaussian_score(realized, vol * math.sqrt(h.trading_days))
        out["methods"][method] = band
    better: dict[str, Any] = {}
    for h in horizons:
        scores = {m: out["methods"][m][h.code]["score"] for m in out["methods"]}
        known = {m: v for m, v in scores.items() if v is not None}
        better[h.code] = min(known, key=lambda m: known[m]) if len(known) == 2 else None
    out["better"] = better
    return out


def _evaluate_stat_drift(cfg: Config, model, prices: pd.Series, dates: pd.DatetimeIndex, fwd: pd.DataFrame,
                         horizons: list[HorizonDef], step: int) -> dict:
    """統計漂移模型：每個評估日只用當時已實現的樣本估計（見 stats_models.momentum_drift），和「不加漂移」比。"""
    by_horizon = {}
    for h in horizons:
        drift = stats_models.momentum_drift(prices, model, h.trading_days)["drift"].reindex(dates)
        stats = evaluate_drift(drift, fwd[h.code], h.trading_days, step, cfg.backtest.min_effective_samples)
        stats["active_share"] = float((drift.dropna() != 0).mean()) if drift.notna().any() else None
        by_horizon[h.code] = stats
    gate = by_horizon.get("M1") or {}
    return {"code": model.code, "name_zh": model.name_zh, "kind": model.kind, "by_horizon": by_horizon,
            "passed": bool(gate.get("significant") and (gate.get("improvement") or 0) > 0)}


def _points(scores: pd.Series, returns: pd.Series) -> dict:
    data = pd.DataFrame({"s": scores, "r": returns}).dropna()
    if len(data) > MAX_POINTS:
        data = data.iloc[:: math.ceil(len(data) / MAX_POINTS)]
    fit: dict[str, Any] = {"slope": None, "intercept": None}
    if len(data) >= 3 and data["s"].std() > 0:
        slope, intercept = np.polyfit(data["s"], data["r"] * 100, 1)
        fit = {"slope": float(slope), "intercept": float(intercept)}
    return {"points": [[ts.date().isoformat(), float(r.s), float(r.r * 100)] for ts, r in data.iterrows()], **fit}


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# ------------------------------ 主流程 ------------------------------

def run(session: Session, cfg: Config, options: Options) -> dict:
    step = options.resolved_step()
    anchor, horizons, settings = anchor_of(cfg), horizons_of(cfg), cfg.backtest
    engine = IndicatorEngine(session, cfg)
    prices, unadjusted = engine.adjusted_series(anchor)
    dates = _evaluation_dates(cfg, options, anchor, prices, step)
    scores, vol, fits, exact = _scores(session, cfg, engine, options.mode, dates, anchor, prices)
    modules, overall = aggregate(cfg, engine, scores)
    fwd = forward_returns(prices, dates, horizons)

    def by_horizon(series: pd.Series) -> dict:
        return {h.code: evaluate(series, fwd[h.code], h.trading_days, step, settings.neutral_band,
                                 settings.min_effective_samples) for h in horizons}

    signal_rows = _signal_rows(cfg, engine, options.mode, scores, exact, by_horizon)
    module_rows = [{"module": m, "name_zh": signals.MODULE_NAMES[m], "scored": int(modules[m].notna().sum()),
                    "by_horizon": by_horizon(modules[m])} for m in DATA_MODULES]
    points = {code: {h.code: _points(scores[code], fwd[h.code]) for h in horizons} for code in cfg.signals}
    points["OVERALL"] = {h.code: _points(overall, fwd[h.code]) for h in horizons}
    fair_rows = []
    for code, model in cfg.fair_values.items():
        target = (engine.adjusted_series(model.target)[0] if model.target in cfg.series
                  else engine.values(model.target).dropna())
        target_fwd = forward_returns(target, dates, horizons) if len(target) > 1 else fwd * np.nan
        row, score = _evaluate_fair_value(cfg, model, fits[code], dates, target_fwd, horizons, step, options.mode)
        fair_rows.append(row)
        points[f"FV:{code}"] = {h.code: _points(score, target_fwd[h.code]) for h in horizons}
    calibration = calibrate(cfg, engine, scores, fwd, horizons, step)
    weekly = dates[:: max(1, 5 // step)]
    return _clean({
        "mode": options.mode, "mode_name": MODES[options.mode], "anchor": anchor, "anchor_name": cfg.name_of(anchor),
        "start": dates[0].date().isoformat(), "end": dates[-1].date().isoformat(), "step": step,
        "dates": len(dates), "neutral_band": settings.neutral_band,
        "min_effective_samples": settings.min_effective_samples,
        "publication_lag_days": settings.publication_lag_days,
        "horizons": [h.model_dump() for h in horizons],
        "signals": signal_rows, "modules": module_rows,
        "overall": {"scored": int(overall.notna().sum()), "by_horizon": by_horizon(overall)},
        "fair_values": fair_rows,
        "band_compare": _band_compare(cfg, prices, dates, horizons),
        "drift_models": {m.code: _evaluate_stat_drift(cfg, m, prices, dates, fwd, horizons, step)
                         for m in (cfg.projection.drift_models if cfg.projection else [])},
        "relations": [_relation_with_alternatives(cfg, engine, code, dates, horizons, step, options.mode)
                      for code in cfg.relations],
        "calibration": calibration,
        "band": {"expected": EXPECTED_COVERAGE, "by_horizon": _band(prices, vol, dates, horizons)},
        "points": points,
        "series": {"dates": [d.date().isoformat() for d in weekly],
                   "price": [float(prices.get(d, np.nan)) for d in weekly],
                   "overall": [float(overall.get(d, np.nan)) for d in weekly]},
        "caveats": _caveats(cfg, engine, options.mode, anchor, unadjusted)})


def _evaluation_dates(cfg: Config, options: Options, anchor: str, prices: pd.Series, step: int) -> pd.DatetimeIndex:
    """檢查參數，回傳評估日（錨定價格有數據的交易日，每隔 step 個取一個）。"""
    if options.mode not in MODES:
        raise BacktestError(f"回測模式必須是：{'、'.join(MODES)}")
    if not 1 <= step <= 60:
        raise BacktestError("評估間隔必須在 1 到 60 個交易日之間")
    if not cfg.signals:
        raise BacktestError("還沒有定義任何信號")
    if len(prices) < 2:
        raise BacktestError(f"{cfg.name_of(anchor)}（{anchor}）沒有足夠的價格數據，無法回測")
    days = prices.index
    if options.start:
        days = days[days >= pd.Timestamp(options.start)]
    if options.end:
        days = days[days <= pd.Timestamp(options.end)]
    if options.start and options.end and options.start >= options.end:
        raise BacktestError("開始日期必須早於結束日期")
    dates = days[::step]
    if len(dates) < 2:
        raise BacktestError("所選區間裡的交易日太少")
    return dates


def _scores(session: Session, cfg: Config, engine: IndicatorEngine, mode: str, dates: pd.DatetimeIndex,
            anchor: str, prices: pd.Series) -> tuple[dict[str, pd.Series], pd.Series, dict, set]:
    """每個信號在各評估日的分數、各評估日的波動率、各公允價值模型在各評估日的擬合，以及「嚴格按期」的信號。"""
    if mode != "observation":
        scores, vol, fits = _vintage(session, cfg, dates, anchor)
        return scores, vol, fits, set()
    scores, exact = _observation_scores(engine, cfg, dates)
    vol = _observation_volatility(prices, cfg, dates)
    fits = {}
    for code, model in cfg.fair_values.items():             # 同一個 fit_at：每個日期只用之前的數據擬合
        frame = fair_value.aligned_frame(engine, cfg, model, lags=cfg.backtest.publication_lag_days)
        fits[code] = {t: fair_value.fit_at(frame, t, model) for t in dates}
    scores.update(fair_value_scores(cfg, fits, dates))
    return scores, vol, fits, exact


def _signal_rows(cfg: Config, engine: IndicatorEngine, mode: str, scores: dict[str, pd.Series], exact: set,
                 by_horizon) -> list[dict[str, Any]]:
    """每個信號一行：各期限的表現，是否近似（用到修訂過的月度數據），以及輸入有沒有明顯趨勢。"""
    coarse = {"monthly", "quarterly"}
    rows: list[dict[str, Any]] = []
    for code, s in cfg.signals.items():
        fv = cfg.fair_values.get(s.input) if s.source == "fair_value" else None
        approx_inputs = [f.input for f in fv.factors] if fv else [s.input]
        row: dict[str, Any] = {
            "code": code, "name_zh": s.name_zh, "module": s.module, "input": s.input, "source": s.source,
            "input_name": fv.name_zh if fv else cfg.name_of(s.input), "direction": s.direction,
            "approx": mode == "observation" and code not in exact
            and any(cfg.frequency_of(i) in coarse for i in approx_inputs),
            "vintage_exact": code in exact,
            "scored": int(scores[code].notna().sum()), "by_horizon": by_horizon(scores[code])}
        if s.source == "fair_value":
            row.update(detrend="none", trend_t=None, trending_share=None)
        else:                                       # 輸入有沒有明顯趨勢：最近一個評估日、回看期內斜率的 t 值
            frame = signals.rolling_scores(engine.values(s.input), s.lookback_days, s.min_points, s.direction,
                                           s.detrend)
            trend = frame["trend_t"].dropna()
            row.update(detrend=s.detrend, trend_t=float(trend.iloc[-1]) if len(trend) else None,
                       trending_share=float((trend.abs() > 2).mean()) if len(trend) else None)
        rows.append(row)
    return rows


def _caveats(cfg: Config, engine: IndicatorEngine, mode: str, anchor: str, unadjusted: int) -> list[str]:
    settings = cfg.backtest
    caveats = [
        (f"{cfg.name_of(anchor)} 的收益已按換月調整；有 {unadjusted} 次換月因缺遠月數據沒能調整，那幾天含跳動。"
         if unadjusted else f"{cfg.name_of(anchor)} 的收益已按換月調整（同一合約對比）。")
        if engine.rolled_curves(anchor) else f"{cfg.name_of(anchor)} 不是期貨曲線上的合約，收益按原始價格計算。",
        "評估日之間的收益窗口互相重疊，統計按「有效樣本」計算；有效樣本少於 "
        f"{settings.min_effective_samples} 的結果標為樣本不足，不要當結論用。",
        "地緣模組不參與回測：事件的方向判斷沒有歷史版本。綜合分數只由"
        + "、".join(signals.MODULE_NAMES[m] for m in DATA_MODULES) + "這幾個模組構成。",
        "價格區間只檢驗推演的基準分支；情境分支是人的假設，無法回測。",
        "公允價值的漂移假設因子在期限內不變；它的回測檢驗的正是這個假設下，加漂移是否讓推演的中樞更準。",
    ]
    if mode == "observation":
        caveats.insert(0, "按數據日期回測：假設每個數據在「日期 + 發佈延遲」那天就已知道，用的是今天看到的數值。"
                          "月度、季度數據可能被修訂過，帶預測期的數據也還原不出當時的預測，"
                          "用到它們的信號標為「近似」，結果可能偏樂觀。")
    return caveats


# ------------------------------ 權重校準 ------------------------------
#
# 用回測裡的預測力（信號分數和之後收益的秩相關 IC）給權重：
#   樣本不足                  → 保留現在的權重（沒有依據就不動）
#   IC 顯著為正（t ≥ 2）       → 權重 = IC / IC_REFERENCE，限制在 [MIN_WEIGHT, WEIGHT_CAP]（IC 0.1 ≈ 權重 1）
#   IC 顯著為負（t ≤ −2）      → 0：方向和實際相反。只建議停用，不自動翻轉方向（方向是人的判斷，見信號配置）
#   不顯著                    → 0：歷史上沒有預測力，不參與合成
# 模組權重用同樣的規則，看「按建議的信號權重合成的模組分數」的 IC。
#
# 建議值用到今天為止的全部回測樣本。它好不好，用「逐步前推」檢驗：每月初只用當時已經實現的收益
# （評估日 + 期限 ≤ 當天）重新算一次權重，用到下個月初，這樣得到的綜合分數沒有用到未來；再和現在的權重比。

IC_REFERENCE = 0.10
MIN_WEIGHT, WEIGHT_CAP = 0.25, 3.0


def proposed_weight(stats: dict, current: float) -> tuple[float, str, str]:
    """（建議權重, 理由, 結論代碼）。stats 是 evaluate() 的結果。"""
    if stats.get("status") != "ok" or stats.get("ic_t") is None:
        return current, "樣本不足：保留現在的權重", "insufficient"
    if stats["ic_t"] >= 2:
        return (round(min(WEIGHT_CAP, max(MIN_WEIGHT, stats["ic"] / IC_REFERENCE)), 2),
                "有預測力：按 IC 給權重", "effective")
    if stats["ic_t"] <= -2:
        return 0.0, "方向和實際相反：建議停用，並核對方向", "reversed"
    return 0.0, "沒有顯著預測力：建議停用", "weak"


def _modules_from(cfg: Config, scores: pd.DataFrame, weights: pd.DataFrame) -> pd.DataFrame:
    """按給定的信號權重合成各模組分數。"""
    out: dict[str, Any] = {}
    for m in DATA_MODULES:
        codes = [c for c, s in cfg.signals.items() if s.module == m]
        out[m] = weighted_mean(scores[codes], weights[codes]) if codes else pd.Series(np.nan, index=scores.index)
    return pd.DataFrame(out, index=scores.index)


def calibrate(cfg: Config, engine: IndicatorEngine, scores: pd.DataFrame, fwd: pd.DataFrame,
              horizons: list[HorizonDef], step: int) -> dict:
    settings = cfg.backtest
    horizon = next((h for h in horizons if h.code == settings.calibration_horizon), horizons[-1])
    returns = fwd[horizon.code]
    ev = lambda series, ret=returns: evaluate(series, ret, horizon.trading_days, step,      # noqa: E731
                                              settings.neutral_band, settings.min_effective_samples)
    dates = scores.index
    codes = list(cfg.signals)
    current_sig = signal_weights(cfg, engine, codes, dates)
    current_mod = pd.DataFrame({m: _stepwise(engine, cfg.projection.module_weights[m], dates, 1.0)
                                if cfg.projection and cfg.projection.module_weights.get(m) else
                                pd.Series(1.0, index=dates) for m in DATA_MODULES}, index=dates)

    def propose(upto: int | None) -> tuple[dict[str, float], dict[str, float], dict, dict]:
        """用前 upto 個評估日（None = 全部）的樣本算建議權重。"""
        part = slice(None, upto)
        at = dates[(upto or len(dates)) - 1]
        sig, sig_rows = {}, {}
        for c in codes:
            stats = evaluate(scores[c].iloc[part], returns.iloc[part], horizon.trading_days, step,
                             settings.neutral_band, settings.min_effective_samples)
            sig[c], reason, verdict = proposed_weight(stats, float(current_sig[c].loc[at]))
            sig_rows[c] = {"stats": stats, "reason": reason, "verdict": verdict}
        modules = _modules_from(cfg, scores, pd.DataFrame({c: pd.Series(sig[c], index=dates) for c in codes}))
        mod: dict[str, float] = {}
        mod_rows: dict[str, dict] = {}
        for m in DATA_MODULES:
            stats = evaluate(modules[m].iloc[part], returns.iloc[part], horizon.trading_days, step,
                             settings.neutral_band, settings.min_effective_samples)
            mod[m], reason, verdict = proposed_weight(stats, float(current_mod[m].loc[at]))
            mod_rows[m] = {"stats": stats, "reason": reason, "verdict": verdict}
        return sig, mod, sig_rows, mod_rows

    sig, mod, sig_rows, mod_rows = propose(None)
    last = dates[-1]
    signal_out = [{"code": c, "name_zh": cfg.signals[c].name_zh, "module": cfg.signals[c].module,
                   "parameter": cfg.signals[c].weight_parameter, "current": float(current_sig[c].loc[last]),
                   "proposed": sig[c], **sig_rows[c]} for c in codes]
    module_out = [{"module": m, "name_zh": signals.MODULE_NAMES[m],
                   "parameter": cfg.projection.module_weights.get(m) if cfg.projection else None,
                   "current": float(current_mod[m].loc[last]), "proposed": mod[m], **mod_rows[m]}
                  for m in DATA_MODULES]

    # 逐步前推：每月第一個評估日重算，只用已經實現的收益
    gap = math.ceil(horizon.trading_days / step)
    starts = [i for i in range(1, len(dates)) if dates[i].month != dates[i - 1].month and i - gap > 0]
    wf_sig = pd.DataFrame(np.nan, index=dates, columns=codes)
    wf_mod = pd.DataFrame(np.nan, index=dates, columns=list(DATA_MODULES))
    s_w: dict[str, float] = {}
    m_w: dict[str, float] = {}
    for k, i in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(dates)
        s_w, m_w, _, _ = propose(i - gap + 1)
        wf_sig.iloc[i:end] = [s_w[c] for c in codes]
        wf_mod.iloc[i:end] = [m_w[m] for m in DATA_MODULES]
    walk = None
    if starts:
        first = dates[starts[0]]
        wf_modules = _modules_from(cfg, scores, wf_sig.fillna(0))
        calibrated = weighted_mean(wf_modules, wf_mod.fillna(0))
        cur_modules = _modules_from(cfg, scores, current_sig)
        current = weighted_mean(cur_modules, current_mod)
        window = dates >= first
        walk = {"start": first.date().isoformat(), "refits": len(starts), "last_weights": {**s_w, **m_w},
                "current": ev(current[window], returns[window]), "calibrated": ev(calibrated[window], returns[window])}
        cur_ic, cal_ic = walk["current"]["ic"], walk["calibrated"]["ic"]
        walk["better"] = None if cur_ic is None or cal_ic is None else bool(cal_ic > cur_ic)
    return {"horizon": horizon.code, "horizon_name": horizon.name_zh, "ic_reference": IC_REFERENCE,
            "signals": signal_out, "modules": module_out, "walk_forward": walk}


# ------------------------------ 記錄與後台執行 ------------------------------

def create(session: Session, options: Options, actor: str) -> BacktestRun:
    if options.mode not in MODES:
        raise BacktestError(f"回測模式必須是：{'、'.join(MODES)}")
    row = BacktestRun(created_by=actor, status="running", mode=options.mode, start_date=options.start,
                      end_date=options.end, step=options.resolved_step())
    session.add(row)
    session.flush()
    return row


def execute(cfg: Config, run_id: int) -> str:
    """執行一個已建立的回測並記下結果。任何異常都被接住。"""
    with db.session_scope() as session:
        row = db.require(session, BacktestRun, run_id)
        options = Options(row.mode, row.start_date, row.end_date, row.step)
    try:
        with db.session_scope() as session:
            results = run(session, cfg, options)
        status, message = "done", None
    except BacktestError as exc:
        results, status, message = None, "failed", str(exc)
    except Exception as exc:
        log.exception("回測 %s 出錯", run_id)
        results, status, message = None, "failed", f"未預期的錯誤（{type(exc).__name__}），詳情見服務器日誌"
    with db.session_scope() as session:
        row = db.require(session, BacktestRun, run_id)
        row.status, row.results, row.finished_at = status, results, utcnow()
        row.message = credentials.redact(message) if message else None
    return status


def start_in_background(cfg: Config, run_id: int) -> None:
    """在後台線程裡執行，接口立刻返回。頁面輪詢結果。"""
    threading.Thread(target=execute, args=(cfg, run_id), name=f"backtest-{run_id}", daemon=True).start()


def mark_stale(session: Session) -> None:
    """執行中太久的（通常是進程在回測途中被關掉了）改成失敗。"""
    for row in session.scalars(select(BacktestRun).where(BacktestRun.status == "running",
                                                         BacktestRun.created_at < utcnow() - STALE_AFTER)):
        row.status, row.finished_at, row.message = "failed", utcnow(), "執行中斷：程序在回測完成前被關閉了"


# ------------------------------ 信號的歷史表現（給推演頁） ------------------------------

TRACK_RECORD_OLD_DAYS = 30
VERDICTS = {
    "effective": "有效",          # IC 顯著為正：信號高的時候，之後確實更常漲
    "reversed": "方向相反",       # IC 顯著為負：信號和之後的走勢顯著反著來，方向可能設錯了
    "weak": "不顯著",             # 樣本夠，但看不出關係
    "insufficient": "樣本不足",   # 有效樣本太少，不能下結論
    "none": "沒有結果",           # 這次回測沒有這個信號，或一次都沒打出分
    "changed": "定義已改",        # 信號的輸入或方向在回測之後改過，結果不再對應現在的信號
}


def verdict(stats: dict | None) -> str:
    """一個信號在某個期限上的回測結論。規則只寫在這裡，頁面只顯示。"""
    if not stats or stats.get("status") == "no_data":
        return "none"
    if stats.get("status") == "insufficient":
        return "insufficient"
    if stats.get("significant") and stats.get("ic") is not None:
        return "effective" if stats["ic"] > 0 else "reversed"
    return "weak"


def _compact(stats: dict | None, code: str) -> dict:
    stats = stats or {}
    keys = ("hit_rate", "up_share", "ic", "ic_t", "n_eff", "calls", "status")
    return {**{k: stats.get(k) for k in keys}, "verdict": code, "verdict_name": VERDICTS[code]}


def track_record(session: Session, cfg: Config, now: datetime | None = None) -> dict:
    """最近一次完成的回測裡，每個信號、每個模組、綜合分數在各期限上的表現和結論。沒有回測時 run 為 None。"""
    row = session.scalar(select(BacktestRun).where(BacktestRun.status == "done", BacktestRun.results.is_not(None))
                         .order_by(BacktestRun.id.desc()).limit(1))
    horizons = [{"code": h.code, "name_zh": h.name_zh} for h in horizons_of(cfg)]
    out: dict = {"run": None, "horizons": horizons, "signals": {}, "modules": {}, "overall": {}, "notes": []}
    if row is None:
        return out
    r: dict = row.results or {}
    age = ((now or utcnow()) - (row.finished_at or row.created_at)).days
    out["run"] = {"id": row.id, "mode": row.mode, "mode_name": MODES.get(row.mode, row.mode), "start": r["start"],
                  "end": r["end"], "finished_at": iso_utc(row.finished_at or row.created_at), "age_days": age,
                  "neutral_band": r.get("neutral_band")}
    if age > TRACK_RECORD_OLD_DAYS:
        out["notes"].append(f"這次回測是 {age} 天前做的，之後的數據沒有包括在內，可以到回測頁重新跑一次")
    codes = [h["code"] for h in horizons]
    past = {s["code"]: s for s in r.get("signals", [])}
    changed = []
    for code, s in cfg.signals.items():
        old = past.get(code)
        if old is not None and (old.get("input") != s.input or old.get("direction") != s.direction):
            changed.append(s.name_zh)
            out["signals"][code] = {h: _compact(None, "changed") for h in codes}
            continue
        by = (old or {}).get("by_horizon", {})
        out["signals"][code] = {h: _compact(by.get(h), verdict(by.get(h))) for h in codes}
    if changed:
        out["notes"].append(f"{'、'.join(changed)} 的定義在回測之後改過，要重新回測才有結果")
    for m in r.get("modules", []):
        out["modules"][m["module"]] = {h: _compact(m["by_horizon"].get(h), verdict(m["by_horizon"].get(h)))
                                       for h in codes}
    overall = r.get("overall", {}).get("by_horizon", {})
    out["overall"] = {h: _compact(overall.get(h), verdict(overall.get(h))) for h in codes}
    out["notes"].append("地緣模組沒有回測：事件的方向判斷沒有歷史版本")
    return out
