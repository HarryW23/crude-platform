"""2026-11 全面檢查時發現並修正的問題，每個一條回歸測試。"""
from datetime import date

import pytest
from sqlalchemy import select

from crudelab.config import normalize_alias
from crudelab.connectors.excel import parse_number
from crudelab.imports import commit_batch, make_resolver, preview_file
from crudelab.ingest import IngestError, Obs, write_observations
from crudelab.master import sync_to_db
from crudelab.models import Series, SeriesAlias

# ------------------------------ 數據層 ------------------------------

def test_config_alias_changes_are_synced(session, cfg):
    """配置裡改了別名指向或刪掉別名，數據庫要跟著變；後台學到的別名不動。"""
    norm = normalize_alias("LCOc1")
    row = session.scalars(select(SeriesAlias).where(SeriesAlias.alias_norm == norm)).one()
    row.series_code = "WTI_M1"                                   # 模擬：舊配置裡它指向別處
    session.add(SeriesAlias(alias_norm="stale alias", alias="stale alias", series_code="BRENT_M1", origin="config"))
    session.add(SeriesAlias(alias_norm="my header", alias="my header", series_code="BRENT_M1", origin="learned"))
    session.commit()
    sync_to_db(session, cfg)
    session.commit()
    aliases = {a.alias_norm: a for a in session.scalars(select(SeriesAlias))}
    assert aliases[norm].series_code == "BRENT_M1"
    assert "stale alias" not in aliases and aliases["my header"].origin == "learned"


def test_alias_to_inactive_series_is_ignored(session):
    session.add(SeriesAlias(alias_norm="old header", alias="old header", series_code="BRENT_M2", origin="learned"))
    session.get(Series, "BRENT_M2").active = False
    session.commit()
    resolve = make_resolver(session)
    assert resolve("old header") is None and resolve("LCOc1") == "BRENT_M1"


def test_single_column_csv_and_typographic_minus(session, settings):
    preview = preview_file(session, settings, "日期\n2026-01-02\n2026-01-05\n".encode(), "one.csv", "excel_other")
    assert preview["sheets"][0]["layout"] in ("wide", "unrecognized")         # 不再拋錯
    assert parse_number("−1.25") == -1.25 and parse_number("1,234.5") == 1234.5


def test_long_learned_header_is_not_stored(session, settings):
    header = "很長的表頭" * 40
    content = f"日期,{header}\n2026-01-02,1\n2026-01-05,2\n".encode()
    preview = preview_file(session, settings, content, "long.csv", "excel_other")
    key = preview["sheets"][0]["columns"][0]["key"]
    result = commit_batch(session, preview["batch_id"], {key: "BRENT_M1"})
    assert result["inserted"] == 2 and result["learned_aliases"] == 0


def test_booleans_are_not_numbers(session):
    with pytest.raises(IngestError):
        write_observations(session, [Obs("BRENT_M1", date(2026, 1, 2), True)], "manual")


# ------------------------------ 計算層 ------------------------------

def test_stale_signal_does_not_score_live_or_in_backtest(session, cfg):
    """序列斷更後，信號不再拿舊值打分；回測在同樣的日子也不打分（兩邊規則一致）。"""
    import numpy as np
    import pandas as pd

    from crudelab import backtest, signals
    from crudelab.indicators import IndicatorEngine
    days = list(pd.bdate_range(end="2026-06-30", periods=200).date)
    rng = np.random.default_rng(3)
    m1 = 80 + np.cumsum(rng.normal(0, 0.5, len(days)))
    for code, values in (("BRENT_M1", m1), ("BRENT_M3", m1 - 1 + rng.normal(0, 0.3, len(days)))):
        write_observations(session, [Obs(code, d, float(v)) for d, v in zip(days, values, strict=True)], "manual")
    session.commit()
    engine = IndicatorEngine(session, cfg)
    fresh = signals.data_signal(engine, "SIG_BRENT_STRUCTURE", pd.Timestamp("2026-07-06"))
    stale = signals.data_signal(engine, "SIG_BRENT_STRUCTURE", pd.Timestamp("2026-07-20"))
    assert fresh["status"] == "ok" and stale["status"] == "stale" and stale["score"] is None
    dates = pd.DatetimeIndex(["2026-07-06", "2026-07-20"])
    scores = backtest._observation_scores(engine, cfg, dates)["SIG_BRENT_STRUCTURE"]
    assert scores.iloc[0] == pytest.approx(fresh["score"]) and np.isnan(scores.iloc[1])


