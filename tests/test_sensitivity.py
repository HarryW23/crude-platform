"""推演敏感度的歷史估計：能從造好的數據裡把已知的關係估回來；沒有關係時明說；採用時記下依據。"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from crudelab import sensitivity
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.models import ParameterVersion

TODAY = date(2026, 10, 30)


def history(session, months=120, g=-0.002, noise=0.01, seed=5):
    """月度庫存隨機變化；價格的月度對數漲跌 = g × 庫存變化 + 噪音。"""
    rng = np.random.default_rng(seed)
    starts = pd.date_range(end="2026-10-01", periods=months, freq="MS")
    stock_change = rng.normal(0, 15, months)
    stocks = 2800 + np.cumsum(stock_change)
    log_price = np.log(80) + np.cumsum(g * stock_change + rng.normal(0, noise, months))
    stock_obs = [Obs("STEO_OECD_STOCKS", d.date(), float(v)) for d, v in zip(starts, stocks, strict=True)]
    write_observations(session, stock_obs, "manual")
    daily = []
    for d, lp in zip(starts, log_price, strict=True):                  # 當月每個交易日都用同一個水平
        month = pd.bdate_range(d, d + pd.offsets.MonthEnd(0))
        daily += [Obs("BRENT_M1", day.date(), float(np.exp(lp))) for day in month]
    write_observations(session, daily, "manual")
    session.commit()
    return float(np.exp(log_price[-1]))


def test_recovers_a_known_relationship(session, cfg):
    price = history(session)
    r = sensitivity.estimate(IndicatorEngine(session, cfg), cfg, TODAY)
    assert r["status"] == "ok" and r["n"] == 119
    assert r["slope_pct"] == pytest.approx(-0.002, rel=0.15)
    assert r["value"] == pytest.approx(0.002 * price, rel=0.15) and r["low"] < r["value"] < r["high"]
    assert r["per_month_1mbd"] == pytest.approx(r["value"] * 30)


def test_no_relationship_is_not_adoptable(session, cfg):
    history(session, g=0.0, noise=0.03)
    r = sensitivity.estimate(IndicatorEngine(session, cfg), cfg, TODAY)
    assert r["status"] in ("weak", "wrong_sign") and r["value"] is not None


def test_wrong_sign_and_insufficient(session, cfg):
    history(session, months=20)
    assert sensitivity.estimate(IndicatorEngine(session, cfg), cfg, TODAY)["status"] == "insufficient"


def test_future_periods_are_not_used(session, cfg):
    history(session)
    write_observations(session, [Obs("STEO_OECD_STOCKS", date(2027, m, 1), 9999.0) for m in range(1, 6)], "manual")
    session.commit()
    r = sensitivity.estimate(IndicatorEngine(session, cfg), cfg, TODAY)
    assert r["end"] <= "2026-10-01" and r["status"] == "ok"


def test_adopt_only_good_estimates(sites, session, cfg):
    admin, front = sites["admin"], sites["front"]
    history(session, g=0.0, noise=0.03)
    assert admin.post("/api/projection/sensitivity/adopt").status_code == 409          # 不可信的不讓採用
    assert front.post("/api/projection/sensitivity/adopt").status_code in (404, 405)
    session.query(ParameterVersion).filter(ParameterVersion.key == "PROJ_PRICE_PER_STOCK").delete()
    from crudelab.models import Observation
    session.query(Observation).delete()
    session.commit()
    history(session)
    body = front.get("/api/projection/sensitivity").json()
    assert body["current"] is None and body["parameter"] == "PROJ_PRICE_PER_STOCK" and body["status"] == "ok"
    r = admin.post("/api/projection/sensitivity/adopt")
    assert r.status_code == 200 and r.json()["value"] == pytest.approx(body["value"], abs=1e-4)
    session.expire_all()
    note = session.query(ParameterVersion).filter(ParameterVersion.key == "PROJ_PRICE_PER_STOCK").one().note
    assert "歷史估計" in note and "t =" in note
    assert front.get("/api/projection/sensitivity").json()["current"] == pytest.approx(r.json()["value"])
