"""計價視角：現貨升貼水的基準按計價月份取合約，而不是當天的 M1（#21）。"""
from datetime import date

import pandas as pd
import pytest

from crudelab import contracts
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations

# 2025-12 的 WTI 合約：11 月 25 日（週二）往前 3 個工作日 = 11 月 20 日（週四）到期（用過去的日期：摘要只看已發生的）
DAYS = [date(2025, 11, 19), date(2025, 11, 20), date(2025, 11, 21), date(2025, 11, 26)]


def load(session, data):
    write_observations(session, [Obs(code, d, v) for code, values in data.items()
                                 for d, v in zip(DAYS, values, strict=True)], "manual")
    session.commit()


@pytest.fixture()
def wti(session):
    load(session, {"WTI_M1": [60, 61, 62, 63], "WTI_M2": [70, 71, 72, 73], "MARS_DIFF": [1.0, 1.0, 1.0, 1.0]})
    return session


def test_wti_trade_month_uses_the_delivery_month_contract(wti, cfg):
    view = cfg.views["WTI_TRADE_MONTH"]
    assert contracts.contract_label(contracts.view_target(view, date(2025, 11, 25))) == "2025-12"
    assert contracts.contract_label(contracts.view_target(view, date(2025, 11, 26))) == "2026-01"   # 26 日起換交易月
    values = IndicatorEngine(wti, cfg).view_values("WTI_TRADE_MONTH")
    # 11/19、11/20：12 月合約還是 M1；11/21：12 月合約已到期但交易月沒結束 → 用它 11/20 的結算價；11/26：1 月合約
    assert list(values) == [60, 61, 61, 63]


def test_outright_uses_view_and_changes_compare_the_same_contract(wti, cfg):
    engine = IndicatorEngine(wti, cfg)
    out = engine.compute("OUT_MARS")
    assert list(out) == [61, 62, 62, 64]
    summary = engine.summary("OUT_MARS")
    # 11/21 → 11/26 計價月份從 12 月換到 1 月：按 1 月合約比（62 → 63），不是 62 → 64
    assert summary["chg_1d"] == pytest.approx(1.0) and summary["roll_adjusted"]


def test_trace_shows_contract_and_settlement(wti, cfg):
    engine = IndicatorEngine(wti, cfg)
    live = engine.trace("OUT_MARS", date(2025, 11, 19))
    view = next(c for c in live["children"] if c["code"] == "WTI_TRADE_MONTH")
    assert view["kind"] == "view" and view["contract"] == "2025-12" and view["children"][0]["code"] == "WTI_M1"
    expired = engine.trace("OUT_MARS", date(2025, 11, 21))
    view = next(c for c in expired["children"] if c["code"] == "WTI_TRADE_MONTH")
    assert view["value"] == 61 and "結算價" in view["note"]


def test_dubai_loading_month_is_two_months_ahead(session, cfg):
    days = [date(2026, 11, 19), date(2026, 12, 1)]
    write_observations(session, [Obs(f"DUBAI_SWAP_M{n}", d, 60 + n + i) for n in (1, 2, 3)
                                 for i, d in enumerate(days)] +
                       [Obs("ESPO_DIFF", d, 4.0) for d in days], "manual")
    session.commit()
    view = cfg.views["DUBAI_LOADING"]
    assert [contracts.contract_label(contracts.view_target(view, d)) for d in days] == ["2027-01", "2027-02"]
    engine = IndicatorEngine(session, cfg)
    assert list(engine.view_values("DUBAI_LOADING")) == [62, 63]       # 兩天都是曲線上的 M2
    assert list(engine.compute("OUT_ESPO")) == [66, 67]


def test_view_needs_a_curve_with_roll_rule(cfg):
    import dataclasses

    from crudelab.config import _validate
    from crudelab.schema import PricingView
    assert cfg.has("WTI_TRADE_MONTH") and cfg.unit_of("WTI_TRADE_MONTH") == "USD/bbl"
    assert cfg.frequency_of("WTI_TRADE_MONTH") == "daily"
    extra = {"X_VIEW": PricingView(code="X_VIEW", name_zh="x", curve="NOPE", months_ahead=1),
             "BRENT_M1": PricingView(code="BRENT_M1", name_zh="y", curve="WTI", months_ahead=1)}
    bad = dataclasses.replace(cfg, views={**cfg.views, **extra})
    errors = _validate(bad)
    assert any("X_VIEW：曲線 NOPE 不存在" in e for e in errors)
    assert any("序列與計價視角重名：BRENT_M1" in e for e in errors)


def test_month_to_date_mean():
    from crudelab import formula as fx
    x = pd.Series([1.0, 2.0, 3.0, 10.0], index=pd.to_datetime(["2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02"]))
    assert list(fx.evaluate("mtd(x)", {"x": x})) == [1.0, 1.5, 3.0, 6.5]
    with pytest.raises(fx.FormulaError):
        fx.parse("mtd(x, 2)")


def test_roll_markers_show_pricing_month_switches(wti, cfg):
    from crudelab.api.routes.indicators import indicator_rolls
    rows = indicator_rolls("OUT_MARS", IndicatorEngine(wti, cfg))
    assert [r["date"] for r in rows] == [date(2025, 11, 26)]          # 26 日換交易月；WTI 自己 11/20 的換月不影響
    assert contracts.view_switch_dates(cfg.views["DUBAI_LOADING"], date(2026, 11, 15), date(2027, 1, 5)) == \
        [date(2026, 12, 1), date(2027, 1, 1)]
