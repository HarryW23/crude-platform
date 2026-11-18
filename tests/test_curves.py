"""期貨曲線：快照、回看、形態、曲線形態指標。"""
from datetime import date

import pandas as pd
import pytest

from crudelab import curves
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations


def put(session, code, days, values):
    write_observations(session, [Obs(code, d, float(v)) for d, v in zip(days, values, strict=True)], "manual")
    session.commit()


def test_curves_loaded_with_twelve_months(cfg):
    assert set(cfg.curves) >= {"BRENT", "WTI", "SC"}
    assert curves.months(cfg, "BRENT")[-1] == (12, "BRENT_M12")
    assert all(f"BRENT_M{n}" in cfg.series for n in range(1, 13))


def test_shape_structure_and_fly():
    pts = [{"month": m, "value": v} for m, v in [(1, 80.0), (6, 77.0), (12, 75.0)]]
    s = curves.shape(pts)
    assert s["spread"] == pytest.approx(5.0) and s["back_month"] == 12
    assert s["structure"].startswith("近強遠弱")
    assert s["fly"] == pytest.approx(80 - 2 * 77 + 75)
    assert curves.shape([{"month": 1, "value": 80.0}, {"month": 6, "value": 82.0}])["structure"].startswith("近弱遠強")
    assert curves.shape([{"month": 1, "value": 80.0}, {"month": 6, "value": 80.2}])["structure"] == "平坦"
    empty = curves.shape([{"month": 1, "value": 80.0}, {"month": 2, "value": None}])
    assert empty["structure"] is None and empty["fly"] is None


def test_snapshot_lookbacks_and_gaps(session, cfg):
    days = list(pd.bdate_range(end=date(2026, 10, 23), periods=40).date)
    for n in (1, 2, 3, 6, 12):
        put(session, f"BRENT_M{n}", days, [70 + i * 0.1 - n * 0.3 for i in range(len(days))])
    snap = curves.snapshot(IndicatorEngine(session, cfg), cfg, "BRENT")
    assert snap["status"] == "ok" and [s["label"] for s in snap["snapshots"]] == ["今天", "1 周前", "1 個月前"]
    assert [s["date"] for s in snap["snapshots"]] == [d.isoformat() for d in (days[-1], days[-6], days[-22])]
    today = {p["month"]: p["value"] for p in snap["snapshots"][0]["points"]}
    assert today[4] is None and today[12] == pytest.approx(70 + 3.9 - 3.6)
    assert snap["months_with_data"] == 5
    assert snap["snapshots"][0]["shape"]["structure"].startswith("近強遠弱")
    assert curves.snapshot(IndicatorEngine(session, cfg), cfg, "WTI")["status"] == "missing_data"


def test_curve_shape_indicators(session, cfg):
    days = list(pd.bdate_range(end=date(2026, 10, 23), periods=5).date)
    for n, v in ((1, 80), (6, 77), (12, 75)):
        put(session, f"BRENT_M{n}", days, [v] * 5)
    engine = IndicatorEngine(session, cfg)
    assert engine.compute("CRV_BRENT_FLY").iloc[-1] == pytest.approx(1.0)
    assert engine.compute("TS_BRENT_M1_M12").iloc[-1] == pytest.approx(5.0)


def test_curves_api_front_only(sites, loaded):
    body = sites["front"].get("/api/curves").json()
    brent = next(c for c in body if c["code"] == "BRENT")
    assert brent["status"] == "ok" and brent["months_with_data"] == 12
    assert sites["admin"].get("/api/curves").status_code == 404
