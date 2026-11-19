"""定時任務的失敗重試和依賴（#43）：日報總是在數據導入後生成。"""
import dataclasses
from datetime import datetime, timedelta

import pytest

from crudelab import db, jobs, scheduler
from crudelab.connectors.base import ConnectorError
from crudelab.models import JobRun
from crudelab.schema import JobDef, ScheduleDef

EIA_SLOT = datetime(2026, 11, 16, 23, 30)      # 上海 周二 07:30
REPORT_SLOT = datetime(2026, 11, 17, 10, 30)   # 上海 周二 18:30


class Flaky:
    """前 fail 次取數失敗，之後成功。"""
    def __init__(self, fail: int):
        self.left = fail

    def fetch(self, *a):
        if self.left > 0:
            self.left -= 1
            raise ConnectorError("EIA 回報錯誤 503")
        return []


@pytest.fixture()
def only(session, settings, cfg, monkeypatch):
    """只保留指定任務的配置。"""
    monkeypatch.setattr(jobs, "SETTLE_SECONDS", 0)

    def make(*codes):
        return dataclasses.replace(cfg, schedule=ScheduleDef(
            timezone=cfg.schedule.timezone, jobs=[jobs.find_job(cfg, c) for c in codes]))
    return make


def eia(monkeypatch, fail: int) -> Flaky:
    connector = Flaky(fail)
    monkeypatch.setattr(jobs, "get_connector", lambda code: connector)
    return connector


def runs(code: str) -> list[tuple[int, str]]:
    with db.session_scope() as s:
        return [(r.attempt, r.status) for r in s.query(JobRun).filter(JobRun.job_code == code).order_by(JobRun.id)]


def test_retry_after_the_delay_and_no_false_recovery(only, settings, monkeypatch):
    cfg = only("PULL_EIA")
    eia(monkeypatch, fail=1)
    first = scheduler.tick(cfg, settings, EIA_SLOT + timedelta(minutes=1))
    assert first[0]["status"] == "retry" and "20 分鐘後自動重試" in first[0]["message"]
    assert first[0]["notified"] is None                                     # 還會重試：不發失敗通知
    assert scheduler.tick(cfg, settings, EIA_SLOT + timedelta(minutes=15)) == []      # 還沒到 20 分鐘
    second = scheduler.tick(cfg, settings, EIA_SLOT + timedelta(minutes=22))
    assert second[0]["status"] == "ok" and second[0]["attempt"] == 2
    assert second[0]["notified"] is None                                    # 沒發過失敗，也就沒有「已恢復」
    assert runs("PULL_EIA") == [(1, "retried"), (2, "ok")]
    assert scheduler.tick(cfg, settings, EIA_SLOT + timedelta(minutes=50)) == []      # 成功後不再跑


def test_notify_once_after_the_last_retry_fails(only, settings, monkeypatch):
    cfg = only("PULL_EIA")
    eia(monkeypatch, fail=99)
    notified = []
    for minutes in (1, 22, 43, 64, 85, 106):
        notified += [r["notified"] for r in scheduler.tick(cfg, settings, EIA_SLOT + timedelta(minutes=minutes))]
    assert runs("PULL_EIA") == [(1, "retried"), (2, "retried"), (3, "retried"), (4, "failed")]   # retries: 3
    assert notified == [None, None, None, "failure"]
    status = scheduler.status(cfg, EIA_SLOT + timedelta(minutes=110))
    assert status["failing"] == ["PULL_EIA"] and "最多 3 次" in status["jobs"][0]["schedule"]


def test_report_waits_for_imports_and_retries(only, settings, monkeypatch):
    cfg = only("IMPORT_INBOX", "PULL_EIA", "REPORT_DAILY")
    eia(monkeypatch, fail=2)
    done = lambda t: {r["job"]: r["status"] for r in scheduler.tick(cfg, settings, t)}  # noqa: E731
    t = REPORT_SLOT + timedelta(minutes=1)                    # 程序 18:31 才開：EIA 補跑（失敗，待重試）
    assert done(t) == {"IMPORT_INBOX": "skipped", "PULL_EIA": "retry"}      # 日報在等 EIA
    assert "REPORT_DAILY" not in done(t + timedelta(minutes=21))            # EIA 第 2 次還是失敗
    third = done(t + timedelta(minutes=42))                                 # EIA 成功，同一輪接著出日報
    assert third["PULL_EIA"] == "ok" and third["REPORT_DAILY"] == "ok"
    with db.session_scope() as s:
        report = s.query(JobRun).filter(JobRun.job_code == "REPORT_DAILY").one()
        pulled = s.query(JobRun).filter(JobRun.job_code == "PULL_EIA", JobRun.status == "ok").one()
        assert report.id > pulled.id and report.started_at >= pulled.started_at


