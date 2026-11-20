"""關係模型的樣本外檢驗（#50）：只用當時之前的樣本擬合，看對之後的預測誤差、偏離回歸和斜率穩定性。"""
import dataclasses

import numpy as np
import pandas as pd

from crudelab import backtest
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.schema import RelationDef

X, Y = "EIA_CRUDE_STOCKS", "EIA_CUSHING_STOCKS"       # 兩條周度序列，當作 x、y
FRIDAYS = pd.date_range("2019-01-04", periods=360, freq="W-FRI")


def setup(session, cfg, y_of_x):
    rng = np.random.default_rng(7)
    x = 430_000 + np.cumsum(rng.normal(0, 3_000, len(FRIDAYS)))
    y = y_of_x(x, rng)
    write_observations(session, [Obs(c, d.date(), float(v)) for d, a, b in zip(FRIDAYS, x, y, strict=True)
                                 for c, v in ((X, a), (Y, b))], "manual")
    session.commit()
    rel = RelationDef(code="R", name_zh="r", y=Y, x=X, lookback_days=730, min_points=40)
    small = dataclasses.replace(cfg, relations={"R": rel})
    dates = pd.bdate_range("2021-06-01", "2025-12-31")[::5]
    return backtest.evaluate_relation(small, IndicatorEngine(session, small), "R", dates,
                                      backtest.horizons_of(small), 5, "observation")


def test_stable_relation_beats_both_baselines(session, cfg):
    out = setup(session, cfg, lambda x, rng: 0.1 * x + rng.normal(0, 300, len(x)))
    m = out["by_horizon"]["M1"]
    assert m["status"] == "ok" and m["skill_vs_mean"] > 0.5 and m["skill_vs_persist"] > 0
    assert out["stability"]["same_sign"] == 1.0 and out["stability"]["r2_median"] > 0.8
    assert out["lag_days"] == 5 and not out["approx"]                 # 周度：發佈延遲 5 天


def test_no_relation_is_not_better_than_the_mean(session, cfg):
    out = setup(session, cfg, lambda x, rng: 40_000 + rng.normal(0, 2_000, len(x)))
    m = out["by_horizon"]["M1"]
    assert m["skill_vs_mean"] < 0.05                                 # 樣本外沒有比「均值」好
    assert out["stability"]["same_sign"] < 0.9 or out["stability"]["r2_median"] < 0.1


def test_mean_reverting_deviation_is_detected(session, cfg):
    def y_of_x(x, rng):
        dev = np.zeros(len(x))
        for i in range(1, len(x)):                                   # 偏離每周回吐一半
            dev[i] = 0.5 * dev[i - 1] + rng.normal(0, 1_000)
        return 0.1 * x + dev
    m = setup(session, cfg, y_of_x)["by_horizon"]["W1"]
    assert m["reversion_ic"] > 0.2 and m["reversion_t"] > 2


def test_fit_never_uses_data_after_the_evaluation_date(session, cfg, monkeypatch):
    """把 ols 包一層，記下每次擬合用到的最晚日期：必須早於（評估日 − 發佈延遲）。"""
    from crudelab import analytics
    seen, original = [], analytics.fit_relation

    def spy(frame, *args):
        seen.append(frame.index.max())
        return original(frame, *args)
    monkeypatch.setattr(analytics, "fit_relation", spy)
    out = setup(session, cfg, lambda x, rng: 0.1 * x + rng.normal(0, 300, len(x)))
    assert out["stability"]["refits"] == len(seen)
    dates = pd.bdate_range("2021-06-01", "2025-12-31")[::5]           # 和 setup 裡的評估日相同；樣本夠了之後每天都擬合
    assert all(last <= t - pd.Timedelta(days=5) for last, t in zip(seen, dates[-len(seen):], strict=True))
