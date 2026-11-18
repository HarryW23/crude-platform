"""合約日曆與換月處理：最後交易日、每天的 M1 是哪個合約、同一合約對比、換月調整收益。"""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from crudelab import contracts as c
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.schema import CurveDef, RollRule

BRENT = RollRule(months_before=2)
WTI = RollRule(months_before=1, day=25, business_days_before=3)


def test_last_trading_day_rules(cfg):
    assert c.last_trading_day(BRENT, c.ordinal(2026, 12)) == date(2026, 10, 30)      # 10 月最後一個工作日
    assert c.last_trading_day(BRENT, c.ordinal(2027, 3)) == date(2027, 1, 29)
    assert c.last_trading_day(WTI, c.ordinal(2026, 12)) == date(2026, 11, 20)        # 11/25 周三，往前 3 個工作日
    assert c.last_trading_day(WTI, c.ordinal(2026, 11)) == date(2026, 10, 20)        # 10/25 周日 → 10/23，再往前 3 個
    gasoil = cfg.curves["GASOIL_ICE"].roll
    assert c.last_trading_day(gasoil, c.ordinal(2026, 11)) == date(2026, 11, 11)     # 11/14 周六 → 11/13，再往前 2 個
    fixed = RollRule(months_before=2, overrides={"2026-12": date(2026, 10, 29)})
    assert c.last_trading_day(fixed, c.ordinal(2026, 12)) == date(2026, 10, 29)


def test_front_contract_rolls_after_last_trading_day():
    assert c.contract_label(c.front_ordinal(BRENT, date(2026, 10, 30))) == "2026-12"   # 最後交易日當天仍是 M1
    assert c.contract_label(c.front_ordinal(BRENT, date(2026, 11, 2))) == "2027-01"
    assert c.rolls_between(BRENT, date(2026, 10, 1), date(2026, 12, 15)) == 2        # 12 月 → 2 月合約
    assert c.roll_dates(BRENT, date(2026, 10, 15), date(2026, 12, 31)) == [date(2026, 11, 2), date(2026, 12, 1)]
    days = pd.bdate_range("2025-01-01", "2027-06-30")
    assert list(c.front_ordinals(WTI, days)) == [c.front_ordinal(WTI, d.date()) for d in days]


def test_all_configured_curves_have_roll_rules(cfg):
    assert all(curve.roll is not None for curve in cfg.curves.values())


def test_curve_config_validation():
    base = dict(code="X", name_zh="x", commodity="BRENT", location="NWE", kind="futures", unit="USD/bbl")
    with pytest.raises(ValidationError):
        CurveDef(**base, months=[1, 3])                     # 月序不連續：換月後 M1 = 原 M2 不成立
    with pytest.raises(ValidationError):
        RollRule(months_before=1, overrides={"2026-13": date(2026, 1, 1)})
    with pytest.raises(ValidationError):
        RollRule(months_before=1, day=31)


def put_contracts(session, days, price_of, months=(1, 2, 3)):
    """按具體合約寫入連續合約：Mn = 當天第 n 個未到期合約的價格。"""
    for n in months:
        obs = [Obs(f"BRENT_M{n}", d.date(), price_of(c.front_ordinal(BRENT, d.date()) + n - 1, d))
               for d in days]
        write_observations(session, obs, "manual")
    session.commit()


def test_same_contract_changes_ignore_roll(session, cfg):
    """每個合約價格固定不變：市場沒有任何波動，連續合約卻在換月日跳一下。同一合約對比的變化應該是 0。"""
    days = pd.bdate_range(end="2026-11-02", periods=30)                # 最後一天正好是換月日
    put_contracts(session, days, lambda o, d: 80.0 - 0.5 * (o - c.ordinal(2026, 12)), months=(1, 2, 3, 4))
    engine = IndicatorEngine(session, cfg)
    raw = engine.series_values("BRENT_M1")
    assert raw.iloc[-1] - raw.iloc[-2] == pytest.approx(-0.5)           # 原始序列：換月跳了 0.5
    s = engine.summary("TS_BRENT_M1_M3")
    assert s["roll_adjusted"] and s["chg_1d"] == pytest.approx(0) and s["chg_5d"] == pytest.approx(0)
    assert s["chg_unadjusted"] == []

    returns, fallback = c.chained_returns(cfg, "BRENT_M1", engine.series_values)
    assert fallback == 0 and np.allclose(returns.to_numpy(), 0)
    adjusted, _ = engine.adjusted_series("BRENT_M1")
    assert adjusted.iloc[-1] == raw.iloc[-1] and np.allclose(adjusted.to_numpy(), raw.iloc[-1])


def test_unadjustable_change_is_flagged(session, cfg):
    """只有 M1：換月後找不到原來的合約，退回直接相減並標出來。"""
    days = pd.bdate_range(end="2026-11-02", periods=10)
    put_contracts(session, days, lambda o, d: 80.0 - 0.5 * (o - c.ordinal(2026, 12)), months=(1,))
    engine = IndicatorEngine(session, cfg)
    _, fallback = c.chained_returns(cfg, "BRENT_M1", engine.series_values)
    assert fallback == 1


def test_pinned_view_maps_back_to_same_contract(session, cfg):
    days = pd.bdate_range(end="2026-11-10", periods=40)
    put_contracts(session, days, lambda o, d: float(o))                 # 價格就是合約序號，方便核對
    view = IndicatorEngine(session, cfg).pinned(pd.Timestamp("2026-11-10"))
    pinned = view.series_values("BRENT_M1")
    assert set(pinned.to_numpy()) == {float(c.ordinal(2027, 1))}         # 每一天都是 2027-01 合約
    assert len(pinned) == len(days)                                      # 最早那天它是 M3，有數據
    assert view.series_values("BRENT_M3").index[0] > days[0]             # 它在更早的日子是 M5，沒有數據


def test_curves_api_has_contracts_and_rolls(sites, loaded):
    body = sites["front"].get("/api/curves").json()
    brent = next(x for x in body if x["code"] == "BRENT")
    today = brent["snapshots"][0]
    assert today["points"][0]["contract"] == "2026-12" and today["points"][1]["contract"] == "2027-01"
    assert brent["snapshots"][2]["rolls_since"] == 1                     # 9 月底到 10 月 23 日之間換過一次
    rolls = sites["front"].get("/api/indicators/TS_BRENT_M1_M3/rolls").json()
    assert {"date": "2026-10-01", "curve": "BRENT", "curve_name": "布倫特期貨"} in rolls
