from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from crudelab import analytics, scenarios
from crudelab.connectors.eia import _period_date
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations


def weekly(n: int, end=date(2026, 10, 23)) -> list[date]:
    return [end - timedelta(days=7 * i) for i in range(n)][::-1]


def put(session, code: str, dates: list[date], values) -> None:
    write_observations(session, [Obs(code, d, float(v)) for d, v in zip(dates, values)], "manual")
    session.commit()


# ------------------------------ 統計函數 ------------------------------

def test_seasonal_stats_requires_all_years():
    index = pd.DatetimeIndex(weekly(6 * 52 + 3))
    series = pd.Series(np.arange(len(index), dtype=float), index=index)
    stats = analytics.seasonal_stats(series, 5)
    last = stats.iloc[-1]
    # 周度數據：一年前的同期差 52 或 53 個點（日期差 ≤ 4 天）
    back = [series.index.get_indexer([series.index[-1] - pd.DateOffset(years=k)], method="nearest")[0]
            for k in range(1, 6)]
    assert last["avg"] == pytest.approx(np.mean(series.iloc[back])) and last["min"] == series.iloc[back[-1]]
    assert stats.iloc[4 * 52]["avg"] != stats.iloc[4 * 52]["avg"]       # 只有四年歷史的日期 → NaN，不用四年湊
    assert analytics.seasonal_stats(pd.Series(dtype=float), 5).empty


