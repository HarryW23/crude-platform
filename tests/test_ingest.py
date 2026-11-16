from datetime import date

import pytest
from sqlalchemy import select

from crudelab.ingest import IngestError, Obs, load_points, load_series, write_observations
from crudelab.models import AuditLog

D1, D2 = date(2026, 9, 1), date(2026, 9, 2)


def test_rewrite_is_idempotent_and_revisions_are_audited(session):
    points = [Obs("BRENT_M1", D1, 70.0), Obs("BRENT_M1", D2, 71.0)]
    assert write_observations(session, points, "reuters_xls") == {"inserted": 2, "updated": 0, "unchanged": 0}
    session.commit()
    assert write_observations(session, points, "reuters_xls") == {"inserted": 0, "updated": 0, "unchanged": 2}
    stats = write_observations(session, [Obs("BRENT_M1", D2, 71.5)], "reuters_xls")
    session.commit()
    assert stats == {"inserted": 0, "updated": 1, "unchanged": 0}
    log = session.scalars(select(AuditLog).where(AuditLog.action == "revise")).one()
    assert log.detail["old"] == 71.0 and log.detail["new"] == 71.5


def test_higher_priority_source_wins_but_both_are_kept(session):
    write_observations(session, [Obs("BRENT_M1", D1, 70.0), Obs("BRENT_M1", D2, 71.0)], "reuters_xls")
    write_observations(session, [Obs("BRENT_M1", D1, 99.0)], "demo")       # 優先級更低
    write_observations(session, [Obs("BRENT_M1", D2, 71.2)], "manual")     # 優先級更高
    session.commit()
    df = load_points(session, ["BRENT_M1"])
    assert dict(zip(df["obs_date"], df["value"])) == {D1: 70.0, D2: 71.2}
    assert list(df["source_code"]) == ["reuters_xls", "manual"]
    assert load_series(session, ["WTI_M1"])["WTI_M1"].empty


def test_unknown_series_or_source_is_rejected(session):
    with pytest.raises(IngestError, match="未知的序列"):
        write_observations(session, [Obs("NOT_A_SERIES", D1, 1.0)], "manual")
    with pytest.raises(IngestError, match="未知的數據源"):
        write_observations(session, [Obs("BRENT_M1", D1, 1.0)], "nope")
    with pytest.raises(IngestError, match="不是有效數字"):
        write_observations(session, [Obs("BRENT_M1", D1, float("nan"))], "manual")
