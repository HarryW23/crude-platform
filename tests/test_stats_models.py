"""推演的統計模型（#27）：EWMA 波動率給區間、動量給漂移；每個日期只用當時已知的價格；選用統計漂移要先通過回測。"""
import math

import numpy as np
import pandas as pd
import pytest

from crudelab import backtest, projection, stats_models
from crudelab.models import BacktestRun
from crudelab.schema import StatDriftModel

DAYS = pd.bdate_range("2019-01-01", periods=1500)
MODEL = StatDriftModel(code="M", name_zh="m", lookback_days=63, window_days=756, min_points=120)


def prices_from(returns):
    return pd.Series(70 * np.exp(np.cumsum(returns)), index=DAYS[:len(returns)])


def test_ewma_reacts_faster_than_history(cfg):
    rng = np.random.default_rng(1)
    r = np.concatenate([rng.normal(0, 0.01, 600), rng.normal(0, 0.04, 30)])        # 突然變劇烈
    p = prices_from(r)
    hist = stats_models.historical_vol(p, 250, 60).iloc[-1]
    ewma = stats_models.ewma_vol(p, 0.94, 60).iloc[-1]
    assert ewma > 2 * hist and ewma == pytest.approx(0.04, rel=0.35)
    assert stats_models.ewma_vol(p.iloc[:30], 0.94, 60).isna().all()              # 樣本不夠：沒有值


def test_momentum_found_in_trending_prices_and_not_in_random_walk():
    rng = np.random.default_rng(2)
    regime = np.repeat(rng.choice([-1, 1], size=12), 125)[:1500]                  # 一段段持續的趨勢
    trending = prices_from(0.0015 * regime + rng.normal(0, 0.01, 1500))
    out = stats_models.momentum_drift(trending, MODEL, 21)
    assert out["t"].dropna().iloc[-1] > 2 and (out["drift"].dropna() != 0).mean() > 0.5
    walk = stats_models.momentum_drift(prices_from(rng.normal(0, 0.01, 1500)), MODEL, 21)
    assert (walk["drift"].dropna() == 0).mean() > 0.8                              # 沒有動量：大多不給漂移


def test_drift_never_uses_future_prices():
    rng = np.random.default_rng(3)
    p = prices_from(rng.normal(0, 0.01, 1500))
    i = 1000
    before = stats_models.momentum_drift(p, MODEL, 21).iloc[i]
    later = p.copy()
    later.iloc[i + 1:] *= np.exp(np.linspace(0, 2, len(later) - i - 1))           # 之後的價格完全改掉
    after = stats_models.momentum_drift(later, MODEL, 21).iloc[i]
    assert before.equals(after)


def test_gaussian_score_prefers_the_right_width():
    rng = np.random.default_rng(4)
    r = pd.Series(rng.normal(0, 0.02, 2000))
    right = stats_models.gaussian_score(r, pd.Series(0.02, index=r.index))
    assert right < stats_models.gaussian_score(r, pd.Series(0.005, index=r.index))   # 太窄
    assert right < stats_models.gaussian_score(r, pd.Series(0.08, index=r.index))    # 太寬


def test_stat_drift_needs_a_passing_backtest(session, cfg):
    row = projection.create(session, "t", cfg=cfg)
    code = cfg.projection.drift_models[0].code
    with pytest.raises(projection.ProjectionError, match="還沒有包含"):
        projection.set_drift_model(session, cfg, row.id, code)
    session.add(BacktestRun(created_by="t", status="done", mode="observation", step=1, results={"drift_models": {
        code: {"by_horizon": {"M1": {"significant": False, "improvement": 0.01, "t": 1.0}}}}}))
    session.flush()
    with pytest.raises(projection.ProjectionError, match="沒有讓 1 個月"):
        projection.set_drift_model(session, cfg, row.id, code)
    session.add(BacktestRun(created_by="t", status="done", mode="observation", step=1, results={"drift_models": {
        code: {"by_horizon": {"M1": {"significant": True, "improvement": 0.04, "t": 2.6}}}}}))
    session.flush()
    assert projection.set_drift_model(session, cfg, row.id, code).drift_model == code
    assert projection.set_drift_model(session, cfg, row.id, "BRENT_FV").drift_model == "BRENT_FV"   # 基本面不受限
    with pytest.raises(projection.ProjectionError, match="沒有名為"):
        projection.set_drift_model(session, cfg, row.id, "NOPE")


def test_projection_uses_stat_drift_and_backtest_reports_it(session, cfg, monkeypatch):
    from crudelab.ingest import Obs, write_observations
    rng = np.random.default_rng(5)
    regime = np.repeat(rng.choice([-1, 1], size=12), 125)[:1500]
    p = prices_from(0.0015 * regime + rng.normal(0, 0.01, 1500))
    days = [d for d in p.index if d <= pd.Timestamp.today()]
    write_observations(session, [Obs("BRENT_M1", d.date(), float(p[d])) for d in days], "manual")
    session.commit()
    row = projection.create(session, "t", cfg=cfg)
    row.drift_model = cfg.projection.drift_models[0].code                           # 直接設，繞過門檻（門檻另有測試）
    session.flush()
    out = projection.compute(session, cfg, row.id)
    assert out["drift"]["kind"] == "momentum" and set(out["drift"]["estimates"]) == {"W1", "M1", "M3"}
    m1 = out["drift"]["estimates"]["M1"]
    if m1["drift"]:
        assert out["cases"][0]["horizons"]["M1"]["drift_effect"] == pytest.approx(
            out["anchor"]["price"] * (math.exp(m1["drift"]) - 1))
    result = backtest.run(session, cfg, backtest.Options("observation", None, None, 5))
    assert set(result["band_compare"]["methods"]) == {"historical", "ewma"}
    assert cfg.projection.drift_models[0].code in result["drift_models"]
