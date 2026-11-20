"""機構預測按報告期保存（#23）：能還原任意一期報告當時的數字，回測不用後來的修訂。"""
import dataclasses
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from crudelab import backtest
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import IngestError, Obs, vintage_points, write_observations
from crudelab.models import Observation, ObservationVintage
from crudelab.schema import SignalDef

S = "STEO_OECD_STOCKS"          # 按期保存的序列
M = date(2026, 3, 1)            # 數據期（月度記在月初）


def current(session):
    return session.query(Observation).filter(Observation.series_code == S, Observation.obs_date == M).one().value


def issues(session):
    return [(r.vintage, r.value) for r in session.query(ObservationVintage).filter(
        ObservationVintage.series_code == S, ObservationVintage.obs_date == M).order_by(ObservationVintage.vintage)]


def test_issues_are_kept_and_backfilling_never_overwrites_newer(session):
    write_observations(session, [Obs(S, M, 2800.0, vintage=date(2026, 4, 7))], "manual")
    write_observations(session, [Obs(S, M, 2800.0, vintage=date(2026, 4, 8))], "manual")   # 數字沒變也記：那一期確認過
    write_observations(session, [Obs(S, M, 2812.0, vintage=date(2026, 5, 12))], "manual")  # 下一期修訂
    stats = write_observations(session, [Obs(S, M, 2790.0, vintage=date(2026, 3, 10))], "manual")   # 補錄更早一期
    session.commit()
    assert issues(session) == [(date(2026, 3, 10), 2790.0), (date(2026, 4, 7), 2800.0), (date(2026, 4, 8), 2800.0),
                               (date(2026, 5, 12), 2812.0)]
    assert current(session) == 2812.0 and stats["older_vintage"] == 1                       # 主表保持最新一期
    write_observations(session, [Obs(S, M, 2801.0, vintage=date(2026, 4, 7))], "manual")   # 更正同一期
    session.commit()
    assert issues(session)[1] == (date(2026, 4, 7), 2801.0) and current(session) == 2812.0
    with pytest.raises(IngestError, match="晚於今天"):
        write_observations(session, [Obs(S, M, 1.0, vintage=date(2099, 1, 1))], "manual")


def test_daily_pull_without_issue_date_only_records_changes(session):
    for _ in range(3):                                                  # 每天取一次，數字一樣
        write_observations(session, [Obs(S, M, 2800.0)], "manual")
    session.commit()
    assert len(issues(session)) == 1 and issues(session)[0][0] == datetime.now(UTC).date()


def test_replay_any_issue(session):
    write_observations(session, [Obs(S, M, 2790.0, vintage=date(2026, 3, 10)),
                                 Obs(S, date(2026, 4, 1), 2795.0, vintage=date(2026, 4, 7))], "manual")
    write_observations(session, [Obs(S, M, 2812.0, vintage=date(2026, 5, 12))], "manual")
    session.commit()
    at = lambda d: dict(zip(vintage_points(session, [S], d)["obs_date"], vintage_points(session, [S], d)["value"]))  # noqa: E731
    assert at(date(2026, 3, 9)) == {}                                   # 第一期之前：什麼都還沒發佈
    assert at(date(2026, 4, 30)) == {M: 2790.0, date(2026, 4, 1): 2795.0}
    assert at(date(2026, 5, 12)) == {M: 2812.0, date(2026, 4, 1): 2795.0}


def test_engine_falls_back_before_the_first_issue(session, cfg):
    write_observations(session, [Obs(S, date(2026, 1, 1), 2700.0)], "demo")    # 沒有期號的舊數據（功能上線前）
    session.query(ObservationVintage).delete()                                  # 當作從前導入的：沒有期記錄
    write_observations(session, [Obs(S, M, 2790.0, vintage=date(2026, 3, 10))], "manual")
    write_observations(session, [Obs(S, M, 2812.0, vintage=date(2026, 5, 12))], "manual")
    session.commit()
    early = IndicatorEngine(session, cfg, vintage=date(2026, 2, 1)).values(S)      # 第一期之前：沿用主表（近似）
    assert early[pd.Timestamp(M)] == 2812.0 and pd.Timestamp(2026, 1, 1) in early.index
    april = IndicatorEngine(session, cfg, vintage=date(2026, 4, 30)).values(S)     # 有期記錄：還原那一期
    assert april[pd.Timestamp(M)] == 2790.0
    assert IndicatorEngine(session, cfg).values(S)[pd.Timestamp(M)] == 2812.0      # 平常：最新一期


