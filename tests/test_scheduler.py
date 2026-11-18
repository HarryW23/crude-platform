"""定時任務：時間算法、認領、三種動作、失敗處理。"""
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from sqlalchemy import func, select

from crudelab import db, jobs, scheduler
from crudelab.config import ConfigError, load_config
from crudelab.connectors.base import ConnectorError, ConnectorNotReady
from crudelab.ingest import Obs
from crudelab.models import ImportBatch, JobRun, Observation, Report
from crudelab.schema import JobDef

TZ = ZoneInfo("Asia/Shanghai")


def job(**kwargs) -> JobDef:
    return JobDef(**{"code": "J", "name_zh": "測試", "action": "report", "kind": "daily", **kwargs})


def utc(*args) -> datetime:
    return datetime(*args)


# ------------------------------ 時間 ------------------------------

def test_daily_and_weekday_slots():
    daily = job(at="18:30")
    # 2026-11-17 是周二。上海 18:30 = UTC 10:30
    assert scheduler.due_slot(daily, utc(2026, 11, 17, 10, 29), TZ) == utc(2026, 11, 16, 10, 30)
    assert scheduler.due_slot(daily, utc(2026, 11, 17, 10, 30), TZ) == utc(2026, 11, 17, 10, 30)
    assert scheduler.next_slot(daily, utc(2026, 11, 17, 10, 30), TZ) == utc(2026, 11, 18, 10, 30)

    weekdays = job(at="18:30", days="weekdays")
    monday = utc(2026, 11, 16, 3, 0)                                        # 周一上午：最近一次是上周五
    assert scheduler.due_slot(weekdays, monday, TZ) == utc(2026, 11, 13, 10, 30)
    assert scheduler.next_slot(weekdays, utc(2026, 11, 13, 11, 0), TZ) == utc(2026, 11, 16, 10, 30)

    friday = job(at="19:00", days=["fri"])
    assert scheduler.due_slot(friday, utc(2026, 11, 17, 0, 0), TZ) == utc(2026, 11, 13, 11, 0)
    early = job(at="07:30")                                                 # 上海 07:30 是 UTC 前一天 23:30
    assert scheduler.due_slot(early, utc(2026, 11, 17, 0, 0), TZ) == utc(2026, 11, 16, 23, 30)


def test_monthly_and_interval_slots():
    monthly = job(at="09:00", day_of_month=1)
    assert scheduler.due_slot(monthly, utc(2026, 11, 17, 0, 0), TZ) == utc(2026, 11, 1, 1, 0)
    assert scheduler.next_slot(monthly, utc(2026, 11, 17, 0, 0), TZ) == utc(2026, 12, 1, 1, 0)
    every = job(every_minutes=10)
    assert scheduler.due_slot(every, utc(2026, 11, 17, 10, 37, 12), TZ) == utc(2026, 11, 17, 10, 30)
    assert scheduler.next_slot(every, utc(2026, 11, 17, 10, 37, 12), TZ) == utc(2026, 11, 17, 10, 40)
    assert scheduler.describe(every) == "每 10 分鐘"
    assert scheduler.describe(job(at="18:30", days="weekdays")) == "周一至周五 18:30"
    assert scheduler.describe(job(at="19:00", days=["fri"])) == "每周五 19:00"
    assert scheduler.describe(monthly) == "每月 1 號 09:00"


def test_catch_up_policy():
    daily = job(at="18:30", catch_up_hours=12)
    slot = utc(2026, 11, 16, 10, 30)
    assert scheduler.should_run(daily, slot, slot + timedelta(hours=11))    # 錯過不久：補跑
    assert not scheduler.should_run(daily, slot, slot + timedelta(hours=13))  # 錯過太久：放棄，等下一次
    assert not scheduler.should_run(job(at="18:30", enabled=False), slot, slot)
    assert not scheduler.should_run(job(at="18:30", catch_up_hours=0), slot, slot + timedelta(minutes=1))
    assert scheduler.should_run(job(every_minutes=10), slot, slot + timedelta(days=3))


@pytest.mark.parametrize("fields, message", [
    ({"at": "18:30", "every_minutes": 5}, "只能寫一個"), ({}, "必須寫一個"), ({"at": "25:00"}, "pattern"),
    ({"every_minutes": 5, "days": "weekdays"}, "不能和 days"), ({"at": "08:00", "action": "pull"}, "需要 source"),
    ({"at": "08:00", "action": "report", "kind": None}, "需要 kind"),
    ({"every_minutes": 5, "action": "import_folder", "source": "reuters_xls", "folder": "../etc"}, "相對路徑"),
])
def test_job_definition_is_validated(fields, message):
    with pytest.raises(ValueError, match=message):
        job(**fields)


