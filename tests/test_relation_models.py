"""關係模型改進（#26）：滯後、轉折點（非線性）、穩健回歸（異常值）、分段穩定性；回測裡並排比較不同寫法。"""
import dataclasses

import numpy as np
import pandas as pd
import pytest

from crudelab import analytics, backtest
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.schema import RelationDef

RNG = np.random.default_rng(11)


def frame(x, y):
    return pd.DataFrame({"x": x, "y": y}, index=pd.date_range("2020-01-03", periods=len(x), freq="W-FRI"))


def test_hinge_finds_the_turning_point():
    x = np.linspace(-60, 60, 240)
    y = np.where(x < 0, -0.02 * x, -0.08 * x) + RNG.normal(0, 0.1, len(x))      # 庫存低於 0 以後才特別敏感
    m = analytics.fit_relation(frame(x, y), "ols", "hinge")
    assert m.hinge == pytest.approx(0, abs=10) and m.slope == pytest.approx(-0.02, abs=0.01)
    assert m.slope_above == pytest.approx(-0.08, abs=0.01)
    linear = analytics.fit_relation(frame(x, y), "ols", "linear")
    assert m.r2 > linear.r2
    assert len(m.curve(-60, 60)) == 3                                             # 折線：兩端加轉折點
    assert analytics.fit_relation(frame(x[:15], y[:15]), "ols", "hinge") is None # 兩側湊不齊各 10 個：不硬算


def test_huber_resists_outliers():
    x = np.linspace(0, 100, 200)
    y = 0.5 * x + RNG.normal(0, 1, len(x))
    y[::25] -= 80                                                                 # 幾個極端點（例如數據錯誤）
    assert abs(analytics.fit_relation(frame(x, y), "huber").slope - 0.5) < 0.02
    assert abs(analytics.fit_relation(frame(x, y), "ols").slope - 0.5) > abs(
        analytics.fit_relation(frame(x, y), "huber").slope - 0.5)


def test_lag_pairs_y_with_earlier_x():
    days = pd.date_range("2021-01-01", periods=400, freq="D")
    x = pd.Series(RNG.normal(0, 1, len(days)).cumsum(), index=days)
    y = pd.Series(2 * x.shift(14).to_numpy() + RNG.normal(0, 0.1, len(days)), index=days).dropna()
    pairs = analytics.aligned_pairs(y, x, lag_days=14)
    assert pairs.index[0] == days[14] and pairs["x"].iloc[0] == x.iloc[0]          # y(t) 配 x(t − 14)
    with_lag = analytics.relation_fit(y, x, 365, 30, lag_days=14)
    without = analytics.relation_fit(y, x, 365, 30)
    assert with_lag["r2"] > 0.99 and with_lag["r2"] > without["r2"] and with_lag["lag_days"] == 14


def test_stability_flags_a_relation_that_flips():
    x = RNG.normal(0, 1, 200)
    y = np.concatenate([x[:100], -x[100:]]) + RNG.normal(0, 0.1, 200)             # 前半正相關，後半負相關
    st = analytics.relation_stability(frame(x, y), "ols", "linear")
    assert st["pieces"] == 4 and st["same_sign"] == 0.5
    steady = analytics.relation_stability(frame(x, 3 * x + RNG.normal(0, 0.1, 200)), "ols", "linear")
    assert steady["same_sign"] == 1.0


def test_relation_api_reports_spec_curve_and_stability(client, loaded):
    fit = client.get("/api/relations/WTI_STRUCTURE_VS_CUSHING").json()
    assert fit["method"] == "ols" and fit["shape"] == "linear" and fit["lag_days"] == 0
    if fit["status"] == "ok":
        assert len(fit["curve"]) == 2 and fit["stability"]["pieces"] >= 1


def test_backtest_compares_specs_and_prefers_the_hinge_when_it_is_real(session, cfg):
    fridays = pd.date_range("2018-01-05", periods=420, freq="W-FRI")
    x = 30_000 + np.cumsum(RNG.normal(0, 1_500, len(fridays)))
    k = np.median(x)
    y = np.where(x < k, 1.0, 1.0 - 0.0008 * (x - k)) - 0.00005 * (x - k).clip(max=0) + RNG.normal(0, 0.05, len(x))
    write_observations(session, [Obs(c, d.date(), float(v)) for d, a, b in zip(fridays, x, y, strict=True)
                                 for c, v in (("EIA_CRUDE_STOCKS", a), ("EIA_CUSHING_STOCKS", b))], "manual")
    session.commit()
    rel = RelationDef(code="R", name_zh="r", y="EIA_CUSHING_STOCKS", x="EIA_CRUDE_STOCKS", lookback_days=1460,
                      min_points=60)
    small = dataclasses.replace(cfg, relations={"R": rel})
    dates = pd.bdate_range("2022-01-03", "2025-12-31")[::10]
    out = backtest._relation_with_alternatives(small, IndicatorEngine(session, small), "R", dates,
                                                backtest.horizons_of(small), 10, "observation")
    assert out["spec_name"] == "直線" and {a["spec_name"] for a in out["alternatives"]} == {"直線（穩健）", "帶轉折點"}
    hinge = next(a for a in out["alternatives"] if a["spec_name"] == "帶轉折點")
    assert hinge["by_horizon"]["M1"]["rmse"] < out["by_horizon"]["M1"]["rmse"]   # 真的有轉折：樣本外更準
