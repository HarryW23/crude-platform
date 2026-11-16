from datetime import date, datetime

import pytest
from sqlalchemy import select

from crudelab.ingest import IngestError, Obs, load_points, to_series, write_observations
from crudelab.models import ObservationRevision

D1, D2 = date(2026, 9, 1), date(2026, 9, 2)
T1, T2, T3 = datetime(2026, 9, 3, 8), datetime(2026, 9, 10, 8), datetime(2026, 9, 20, 8)


def values(session, **kwargs):
    df = load_points(session, ["BRENT_M1"], **kwargs)
    return dict(zip(df["obs_date"], df["value"]))


def test_rewrite_is_idempotent_and_revisions_keep_old_values(session):
    points = [Obs("BRENT_M1", D1, 70.0, "a!B2"), Obs("BRENT_M1", D2, 71.0, "a!B3")]
    assert write_observations(session, points, "reuters_xls", now=T1) == {"inserted": 2, "updated": 0, "unchanged": 0}
    assert write_observations(session, points, "reuters_xls", now=T2) == {"inserted": 0, "updated": 0, "unchanged": 2}
    assert write_observations(session, [Obs("BRENT_M1", D2, 71.5, "b!B3")], "reuters_xls", now=T2)["updated"] == 1
    assert write_observations(session, [Obs("BRENT_M1", D2, 71.8, "c!B3")], "reuters_xls", now=T3)["updated"] == 1
    session.commit()
    revisions = session.scalars(select(ObservationRevision).order_by(ObservationRevision.id)).all()
    assert [(r.value, r.source_ref, r.recorded_at, r.replaced_at) for r in revisions] == \
        [(71.0, "a!B3", T1, T2), (71.5, "b!B3", T2, T3)]


def test_as_of_reconstructs_what_was_known_at_the_time(session):
    write_observations(session, [Obs("BRENT_M1", D1, 70.0)], "reuters_xls", now=T1)
    write_observations(session, [Obs("BRENT_M1", D1, 70.4), Obs("BRENT_M1", D2, 71.0)], "reuters_xls", now=T2)
    write_observations(session, [Obs("BRENT_M1", D1, 70.6)], "reuters_xls", now=T3)
    session.commit()
    assert values(session, as_of=datetime(2026, 9, 1)) == {}
    assert values(session, as_of=T1) == {D1: 70.0}
    assert values(session, as_of=datetime(2026, 9, 15)) == {D1: 70.4, D2: 71.0}
    assert values(session) == {D1: 70.6, D2: 71.0}


def test_higher_priority_source_wins_but_both_are_kept(session):
    write_observations(session, [Obs("BRENT_M1", D1, 70.0), Obs("BRENT_M1", D2, 71.0)], "reuters_xls")
    write_observations(session, [Obs("BRENT_M1", D1, 99.0)], "demo")       # 優先級更低
    write_observations(session, [Obs("BRENT_M1", D2, 71.2)], "manual")     # 優先級更高
    session.commit()
    df = load_points(session, ["BRENT_M1"])
    assert dict(zip(df["obs_date"], df["value"])) == {D1: 70.0, D2: 71.2}
    assert list(df["source_code"]) == ["reuters_xls", "manual"]
    assert to_series(load_points(session, ["WTI_M1"]), "WTI_M1").empty


def test_invalid_input_is_rejected(session):
    with pytest.raises(IngestError, match="未知的序列"):
        write_observations(session, [Obs("NOT_A_SERIES", D1, 1.0)], "manual")
    with pytest.raises(IngestError, match="未知的數據源"):
        write_observations(session, [Obs("BRENT_M1", D1, 1.0)], "nope")
    with pytest.raises(IngestError, match="不是有效數字"):
        write_observations(session, [Obs("BRENT_M1", D1, float("nan"))], "manual")
