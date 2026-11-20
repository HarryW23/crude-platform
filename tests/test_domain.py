"""取值約束（domain.py）和業務代碼裡的定義必須一致：改了一邊忘了另一邊，這裡會失敗。"""
from typing import get_args

import pytest
import sqlalchemy as sa

from crudelab import auth, backtest, domain, events, reports, schema
from crudelab.llm import limits
from crudelab.models import Base, Event, JobRun


def literal(model, field):
    return get_args(model.model_fields[field].annotation)


def test_domain_matches_business_definitions():
    assert domain.COMMODITY_CATEGORIES == literal(schema.CommodityDef, "category")
    assert domain.SOURCE_KINDS == literal(schema.SourceDef, "kind")
    assert domain.SOURCE_STATUSES == literal(schema.SourceDef, "status")
    assert domain.SERIES_KINDS == get_args(schema.SeriesKind)
    assert domain.FREQUENCIES == get_args(schema.Frequency)
    assert domain.MODULES == get_args(schema.Module)
    assert domain.EVENT_CATEGORIES == events.CATEGORIES and domain.EVENT_STATUSES == events.STATUSES
    assert domain.ROLES == auth.ROLES
    assert set(domain.REPORT_KINDS) == set(reports.KINDS)
    assert set(domain.LLM_PURPOSES) == set(limits.PURPOSES)
    assert set(domain.BACKTEST_MODES) == set(backtest.MODES)
    assert domain.REPORT_KINDS == tuple(x for x in get_args(literal(schema.JobDef, "kind")[0]))


def test_every_checked_column_exists():
    for table, columns in domain.CHECKED.items():
        assert set(columns) <= set(Base.metadata.tables[table].c.keys()), table


def test_unsafe_values_are_refused():
    with pytest.raises(ValueError):
        domain.in_list("x", ("ok'; DROP TABLE x; --",))


def test_database_refuses_bad_values(session):
    session.add(JobRun(job_code="X", slot=sa.func.now(), trigger="schedule", status="okay"))
    with pytest.raises(sa.exc.IntegrityError):
        session.flush()
    session.rollback()
    for bad in ({"bias": 3}, {"probability": 1.5}, {"status": "rumour"}):
        session.add(Event(**{"title": "t", "category": "other", "status": "rumor", "bias": 0, "origin": "manual",
                             "created_by": "t", **bad}))
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
        session.rollback()