def test_backtest_uses_the_issue_published_at_the_time(session, cfg):
    """每期報告都把最近一個月的數字往上修 50：按期還原的分數和用最終數字算的不同，且不再算「近似」。"""
    months = pd.date_range("2021-01-01", "2026-06-01", freq="MS")
    final = {m.date(): 2800.0 + 10 * ((i * 7) % 11) for i, m in enumerate(months)}
    obs = []
    for i, m in enumerate(months[:-1]):                  # 第 i+1 個月 10 日發佈：上個月首發偏低 50，再早的已經是最終值
        issue = (months[i + 1] + pd.Timedelta(days=9)).date()
        obs += [Obs(S, d, v - (50 if d == m.date() else 0), vintage=issue)
                for d, v in final.items() if d <= m.date()]
    write_observations(session, obs, "manual")
    session.commit()
    signal = SignalDef(code="SIG_T", name_zh="t", module="balance", input=S, direction=-1, lookback_days=1500,
                       min_points=12)
    small = dataclasses.replace(cfg, signals={"SIG_T": signal})
    dates = pd.bdate_range("2023-01-02", "2026-05-29")[::5]
    engine = IndicatorEngine(session, small)
    scores, exact = backtest._observation_scores(engine, small, dates)
    assert exact == {"SIG_T"}
    t = dates[100]
    issue = max(pd.Timestamp(r.vintage) for r in session.query(ObservationVintage.vintage).distinct()
                if pd.Timestamp(r.vintage) <= t)
    replay = IndicatorEngine(session, small, vintage=issue.date()).values(S)
    expected = backtest.rolling_scores(replay[replay.index <= t], 1500, 12, -1).iloc[-1]["score"]
    assert scores.loc[t, "SIG_T"] == pytest.approx(expected)
    naive = backtest._known_scores(small, signal, engine.values(S), dates, 40)
    assert (scores["SIG_T"] - naive).abs().max() > 1e-6                  # 和用修訂後數字算的確實不同


def test_import_with_issue_date(session, settings):
    from crudelab.imports import ImportFlowError, commit_batch, preview_file
    csv = b"Date,STEO OECD Stocks\n2026-03-01,2790\n2026-04-01,2795\n"
    with pytest.raises(ImportFlowError, match="晚於今天"):
        preview_file(session, settings, csv, "steo.csv", "reuters_xls", vintage=date(2099, 1, 1))
    preview = preview_file(session, settings, csv, "steo.csv", "reuters_xls", vintage=date(2026, 3, 10))
    assert preview["sheets"][0]["columns"][0]["series_code"] == S and preview["vintage"] == "2026-03-10"
    commit_batch(session, preview["batch_id"])
    session.commit()
    assert issues(session) == [(date(2026, 3, 10), 2790.0)]
    other = preview_file(session, settings, b"Date,LCOc1\n2026-03-02,70\n2026-03-03,71\n", "b.csv", "reuters_xls",
                         vintage=date(2026, 3, 10))
    assert any("不是按期保存的" in w for w in other["warnings"])


def test_one_file_with_several_issues_of_the_same_point(session):
    """一個文件裡有同一個月的好幾期（修訂歷史表）：每一期都要記下，主表取最新一期。"""
    write_observations(session, [Obs(S, M, 2790.0, vintage=date(2026, 3, 10)),
                                 Obs(S, M, 2805.0, vintage=date(2026, 5, 12)),
                                 Obs(S, M, 2800.0, vintage=date(2026, 4, 7))], "manual")
    session.commit()
    assert issues(session) == [(date(2026, 3, 10), 2790.0), (date(2026, 4, 7), 2800.0), (date(2026, 5, 12), 2805.0)]
    assert current(session) == 2805.0


