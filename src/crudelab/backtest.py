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

三、其他已知的局限
  - anchor 是連續合約，前向收益裡含換月當天的跳動。
  - 參數（模組權重等）按生效日期取值，但不按錄入時間還原。
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import credentials, db, projection, signals
from .config import Config
from .indicators import IndicatorEngine
from .models import BacktestRun, Observation, utcnow
from .schema import HorizonDef

EXPECTED_COVERAGE = math.erf(1 / math.sqrt(2))          # 正態分佈一倍標準差內的概率，約 68.3%
DEFAULT_HORIZONS = [HorizonDef(code="W1", name_zh="1 周", calendar_days=7, trading_days=5),
                    HorizonDef(code="M1", name_zh="1 個月", calendar_days=30, trading_days=21),
                    HorizonDef(code="M3", name_zh="3 個月", calendar_days=91, trading_days=63)]
MODES = {"observation": "按數據日期", "vintage": "按入庫時間（嚴格）"}
DATA_MODULES = ("spread", "balance", "refinery")
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

def rolling_scores(values: pd.Series, lookback_days: int, min_points: int, direction: int) -> pd.DataFrame:
    """每個數據日期上的信號分數：和 signals.data_signal 在那一天算出的完全一樣。

    signals.py 的回看窗口是 [最新日期 − lookback_days, 最新日期]，兩端都含，所以這裡用 closed="both"。
    """
    columns = ["value", "mean", "std", "n", "z", "score"]
    if values.empty:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([]))
    s = values.sort_index()
    window = s.rolling(f"{lookback_days}D", closed="both")
    mean, std, n = window.mean(), window.std(), window.count()
    usable = (n >= min_points) & (std > 0)
    z = ((s - mean) / std).where(usable)
    score = (z / 2).clip(-1, 1) * direction
    return pd.DataFrame({"value": s, "mean": mean, "std": std, "n": n, "z": z, "score": score})


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


def _observation_scores(engine: IndicatorEngine, cfg: Config, dates: pd.DatetimeIndex) -> pd.DataFrame:
    out = {}
    for code, s in cfg.signals.items():
        frame = rolling_scores(engine.values(s.input), s.lookback_days, s.min_points, s.direction)
        lag = cfg.backtest.publication_lag_days[cfg.frequency_of(s.input)]
        out[code] = as_available(frame, dates, lag)["score"].astype(float)
    return pd.DataFrame(out, index=dates)


def _observation_volatility(prices: pd.Series, cfg: Config, dates: pd.DatetimeIndex) -> pd.Series:
    """每個評估日當時算得出的日波動率：和 projection._volatility 在那一天算出的一樣。"""
    settings = cfg.projection
    lookback = settings.volatility_lookback if settings else 250
    min_points = settings.volatility_min_points if settings else 60
    returns = np.log(prices[prices > 0]).diff().dropna()
    vol = returns.rolling(lookback, min_periods=min_points).std()
    return vol.reindex(dates, method="ffill")


# ------------------------------ 信號分數（按入庫時間） ------------------------------

def _vintage(session: Session, cfg: Config, dates: pd.DatetimeIndex, anchor: str) -> tuple[pd.DataFrame, pd.Series]:
    first_recorded = session.scalar(select(func.min(Observation.created_at)))
    if first_recorded is None or first_recorded.date() > dates[-1].date():
        raise BacktestError("按入庫時間回測需要系統已經持續收集了一段時間的數據。目前所選區間結束時還沒有任何數據入庫"
                            f"（最早的數據是 {first_recorded.date() if first_recorded else '—'} 才入庫的）。"
                            "回測事後導入的歷史，請改用「按數據日期」")
    if len(dates) > cfg.backtest.vintage_max_dates:
        raise BacktestError(f"按入庫時間回測要在每個評估日重算一遍，所選區間有 {len(dates)} 個評估日，"
                            f"超過上限 {cfg.backtest.vintage_max_dates}。請縮短區間或加大間隔")
    rows, vols = {}, {}
    for t in dates:
        engine = IndicatorEngine(session, cfg, as_of=datetime.combine(t.date(), time(23, 59, 59)))
        rows[t] = {code: signals.data_signal(engine, code, t)["score"] for code in cfg.signals}
        prices = engine.series_values(anchor)
        prices = prices[prices.index <= t]
        settings = cfg.projection
        vols[t] = projection._volatility(prices, settings.volatility_lookback if settings else 250,
                                         settings.volatility_min_points if settings else 60) if len(prices) else None
    scores = pd.DataFrame.from_dict(rows, orient="index").reindex(dates).astype(float)
    return scores, pd.Series(vols, dtype=float).reindex(dates)


# ------------------------------ 匯總：模組與綜合 ------------------------------