def test_report_runs_anyway_with_a_note(only, settings, monkeypatch):
    cfg = only("IMPORT_INBOX", "PULL_EIA", "REPORT_DAILY")
    eia(monkeypatch, fail=99)
    t = REPORT_SLOT + timedelta(minutes=1)
    for minutes in (0, 21, 42, 63):                                          # EIA 重試到最終失敗
        last = {r["job"]: r for r in scheduler.tick(cfg, settings, t + timedelta(minutes=minutes))}
    assert last["PULL_EIA"]["status"] == "failed"
    assert last["REPORT_DAILY"]["status"] == "ok" and "取 EIA 數據 最近一次失敗了" in last["REPORT_DAILY"]["message"]

    # 依賴卡在「執行中」（每天一次的任務，這一天不會再跑別的）：最多等 wait_minutes，之後照常執行並註明
    base = only("PULL_EIA")
    cfg2 = dataclasses.replace(base, schedule=ScheduleDef(timezone=base.schedule.timezone, jobs=[
        *base.schedule.jobs, JobDef(code="REPORT_WEEKLY", name_zh="生成周報", action="report", kind="weekly",
                                    at="19:00", days=["fri"], after=["PULL_EIA"])]))
    friday = datetime(2026, 11, 20, 11, 0)                                   # 上海 周五 19:00
    with db.session_scope() as s:
        s.add(JobRun(job_code="PULL_EIA", slot=datetime(2026, 11, 19, 23, 30), trigger="schedule", status="running",
                     attempt=1, started_at=datetime(2026, 11, 19, 23, 30)))
    assert "REPORT_WEEKLY" not in {r["job"] for r in scheduler.tick(cfg2, settings, friday + timedelta(minutes=30))}
    late = {r["job"]: r for r in scheduler.tick(cfg2, settings, friday + timedelta(minutes=121))}
    assert late["REPORT_WEEKLY"]["status"] == "ok" and "等了 121 分鐘" in late["REPORT_WEEKLY"]["message"]


def test_disabled_dependency_is_not_waited_for(only, settings, monkeypatch, cfg):
    from crudelab import switches
    eia(monkeypatch, fail=0)
    narrowed = only("IMPORT_INBOX", "PULL_EIA", "REPORT_DAILY")
    with db.session_scope() as s:
        switches.set_switch(s, cfg, "job:PULL_EIA", False, "t")
    ran = {r["job"] for r in scheduler.tick(narrowed, settings, REPORT_SLOT + timedelta(minutes=1))}
    assert ran == {"IMPORT_INBOX", "REPORT_DAILY"}                         # 不等被停用的 EIA


def test_schedule_validation():
    base = {"name_zh": "x", "action": "report", "kind": "daily", "at": "18:30"}
    with pytest.raises(ValueError, match="不存在"):
        ScheduleDef(jobs=[JobDef(code="A", **base, after=["NOPE"])])
    with pytest.raises(ValueError, match="成環"):
        ScheduleDef(jobs=[JobDef(code="A", **base, after=["B"]), JobDef(code="B", **base, after=["A"])])
    with pytest.raises(ValueError, match="下一次定時執行"):
        JobDef(code="C", name_zh="x", action="news", every_minutes=10, retries=1, retry_minutes=15)
    with pytest.raises(ValueError, match="補跑期限"):
        JobDef(code="D", **base, after=["A"], wait_minutes=600, catch_up_hours=2)


def test_retries_continue_past_the_catch_up_window_and_never_dangle(only, settings, monkeypatch):
    """18:31 才補跑 07:30 的 EIA（補跑期限 12 小時）：之後的重試照樣進行；程序關到第二天，掛著的重試定為失敗並通知。"""
    cfg = only("PULL_EIA")
    eia(monkeypatch, fail=99)
    t = REPORT_SLOT + timedelta(minutes=1)
    for minutes in (0, 21, 42, 63):                                          # 最後一次已超過 07:30 + 12 小時
        scheduler.tick(cfg, settings, t + timedelta(minutes=minutes))
    assert runs("PULL_EIA") == [(1, "retried"), (2, "retried"), (3, "retried"), (4, "failed")]



def test_dangling_retry_is_finalised_and_notified(only, settings, monkeypatch):
    """程序在等重試的時候被關掉，到第二天才開：掛著的那次定為失敗並發失敗通知，不會永遠「待重試」。"""
    cfg = only("PULL_EIA")
    eia(monkeypatch, fail=0)
    with db.session_scope() as s:
        s.add(JobRun(job_code="PULL_EIA", slot=EIA_SLOT, trigger="schedule", status="retry",
                     attempt=1, message="EIA 回報錯誤 503", started_at=EIA_SLOT))
    next_day = scheduler.tick(cfg, settings, EIA_SLOT + timedelta(days=1, minutes=1))
    abandoned = [r for r in next_day if r["message"] == "上次失敗後沒能重試"]
    assert len(abandoned) == 1 and abandoned[0]["notified"] == "failure"
    assert runs("PULL_EIA") == [(1, "failed"), (1, "ok")]                   # 當天的照常執行