def test_schedule_config_cross_checks(tmp_path):
    import shutil

    from crudelab.settings import PROJECT_ROOT
    target = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", target)
    path = target / "schedule.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["timezone"] = "Mars/Olympus"
    doc["jobs"][0]["source"] = "eia"                                        # 收件夾任務配了一個接口型數據源
    doc["jobs"][1]["source"] = "nope"
    doc["jobs"].append(dict(next(j for j in doc["jobs"] if j["code"] == "REPORT_DAILY")))
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(target)
    for expected in ("不認識的時區", "需要 excel 類型", "數據源 nope 不存在", "REPORT_DAILY 重複定義"):
        assert expected in str(exc.value)


# ------------------------------ 執行 ------------------------------

@pytest.fixture()
def ready(session, settings, cfg, monkeypatch):
    monkeypatch.setattr(jobs, "SETTLE_SECONDS", 0)
    return settings


def test_only_one_process_can_claim_a_slot(ready):
    slot = utc(2026, 11, 17, 10, 30)
    first = jobs.claim("REPORT_DAILY", slot, "schedule")
    assert first is not None and jobs.claim("REPORT_DAILY", slot, "schedule") is None
    assert jobs.claim("REPORT_DAILY", slot + timedelta(days=1), "schedule") is not None
    assert jobs.claim("REPORT_WEEKLY", slot, "schedule") is not None


def test_tick_runs_due_jobs_once(ready, cfg, session):
    now = utc(2026, 11, 17, 10, 31)                                         # 周二 上海 18:31：日報剛到點
    results = scheduler.tick(cfg, ready, now)
    by_job = {r["job"]: r for r in results}
    assert by_job["REPORT_DAILY"]["status"] == "ok" and by_job["IMPORT_INBOX"]["status"] == "skipped"
    assert by_job["PULL_EIA"]["status"] == "skipped" and "EIA_API_KEY" in by_job["PULL_EIA"]["message"]
    assert "REPORT_WEEKLY" not in by_job and "REPORT_MONTHLY" not in by_job  # 周報上周五的已超過補跑時限；月報停用
    assert scheduler.tick(cfg, ready, now + timedelta(seconds=30)) == []     # 同一個時點不重跑
    later = scheduler.tick(cfg, ready, now + timedelta(minutes=10))
    assert [r["job"] for r in later] == ["IMPORT_INBOX"]                     # 只有間隔任務進了下一格
    assert session.scalar(select(func.count()).select_from(Report)) == 1

    status = scheduler.status(cfg, now + timedelta(minutes=10))
    daily = next(j for j in status["jobs"] if j["code"] == "REPORT_DAILY")
    assert status["running"] and daily["next_run"] == "2026-11-18T10:30:00Z" and daily["last"]["status"] == "ok"
    # 「最近一次」記的是實際執行的時刻（真實時鐘），只檢查它是今天（按上海時區），不寫死日期
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert daily["last"]["at"].startswith(today) and daily["schedule"] == "周一至周五 18:30"
    assert next(j for j in status["jobs"] if j["code"] == "REPORT_MONTHLY")["next_run"] is None
    assert not scheduler.status(cfg, now + timedelta(hours=1))["running"]    # 心跳過期 → 顯示未運行


def test_inbox_import_moves_files_and_reports_problems(ready, cfg, session, sample_path):
    inbox = ready.data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "daily.xlsx").write_bytes(sample_path.read_bytes())
    (inbox / "broken.xlsx").write_bytes(b"this is not a workbook")
    (inbox / "notes.txt").write_text("不是數據文件，應該被忽略", encoding="utf-8")
    fresh = inbox / "still-copying.csv"
    fresh.write_text("Date,LCOc1\n2026-09-01,70\n", encoding="utf-8")
    result = jobs.run_now(cfg, ready, "IMPORT_INBOX")
    assert result["status"] == "failed" and "導入 2 個文件" in result["message"] and "1 個文件失敗" in result["message"]
    assert "沒有認出來" in result["message"]                                 # 演示文件裡有一列認不出
    assert sorted(p.name for p in inbox.iterdir()) == ["done", "failed", "notes.txt"]
    assert len(list((inbox / "done").iterdir())) == 2 and len(list((inbox / "failed").iterdir())) == 1
    assert session.scalar(select(func.count()).select_from(Observation)) > 1000
    assert session.scalar(select(func.count()).select_from(ImportBatch).where(ImportBatch.status == "committed")) == 2
    run = jobs.recent_runs(session, 1)[0]
    files = {f["file"]: f for f in run.detail["files"]}
    assert "無法打開" in files["broken.xlsx"]["error"] and files["daily.xlsx"]["inserted"] > 1000
    assert run.trigger == "manual"
    assert jobs.run_now(cfg, ready, "IMPORT_INBOX")["status"] == "skipped"   # 收件夾空了


