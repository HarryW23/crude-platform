"""調度器：決定每個定時任務什麼時候該跑，到點就交給 jobs.py 執行。

時間的算法是純函數（due_slot / next_slot），和執行分開，方便測試。
  - 每個任務在任意時刻都有一個「最近一次該跑的時點」（slot）。調度器每隔 TICK_SECONDS 看一次：
    如果這個 slot 還沒被執行過，就認領並執行。認領靠數據庫的唯一約束，所以多個進程同時跑調度也不會重複執行。
  - 程序沒開而錯過的任務：按時刻跑的（at）在 catch_up_hours 之內補跑一次，超過就放棄；
    按間隔跑的（every_minutes）不補，直接等下一次。
  - 任務一個一個順序執行，不並行：量很小，順序執行最不容易出問題。
時區在 schedule.yaml 裡指定；數據庫裡一律存 UTC。
"""
from __future__ import annotations

import logging
import socket
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, jobs
from .config import Config
from .models import JobRun, SchedulerHeartbeat, utcnow
from .schema import WEEKDAYS, JobDef
from .settings import Settings

TICK_SECONDS = 30
HEARTBEAT_FRESH = timedelta(minutes=2)
log = logging.getLogger("crudelab.scheduler")
_EPOCH = datetime(2000, 1, 1)


def _utc(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


def _matches(job: JobDef, day) -> bool:
    return day.weekday() in job.weekdays and (job.day_of_month is None or day.day == job.day_of_month)


def _at(job: JobDef, day, tz: ZoneInfo) -> datetime:
    hour, minute = map(int, job.at.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def due_slot(job: JobDef, now: datetime, tz: ZoneInfo) -> datetime | None:
    """最近一次該跑的時點（UTC，不晚於 now）。now 是不帶時區的 UTC。"""
    if job.every_minutes is not None:
        step = job.every_minutes * 60
        return _EPOCH + timedelta(seconds=int((now - _EPOCH).total_seconds()) // step * step)
    local_now = now.replace(tzinfo=UTC).astimezone(tz)
    for back in range(0, 63):
        day = (local_now - timedelta(days=back)).date()
        if _matches(job, day) and _at(job, day, tz) <= local_now:
            return _utc(_at(job, day, tz))
    return None


def next_slot(job: JobDef, now: datetime, tz: ZoneInfo) -> datetime | None:
    """下一次該跑的時點（UTC，晚於 now）。"""
    if job.every_minutes is not None:
        return due_slot(job, now, tz) + timedelta(minutes=job.every_minutes)
    local_now = now.replace(tzinfo=UTC).astimezone(tz)
    for ahead in range(0, 63):
        day = (local_now + timedelta(days=ahead)).date()
        if _matches(job, day) and _at(job, day, tz) > local_now:
            return _utc(_at(job, day, tz))
    return None


def should_run(job: JobDef, slot: datetime | None, now: datetime) -> bool:
    if not job.enabled or slot is None:
        return False
    if job.every_minutes is not None:
        return True                                   # 間隔任務：當前這一格還沒跑就跑
    return now - slot <= timedelta(hours=job.catch_up_hours)


def describe(job: JobDef) -> str:
    """給人看的時間說明。"""
    if job.every_minutes is not None:
        return f"每 {job.every_minutes} 分鐘"
    names = dict(zip(WEEKDAYS, "一二三四五六日", strict=True))
    if isinstance(job.days, str):
        days = {"daily": "每天", "weekdays": "周一至周五"}[job.days]
    else:
        days = "每周" + "、".join(names[d] for d in job.days)
    if job.day_of_month is not None:
        days = f"每月 {job.day_of_month} 號" + ("" if job.days == "daily" else f"（且逢{days}）")
    return f"{days} {job.at}"


def tick(cfg: Config, settings: Settings, now: datetime | None = None) -> list[dict]:
    """看一遍所有任務，執行到點的。回傳這一輪執行的結果。"""
    now = now or utcnow()
    with db.session_scope() as session:
        beat = session.get(SchedulerHeartbeat, 1)
        if beat is None:
            session.add(SchedulerHeartbeat(id=1, at=now, host=socket.gethostname()[:128]))
        else:
            beat.at, beat.host = now, socket.gethostname()[:128]
    tz = ZoneInfo(cfg.schedule.timezone)
    results = []
    for job in cfg.schedule.jobs:
        slot = due_slot(job, now, tz)
        if not should_run(job, slot, now):
            continue
        run_id = jobs.claim(job.code, slot, "schedule")
        if run_id is None:                            # 這個時點已經執行過（或別的進程正在執行）
            continue
        result = jobs.execute(cfg, settings, job, run_id)
        log.warning("定時任務 %s：%s　%s", job.code, result["status"], result["message"])
        results.append(result)
    return results


def status(cfg: Config, now: datetime | None = None) -> dict:
    """給頁面用：調度器是否在運行、每個任務的時間安排和最近一次結果。"""
    now = now or utcnow()
    tz = ZoneInfo(cfg.schedule.timezone)

    def local(value: datetime | None) -> str | None:
        return value.replace(tzinfo=UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else None

    with db.session_scope() as session:
        beat = session.get(SchedulerHeartbeat, 1)
        out_jobs = []
        for job in cfg.schedule.jobs:
            last = next(iter(jobs.recent_runs(session, 1, job.code)), None)
            out_jobs.append({
                "code": job.code, "name_zh": job.name_zh, "enabled": job.enabled, "action": job.action,
                "schedule": describe(job), "next_run": local(next_slot(job, now, tz)) if job.enabled else None,
                "last": None if last is None else {"status": last.status, "message": last.message,
                                                   "trigger": last.trigger, "at": local(last.started_at)}})
        return {"timezone": cfg.schedule.timezone, "now": local(now),
                "running": beat is not None and now - beat.at <= HEARTBEAT_FRESH,
                "last_heartbeat": local(beat.at) if beat else None, "jobs": out_jobs}


def run_view(run: JobRun, tz: ZoneInfo) -> dict:
    def local(value: datetime | None) -> str | None:
        return value.replace(tzinfo=UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else None
    seconds = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    return {"id": run.id, "job": run.job_code, "trigger": run.trigger, "status": run.status,
            "started_at": local(run.started_at), "seconds": seconds, "message": run.message, "detail": run.detail}