def aggregate(cfg: Config, engine: IndicatorEngine, scores: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """和 signals.compute 一樣的加權：模組內按信號 weight，模組間按參數權重（按生效日期）。地緣不參與。"""
    modules = {}
    for module in DATA_MODULES:
        codes = [c for c, s in cfg.signals.items() if s.module == module]
        if not codes:
            modules[module] = pd.Series(np.nan, index=scores.index)
            continue
        weights = pd.Series({c: cfg.signals[c].weight for c in codes})
        part = scores[codes]
        present = part.notna()
        total = present.mul(weights, axis=1).sum(axis=1)
        modules[module] = part.fillna(0).mul(weights, axis=1).sum(axis=1).div(total.where(total > 0))
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


# ------------------------------ 評估 ------------------------------

def forward_returns(prices: pd.Series, dates: pd.DatetimeIndex, horizons: list[HorizonDef]) -> pd.DataFrame:
    """從 t 到 t 之後第 h 個交易日（按 anchor 自己的交易日）的對數收益。之後的數據不夠時為 NaN。"""
    prices = prices.sort_index()
    out = {}
    for h in horizons:
        ahead = prices.shift(-h.trading_days)
        out[h.code] = np.log(ahead / prices).reindex(dates)
    return pd.DataFrame(out, index=dates)


def evaluate(scores: pd.Series, returns: pd.Series, horizon_days: int, step: int, band: float,
             min_effective: int) -> dict:
    data = pd.DataFrame({"s": scores, "r": returns}).dropna()
    n = len(data)
    n_eff = n * min(1.0, step / horizon_days)
    out = {"n": n, "n_eff": round(n_eff, 1), "hit_rate": None, "hit_low": None, "hit_high": None, "calls": 0,
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
    out = {}
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


def _points(scores: pd.Series, returns: pd.Series) -> dict:
    data = pd.DataFrame({"s": scores, "r": returns}).dropna()
    if len(data) > MAX_POINTS:
        data = data.iloc[:: math.ceil(len(data) / MAX_POINTS)]
    fit = {"slope": None, "intercept": None}
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
    if options.mode not in MODES:
        raise BacktestError(f"回測模式必須是：{'、'.join(MODES)}")
    step = options.resolved_step()
    if not 1 <= step <= 60:
        raise BacktestError("評估間隔必須在 1 到 60 個交易日之間")
    if not cfg.signals:
        raise BacktestError("還沒有定義任何信號")
    anchor, horizons, settings = anchor_of(cfg), horizons_of(cfg), cfg.backtest
    engine = IndicatorEngine(session, cfg)
    prices = engine.series_values(anchor).dropna()
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

    if options.mode == "observation":
        scores = _observation_scores(engine, cfg, dates)
        vol = _observation_volatility(prices, cfg, dates)
    else:
        scores, vol = _vintage(session, cfg, dates, anchor)
    modules, overall = aggregate(cfg, engine, scores)
    fwd = forward_returns(prices, dates, horizons)

    def by_horizon(series: pd.Series) -> dict:
        return {h.code: evaluate(series, fwd[h.code], h.trading_days, step, settings.neutral_band,
                                 settings.min_effective_samples) for h in horizons}

    coarse = {"monthly", "quarterly"}
    signal_rows = []
    for code, s in cfg.signals.items():
        signal_rows.append({
            "code": code, "name_zh": s.name_zh, "module": s.module, "input": s.input,
            "input_name": cfg.name_of(s.input), "direction": s.direction,
            "approx": options.mode == "observation" and cfg.frequency_of(s.input) in coarse,
            "scored": int(scores[code].notna().sum()), "by_horizon": by_horizon(scores[code])})
    module_rows = [{"module": m, "name_zh": signals.MODULE_NAMES[m], "scored": int(modules[m].notna().sum()),
                    "by_horizon": by_horizon(modules[m])} for m in DATA_MODULES]

    points = {code: {h.code: _points(scores[code], fwd[h.code]) for h in horizons} for code in cfg.signals}
    points["OVERALL"] = {h.code: _points(overall, fwd[h.code]) for h in horizons}
    weekly = dates[:: max(1, 5 // step)]
    caveats = [
        f"{cfg.name_of(anchor)} 是連續合約，之後的收益裡含換月當天的跳動。",
        "評估日之間的收益窗口互相重疊，統計按「有效樣本」計算；有效樣本少於 "
        f"{settings.min_effective_samples} 的結果標為樣本不足，不要當結論用。",
        "地緣模組不參與回測：事件的方向判斷沒有歷史版本。綜合分數只由地域價差、供需、煉廠三個模組構成。",
        "價格區間只檢驗推演的基準分支；情境分支是人的假設，無法回測。",
    ]
    if options.mode == "observation":
        caveats.insert(0, "按數據日期回測：假設每個數據在「日期 + 發佈延遲」那天就已知道，用的是今天看到的數值。"
                          "月度、季度數據可能被修訂過，帶預測期的數據也還原不出當時的預測，"
                          "用到它們的信號標為「近似」，結果可能偏樂觀。")
    return _clean({
        "mode": options.mode, "mode_name": MODES[options.mode], "anchor": anchor, "anchor_name": cfg.name_of(anchor),
        "start": dates[0].date().isoformat(), "end": dates[-1].date().isoformat(), "step": step,
        "dates": len(dates), "neutral_band": settings.neutral_band,
        "min_effective_samples": settings.min_effective_samples,
        "publication_lag_days": settings.publication_lag_days,
        "horizons": [h.model_dump() for h in horizons],
        "signals": signal_rows, "modules": module_rows,
        "overall": {"scored": int(overall.notna().sum()), "by_horizon": by_horizon(overall)},
        "band": {"expected": EXPECTED_COVERAGE, "by_horizon": _band(prices, vol, dates, horizons)},
        "points": points,
        "series": {"dates": [d.date().isoformat() for d in weekly],
                   "price": [float(prices.get(d, np.nan)) for d in weekly],
                   "overall": [float(overall.get(d, np.nan)) for d in weekly]},
        "caveats": caveats})


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
        row = session.get(BacktestRun, run_id)
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
        row = session.get(BacktestRun, run_id)
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