def test_files_still_being_written_are_left_alone(session, settings, cfg, monkeypatch):
    inbox = settings.data_dir / "inbox"
    inbox.mkdir(parents=True)
    path = inbox / "x.csv"
    path.write_text("Date,LCOc1\n2026-09-01,70\n2026-09-02,71\n", encoding="utf-8")
    assert jobs.run_now(cfg, settings, "IMPORT_INBOX")["status"] == "skipped" and path.exists()
    old = time.time() - 120
    os.utime(path, (old, old))
    assert jobs.run_now(cfg, settings, "IMPORT_INBOX")["status"] == "ok" and not path.exists()


class _FakeConnector:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def fetch(self, series, start, end):
        return self.behaviour()


def test_pull_outcomes_and_no_leak_on_failure(ready, cfg, session, monkeypatch):
    secret = "canary-key-55aa77bb99"                                        # secret-scan: ok
    monkeypatch.setenv("EIA_API_KEY", secret)

    def use(behaviour):
        monkeypatch.setattr(jobs, "get_connector", lambda code: _FakeConnector(behaviour))
        return jobs.run_now(cfg, ready, "PULL_EIA")

    ok = use(lambda: [Obs("EIA_CRUDE_STOCKS", datetime(2026, 10, 23).date(), 430000.0)])
    assert ok["status"] == "ok" and "新增 1" in ok["message"]

    def not_ready():
        raise ConnectorNotReady("還沒有設定密鑰")

    def refused():
        raise ConnectorError("EIA 回報錯誤 500")

    def unexpected():
        raise RuntimeError(f"GET https://api.eia.gov/x?api_key={secret} blew up")

    assert use(not_ready)["status"] == "skipped"
    failed = use(refused)
    assert (failed["status"], failed["message"]) == ("failed", "EIA 回報錯誤 500")
    crashed = use(unexpected)
    assert crashed["status"] == "failed" and "RuntimeError" in crashed["message"] and secret not in crashed["message"]
    assert all(secret not in (r.message or "") + str(r.detail) for r in jobs.recent_runs(session))
    assert jobs.run_now(cfg, ready, "REPORT_DAILY")["status"] == "ok"        # 一個任務崩了不影響後面的
    with pytest.raises(jobs.JobError, match="沒有名為"):
        jobs.run_now(cfg, ready, "NOPE")


def test_interrupted_runs_are_marked_failed(ready, session):
    old = jobs.claim("REPORT_DAILY", utc(2026, 11, 1, 0, 0), "schedule")
    recent = jobs.claim("REPORT_DAILY", utc(2026, 11, 2, 0, 0), "schedule")
    with db.session_scope() as s:
        s.get(JobRun, old).started_at -= timedelta(hours=3)
    assert jobs.recover_stale() == 1
    assert session.get(JobRun, old).status == "failed" and "中斷" in session.get(JobRun, old).message
    assert session.get(JobRun, recent).status == "running"


def test_schedule_api_is_admin_only(sites, monkeypatch):
    front, admin = sites["front"], sites["admin"]
    assert front.get("/api/schedule").status_code == 404
    assert front.post("/api/schedule/REPORT_DAILY/run").status_code == 404
    status = admin.get("/api/schedule").json()
    assert status["running"] is False and [j["code"] for j in status["jobs"]][0] == "IMPORT_INBOX"
    assert admin.post("/api/schedule/REPORT_DAILY/run").json()["status"] == "ok"
    assert admin.post("/api/schedule/NOPE/run").status_code == 404
    runs = admin.get("/api/schedule/runs").json()
    assert runs[0]["job"] == "REPORT_DAILY" and runs[0]["trigger"] == "manual" and runs[0]["seconds"] is not None
    assert admin.get("/api/schedule/runs?job=PULL_EIA").json() == []
    assert "job_run" in {a["action"] for a in admin.get("/api/audit").json()}


def test_iso_utc_marks_timezone():
    from datetime import UTC, datetime, timedelta, timezone

    from crudelab.models import iso_utc
    assert iso_utc(None) is None
    assert iso_utc(datetime(2026, 11, 18, 10, 30)) == "2026-11-18T10:30:00Z"          # 數據庫裡的時間：不帶時區的 UTC
    shanghai = datetime(2026, 11, 18, 18, 30, tzinfo=timezone(timedelta(hours=8)))
    assert iso_utc(shanghai) == "2026-11-18T10:30:00Z"
    assert iso_utc(datetime(2026, 11, 18, 10, 30, 15, 999, tzinfo=UTC)) == "2026-11-18T10:30:15Z"
