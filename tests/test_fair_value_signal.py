"""公允價值偏離作為信號（#52）：初始權重 0，只計算不合成；回測和實時同一條公式，權重由回測校準。"""
import dataclasses
from datetime import date

import pandas as pd
import pytest

from crudelab import backtest, fair_value, signals
from crudelab.fair_value import Fit
from crudelab.indicators import IndicatorEngine

TODAY = pd.Timestamp(2026, 11, 18)


def test_live_score_and_zero_weight(session, cfg, monkeypatch):
    monkeypatch.setattr(fair_value, "current", lambda e, c, code, on=None: Fit(
        model=code, date="2026-11-18", status="ok", n=500, deviation_pct=0.06, z=1.5))
    engine = IndicatorEngine(session, cfg)
    sig = signals.data_signal(engine, "SIG_BRENT_FAIR_VALUE", TODAY)
    assert sig["status"] == "ok" and sig["score"] == pytest.approx(-0.75)     # 高於公允價值 1.5σ → 偏空
    assert sig["weight"] == 0 and sig["input_name"] == cfg.fair_values["BRENT_FV"].name_zh
    stale = Fit(model="BRENT_FV", date="2026-10-01", status="ok", n=500, z=1.5)
    monkeypatch.setattr(fair_value, "current", lambda e, c, code, on=None: stale)
    assert signals.data_signal(engine, "SIG_BRENT_FAIR_VALUE", TODAY)["status"] == "stale"
    monkeypatch.setattr(fair_value, "current", lambda e, c, code, on=None: Fit(model=code, date="2026-11-18",
                                                                               status="insufficient", n=20))
    assert signals.data_signal(engine, "SIG_BRENT_FAIR_VALUE", TODAY)["status"] == "insufficient"


def test_zero_weight_does_not_move_the_composite(session, cfg, monkeypatch):
    monkeypatch.setattr(fair_value, "current", lambda e, c, code, on=None: Fit(
        model=code, date=date.today().isoformat(), status="ok", n=500, z=-2.0))
    result = signals.compute(session, IndicatorEngine(session, cfg))
    balance = next(m for m in result["modules"] if m["module"] == "balance")
    fv = next(s for s in balance["signals"] if s["code"] == "SIG_BRENT_FAIR_VALUE")
    assert fv["score"] == pytest.approx(1.0) and fv["weight"] == 0
    assert balance["score"] is None                                         # 其他信號沒數據；權重 0 的不算數


def test_backtest_uses_the_same_formula_per_date(cfg):
    dates = pd.bdate_range("2026-01-05", periods=4)
    fits = {"BRENT_FV": {dates[0]: Fit(model="BRENT_FV", z=1.5), dates[1]: Fit(model="BRENT_FV", z=-4.0),
                         dates[2]: Fit(model="BRENT_FV", z=None)}}
    got = backtest.fair_value_scores(cfg, fits, dates)["SIG_BRENT_FAIR_VALUE"]
    assert got.iloc[0] == pytest.approx(-0.75) and got.iloc[1] == 1.0 and got.iloc[2:].isna().all()


def test_config_checks_the_model_exists(cfg):
    from crudelab.config import _validate
    bad = cfg.signals["SIG_BRENT_FAIR_VALUE"].model_copy(update={"input": "NOPE_FV"})
    errors = _validate(dataclasses.replace(cfg, signals={**cfg.signals, "SIG_BRENT_FAIR_VALUE": bad}))
    assert any("NOPE_FV 不是公允價值模型" in e for e in errors)