def test_year_ago_matches_monthly_exactly():
    index = pd.DatetimeIndex([date(2024 + m // 12, m % 12 + 1, 1) for m in range(30)])
    series = pd.Series(range(30), index=index, dtype=float)
    assert (series - analytics.year_ago(series)).dropna().tolist() == [12.0] * 18


def test_linear_fit_recovers_known_relation_and_refuses_small_samples():
    index = pd.DatetimeIndex(weekly(80))
    x = pd.Series(np.linspace(-50, 50, 80), index=index)
    daily = pd.date_range(index[0] - pd.Timedelta(days=2), index[-1])
    y = (3.0 - 0.04 * x).reindex(daily, method="nearest")
    y = y[y.index.day_of_week < 5]                                        # y 是日度；周五對得上
    fit = analytics.linear_fit(y, x, lookback_days=2000, min_points=30)
    assert fit["status"] == "ok" and fit["n"] == 80
    assert fit["slope"] == pytest.approx(-0.04) and fit["intercept"] == pytest.approx(3.0)
    assert fit["r2"] == pytest.approx(1)
    assert fit["latest"]["residual"] == pytest.approx(0, abs=1e-9)
    short = analytics.linear_fit(y, x, lookback_days=100, min_points=30)
    assert short["status"] == "insufficient" and short["slope"] is None and 10 < short["n"] < 30


def test_eia_period_dates():
    assert _period_date("2026-10-23") == date(2026, 10, 23) and _period_date("2026-09") == date(2026, 9, 1)
    assert _period_date("2026-Q3") == date(2026, 7, 1) and _period_date("2026") == date(2026, 1, 1)
    assert _period_date("garbage") is None


# ------------------------------ 指標與平衡表 ------------------------------

def test_seasonal_indicator(session, cfg):
    dates = weekly(6 * 52 + 2)
    put(session, "EIA_CRUDE_STOCKS", dates, [400_000 + 10 * i for i in range(len(dates))])
    engine = IndicatorEngine(session, cfg)
    deviation = engine.compute("STK_US_CRUDE_VS_5Y")
    assert len(deviation) < 60                                          # 只有湊得齊五年同期的日期才有值
    assert deviation.iloc[-1] == pytest.approx(10 * np.mean([52, 104, 156, 209, 261]), rel=0.02)
    summary = engine.summary("STK_US_CRUDE_VS_5Y")
    assert summary["frequency"] == "weekly" and engine.summary("SPR_BRENT_WTI")["frequency"] == "daily"


def test_us_crude_balance_and_residual(session, cfg):
    dates = weekly(3)
    for code, values in {"EIA_CRUDE_PRODUCTION": [13_000] * 3, "EIA_CRUDE_IMPORTS": [6_000] * 3,
                         "EIA_CRUDE_EXPORTS": [4_000] * 3, "EIA_REFINERY_INPUT": [16_000, 15_500, 14_600],
                         "EIA_CRUDE_STOCKS": [430_000, 431_400, 435_600], "EIA_SPR_STOCKS": [400_000] * 3}.items():
        put(session, code, dates, values)
    engine = IndicatorEngine(session, cfg)
    assert list(engine.compute("BAL_US_CRUDE_BALANCE")) == [-1000, -500, 400]
    assert list(engine.compute("BAL_US_CRUDE_ACTUAL")) == pytest.approx([200, 600])     # 第一周沒有上周可比
    assert list(engine.compute("BAL_US_CRUDE_RESIDUAL")) == pytest.approx([-700, -200])
    spec = cfg.balances["US_CRUDE"]
    assert [r.kind for r in spec.rows] == ["supply", "supply", "supply_total", "demand", "demand", "demand_total",
                                           "balance", "actual", "residual"]


def test_saved_scenario_adjusts_a_date_range_and_flows_through(session, cfg):
    months = [date(2026, m, 1) for m in range(9, 13)]
    for code, value in {"STEO_OPEC_SUPPLY": 33.0, "STEO_NONOPEC_SUPPLY": 70.0,
                        "STEO_OECD_DEMAND": 46.0, "STEO_NONOECD_DEMAND": 57.5}.items():
        put(session, code, months, [value] * 4)
    scenarios.create(session, "MY_VIEW", "我的判斷")
    first = scenarios.add_adjustment(session, cfg, "MY_VIEW", "BAL_GLOBAL_S_OPEC", date(2026, 10, 1),
                                     date(2026, 11, 30), 0.4, "OPEC+ 提前增產")
    scenarios.add_adjustment(session, cfg, "MY_VIEW", "STEO_NONOECD_DEMAND", date(2026, 12, 1), None, -0.3, "需求放緩")
    session.commit()

    base = IndicatorEngine(session, cfg)
    view = IndicatorEngine(session, cfg, scenario=scenarios.load(session, cfg, "MY_VIEW"))
    assert list(base.compute("BAL_GLOBAL_BALANCE")) == pytest.approx([-0.5] * 4)
    assert list(view.compute("BAL_GLOBAL_BALANCE")) == pytest.approx([-0.5, -0.1, -0.1, -0.2])
    # 調的是序列，所以會傳到所有用到它的指標
    assert list(view.compute("GLB_DEMAND")) == pytest.approx([103.5, 103.5, 103.5, 103.2])
    assert list(base.series_values("STEO_NONOECD_DEMAND")) == [57.5] * 4                      # 數據本身沒被改

    # 「最新值」取已發生的最後一期（測試日期在 2026-11 之後，9–12 月都已發生時取 12 月）；未來期間只算預測
    future = [date(date.today().year + 1, m, 1) for m in (1, 2)]
    for code, value in {"STEO_OPEC_SUPPLY": 40.0, "STEO_NONOPEC_SUPPLY": 70.0,
                        "STEO_OECD_DEMAND": 46.0, "STEO_NONOECD_DEMAND": 57.5}.items():
        put(session, code, future, [value] * 2)
    summary = IndicatorEngine(session, cfg).summary("BAL_GLOBAL_BALANCE")
    assert summary["last_date"] <= date.today().isoformat() and summary["last_value"] == pytest.approx(-0.5)
    assert summary["forecast_until"] == future[-1].isoformat() and summary["n"] == 6

    before_removal = first.created_at
    scenarios.remove_adjustment(session, first.id)
    session.commit()
    assert len(scenarios.adjustments(session, "MY_VIEW")) == 1
    assert len(scenarios.adjustments(session, "MY_VIEW", as_of=before_removal)) == 1        # 那時第二條還沒建
    after = IndicatorEngine(session, cfg, scenario=scenarios.load(session, cfg, "MY_VIEW"))
    assert list(after.compute("BAL_GLOBAL_BALANCE"))[:4] == pytest.approx([-0.5, -0.5, -0.5, -0.2])


def test_scenario_input_is_validated(session, cfg):
    scenarios.create(session, "S1", "測試")
    for kwargs, message in [({"target": "NOPE"}, "既不是序列也不是指標"), ({"delta": 0}, "非零"),
                            ({"date_to": date(2026, 1, 1)}, "不能早於")]:
        args = {"target": "STEO_OPEC_SUPPLY", "date_from": date(2026, 6, 1), "date_to": None, "delta": 1.0, **kwargs}
        with pytest.raises(scenarios.ScenarioError, match=message):
            scenarios.add_adjustment(session, cfg, "S1", note=None, **args)
    with pytest.raises(scenarios.ScenarioError, match="已存在"):
        scenarios.create(session, "S1", "重複")
    with pytest.raises(scenarios.ScenarioError, match="大寫"):
        scenarios.create(session, "bad code", "x")
    scenarios.archive(session, "S1")
    with pytest.raises(scenarios.ScenarioError, match="沒有名為"):
        scenarios.load(session, cfg, "S1")
