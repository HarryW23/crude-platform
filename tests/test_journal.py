"""觀點記錄與覆盤：記錄時的快照、到期評分（方向 / 區間 / 把握 / 系統）、撤銷規則、匯總統計。"""
from datetime import date, timedelta

import pandas as pd
import pytest

from crudelab import journal
from crudelab.ingest import Obs, write_observations
from crudelab.models import MarketView

MADE = date(2026, 6, 1)


def prices(session, path):
    """BRENT_M1 的日度價格：path(i) 給第 i 個交易日的價格。"""
    days = list(pd.bdate_range(end="2026-09-30", periods=260).date)
    write_observations(session, [Obs("BRENT_M1", d, float(path(i, d))) for i, d in enumerate(days)], "manual")
    session.commit()


def wiggle(i, d):
    base = 80 + (0.3 if i % 2 else -0.3)                 # 有波動，算得出波動率
    return base * (1.10 if d > date(2026, 6, 15) else 1.0)   # 6 月中之後整體上漲 10%


def test_create_validation_and_snapshot(session, cfg):
    prices(session, wiggle)
    for bad in (dict(horizon="X1", direction=1), dict(horizon="M1", direction=2),
                dict(horizon="M1", direction=1, low=80), dict(horizon="M1", direction=1, low=90, high=80),
                dict(horizon="M1", direction=1, confidence=0.3), dict(horizon="M1", direction=1, reasons=["luck"])):
        with pytest.raises(journal.JournalError):
            journal.create(session, cfg, today=MADE, **bad)
    v = journal.create(session, cfg, horizon="M1", direction=1, low=85, high=95, confidence=0.7,
                       reasons=["inventory", "positioning"], rationale="庫存去化", today=MADE)
    assert v.target_date == MADE + timedelta(days=30) and v.start_date == MADE
    assert v.start_price == pytest.approx(80.3) or v.start_price == pytest.approx(79.7)
    assert v.daily_vol and v.daily_vol > 0 and "overall" in v.system


def test_scoring_open_and_due(session, cfg):
    prices(session, wiggle)
    up = journal.create(session, cfg, horizon="M1", direction=1, low=85, high=95, confidence=0.8, today=MADE)
    down = journal.create(session, cfg, horizon="M1", direction=-1, confidence=0.6, reasons=["geo"], today=MADE)
    session.commit()
    early = journal.overview(session, cfg, today=date(2026, 6, 20))
    assert {v["status"] for v in early["views"]} == {"open"} and early["views"][0]["return"] > 0.05

    later = journal.overview(session, cfg, today=date(2026, 8, 1))
    rows = {v["id"]: v for v in later["views"]}
    assert rows[up.id]["status"] == "scored" and rows[up.id]["actual"] == 1 and rows[up.id]["hit"]
    assert rows[up.id]["range_hit"] is True and rows[up.id]["brier"] == pytest.approx(0.04)
    assert rows[down.id]["hit"] is False and rows[down.id]["brier"] == pytest.approx(0.36)
    s = later["summary"]
    assert s["scored"] == 2 and s["hit"]["rate"] == 0.5 and s["always_up"]["rate"] == 1.0
    assert s["by_reason"]["geo"]["rate"] == 0.0
    assert [c["n"] for c in s["calibration"]] == [1, 1]


def test_small_moves_count_as_neutral(session, cfg):
    prices(session, lambda i, d: 80 + (0.3 if i % 2 else -0.3))           # 沒有趨勢
    v = journal.create(session, cfg, horizon="W1", direction=0, today=MADE)
    session.commit()
    row = journal.overview(session, cfg, today=date(2026, 7, 1))["views"][0]
    assert row["actual"] == 0 and row["hit"] is True and abs(row["return"]) < row["band"]
    assert v.id == row["id"]


def test_cancel_rules(session, cfg):
    prices(session, wiggle)
    v = journal.create(session, cfg, horizon="W1", direction=1, today=MADE)
    with pytest.raises(journal.JournalError, match="原因"):
        journal.cancel(session, v.id, " ", today=MADE)
    with pytest.raises(journal.JournalError, match="到期"):
        journal.cancel(session, v.id, "記錯了", today=MADE + timedelta(days=7))      # 到期的不能撤銷
    journal.cancel(session, v.id, "記錯了", today=MADE + timedelta(days=1))
    session.commit()
    with pytest.raises(journal.JournalError, match="撤銷過"):
        journal.cancel(session, v.id, "again", today=MADE)
    result = journal.overview(session, cfg, today=date(2026, 8, 1))
    assert result["views"][0]["status"] == "cancelled" and result["summary"]["scored"] == 0
    assert session.get(MarketView, v.id).cancel_reason == "記錯了"


def test_api(sites, session, cfg):
    prices(session, wiggle)
    front = sites["front"]
    r = front.post("/api/views", json={"horizon": "M1", "direction": 1, "confidence": 0.7, "reasons": ["supply"],
                                       "rationale": "OPEC+ 延長減產"})
    assert r.status_code == 200
    body = front.get("/api/views").json()
    assert body["views"][0]["reason_names"] == ["OPEC+ / 供應"] and body["anchor"] == "BRENT_M1"
    assert front.post("/api/views", json={"horizon": "M1", "direction": 5}).status_code == 400
    assert front.post(f"/api/views/{r.json()['id']}/cancel", json={"reason": "測試"}).status_code == 200
    assert sites["admin"].get("/api/views").status_code == 404