def test_formula_arity_and_count_arguments_are_checked():
    from crudelab import formula as fx
    for bad in ("lag(A)", "ma(A, 0)", "ma(A, 2.5)", "abs(A, 2)", "seasonal_avg(A)"):
        with pytest.raises(fx.FormulaError):
            fx.parse(bad)
    fx.parse("diff(A)")
    fx.parse("yoy(ma(A, 4))")


def test_window_functions_over_mixed_frequencies_are_rejected(cfg):
    import copy

    from crudelab.config import _validate_indicators
    from crudelab.schema import IndicatorDef
    bad = copy.copy(cfg)
    bad.indicators = {**cfg.indicators, "MIXED": IndicatorDef(
        code="MIXED", name_zh="x", module="balance", unit="kb",
        formula="diff(EIA_CRUDE_STOCKS, 1) + 0 * BRENT_M1 * EIA_CRUDE_STOCKS / BRENT_M1")}
    errors = _validate_indicators(bad)
    assert any("MIXED" in e and "頻率不同" in e for e in errors)
    assert not any("頻率不同" in e for e in _validate_indicators(cfg))      # 現有配置沒有這種寫法


# ------------------------------ 性能：緩存不能讀到舊數據 ------------------------------

def test_point_cache_sees_every_kind_of_change(session, cfg):
    from sqlalchemy import delete

    from crudelab.indicators import IndicatorEngine
    from crudelab.models import Observation

    def latest():
        values = IndicatorEngine(session, cfg).series_values("BRENT_M1")
        return None if values.empty else float(values.iloc[-1])
    assert latest() is None
    write_observations(session, [Obs("BRENT_M1", date(2026, 1, 2), 80.0)], "manual")
    session.commit()
    assert latest() == 80.0                                               # 新增
    write_observations(session, [Obs("BRENT_M1", date(2026, 1, 2), 81.0)], "manual")
    session.commit()
    assert latest() == 81.0                                               # 改值
    write_observations(session, [Obs("BRENT_M1", date(2026, 1, 2), 79.0)], "demo")
    session.commit()
    assert latest() == 81.0                                               # 優先級低的來源不蓋過
    session.execute(delete(Observation).where(Observation.source_code == "manual"))
    session.commit()
    assert latest() == 79.0                                               # 刪除


def test_pinned_views_are_shared():
    import pandas as pd

    from crudelab.indicators import IndicatorEngine
    engine = IndicatorEngine.__new__(IndicatorEngine)
    engine._pin, engine._views, engine._computed, engine._step = None, {}, {}, set()
    t = pd.Timestamp("2026-10-01")
    assert engine.pinned(t) is engine.pinned(t)
    with pytest.raises(ValueError):
        engine.pinned(t).pinned(t)


# ------------------------------ 運行層 ------------------------------

def test_prune_history_keeps_meaningful_runs(session):
    from datetime import datetime, timedelta

    from crudelab import jobs
    from crudelab.models import JobRun
    old = datetime(2026, 1, 1)
    for i, status in enumerate(("skipped", "ok", "failed")):
        session.add(JobRun(job_code="X", slot=old + timedelta(minutes=i), trigger="schedule", status=status,
                           started_at=old))
    session.add(JobRun(job_code="X", slot=datetime(2026, 11, 1), trigger="schedule", status="skipped",
                       started_at=datetime(2026, 11, 1)))
    session.commit()
    assert jobs.prune_history(datetime(2026, 11, 18)) == 1
    session.expire_all()
    left = sorted(r.status for r in session.scalars(select(JobRun)))
    assert left == ["failed", "ok", "skipped"]                     # 只刪 30 天前的「跳過」


def test_login_failure_tracking_is_bounded(monkeypatch):
    from crudelab import auth
    monkeypatch.setattr(auth, "MAX_TRACKED_NAMES", 50)
    auth._failures.clear()
    for i in range(200):
        auth._recent_failures(f"random-{i}")
    assert len(auth._failures) <= 52
    auth._failures.clear()


def test_chat_history_is_capped(sites):
    long_turn = {"role": "user", "content": "x" * 9000}
    r = sites["front"].post("/api/chat", json={"question": "q", "history": [long_turn]})
    assert r.status_code == 422
    r = sites["front"].post("/api/chat", json={"question": "q", "history": [{"role": "user", "content": "x"}] * 41})
    assert r.status_code == 422