# ------------------------------ 外部測試回報的三個問題（4577b58） ------------------------------

def at(session, day):
    frame = vintage_points(session, [S], day)
    return dict(zip(frame["obs_date"], frame["value"], strict=True)).get(M)


def test_backfilling_an_older_issue_keeps_a_newer_confirmation(session):
    """3 月報 100、5 月也報 100，之後補錄 4 月的 120：主表和 5 月的還原都應是 100（以前都變成 120）。"""
    write_observations(session, [Obs(S, M, 100.0, vintage=date(2026, 3, 10))], "manual")
    write_observations(session, [Obs(S, M, 100.0, vintage=date(2026, 5, 12))], "manual")
    write_observations(session, [Obs(S, M, 120.0, vintage=date(2026, 4, 7))], "manual")
    session.commit()
    assert current(session) == 100.0
    assert at(session, date(2026, 5, 12)) == 100.0 and at(session, date(2026, 4, 30)) == 120.0
    assert at(session, date(2026, 3, 31)) == 100.0


def test_one_batch_with_an_older_and_a_newer_issue_updates_the_main_table(session):
    """已有 3 月的 100，同一批寫入 2 月的 90 和 4 月的 110：主表應是 110（以前停在 100）。"""
    write_observations(session, [Obs(S, M, 100.0, vintage=date(2026, 3, 10))], "manual")
    write_observations(session, [Obs(S, M, 90.0, vintage=date(2026, 2, 10)),
                                 Obs(S, M, 110.0, vintage=date(2026, 4, 7))], "manual")
    session.commit()
    assert current(session) == 110.0
    assert issues(session) == [(date(2026, 2, 10), 90.0), (date(2026, 3, 10), 100.0), (date(2026, 4, 7), 110.0)]


def test_purging_a_source_also_removes_its_issue_history(session, settings, capsys, monkeypatch):
    from crudelab.cli import main
    monkeypatch.setenv("DATABASE_URL", settings.database_url)          # 命令行連到測試用的同一個數據庫
    from crudelab.models import AuditLog
    write_observations(session, [Obs(S, M, 100.0, vintage=date(2026, 3, 10))], "demo")
    write_observations(session, [Obs(S, M, 120.0, vintage=date(2026, 5, 12))], "demo")
    session.commit()
    assert main(["purge-source", "demo", "--yes"]) == 0
    session.expire_all()
    assert current_or_none(session) is None and at(session, date(2026, 5, 12)) is None    # 以前還能查到 120
    log = session.query(AuditLog).filter(AuditLog.action == "purge").one()
    assert log.detail["deleted_issues"] == 2


def test_daily_pulls_extend_a_run_and_a_backfill_inside_it_is_bounded(session):
    """每天自動取數（不帶報告期）數字沒變：不加行，只延長「確認到哪天」。之後補錄落在這段期間中間的一期：
    那一期顯示補錄的值，第二天起恢復每天取數確認的值，主表不變。"""
    from datetime import datetime
    for day in (1, 2, 3, 20):
        write_observations(session, [Obs(S, M, 100.0)], "manual", now=datetime(2026, 4, day, 12))
    session.commit()
    assert issues(session) == [(date(2026, 4, 1), 100.0)]                                  # 一行，確認到 4 月 20 日
    write_observations(session, [Obs(S, M, 120.0, vintage=date(2026, 4, 7))], "manual",
                       now=datetime(2026, 4, 21, 12))
    session.commit()
    assert current(session) == 100.0
    assert at(session, date(2026, 4, 7)) == 120.0
    assert at(session, date(2026, 4, 8)) == 100.0 and at(session, date(2026, 4, 20)) == 100.0


def current_or_none(session):
    row = session.query(Observation).filter(Observation.series_code == S, Observation.obs_date == M).one_or_none()
    return None if row is None else row.value
