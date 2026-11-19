"""調度器：決定每個定時任務什麼時候該跑，到點就交給 jobs.py 執行。

時間的算法是純函數（due_slot / next_slot），和執行分開，方便測試。
  - 每個任務在任意時刻都有一個「最近一次該跑的時點」（slot）。調度器每隔 TICK_SECONDS 看一次：
    如果這個 slot 還沒被執行過，就認領並執行。認領靠數據庫的唯一約束，所以多個進程同時跑調度也不會重複執行。
  - 程序沒開而錯過的任務：按時刻跑的（at）在 catch_up_hours 之內補跑一次，超過就放棄；
    按間隔跑的（every_minutes）不補，直接等下一次。
  - 任務一個一個順序執行，不並行：量很小，順序執行最不容易出問題。
  - 失敗重試（retries）：失敗後隔 retry_minutes 再認領同一個時點的下一次嘗試（attempt + 1）。
  - 依賴（after）：到點後先看依賴的任務各自「最近一次該跑的那次」有沒有執行完；沒有就等（下一輪再看），
    最多等 wait_minutes，等不到就照常執行並在結果裡註明。依賴被停用或錯過了補跑期限（不會再跑）時不等。
時區在 schedule.yaml 裡指定；數據庫裡一律存 UTC。
"""
from __future__ import annotations

import logging
import socket
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import db, jobs, notifications, switches
from .config import Config
from .models import JobRun, SchedulerHeartbeat, iso_utc, utcnow
from .schema import WEEKDAYS, JobDef
from .settings import Settings

TICK_SECONDS = 30
HEARTBEAT_FRESH = timedelta(minutes=2)
log = logging.getLogger("crudelab.scheduler")
_EPOCH = datetime(2000, 1, 1)
_last_prune: datetime | None = None


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


def should_run(job: JobDef, slot: datetime | None, now: datetime, enabled: bool | None = None) -> bool:
    """enabled：這個任務的開關目前的實際狀態（後台點過就以後台為準）；不給就用配置裡的預設值。"""
    if not (job.enabled if enabled is None else enabled) or slot is None:
        return False
    if job.every_minutes is not None:
        return True                                   # 間隔任務：當前這一格還沒跑就跑
    return now - slot <= timedelta(hours=job.catch_up_hours)


def describe(job: JobDef) -> str:
    """給人看的時間說明（含重試和依賴）。"""
    extra = (f"，失敗後每 {job.retry_minutes} 分鐘重試，最多 {job.retries} 次" if job.retries else "") + \
        (f"，等 {'、'.join(job.after)} 完成後執行" if job.after else "")
    return _when(job) + extra


def _when(job: JobDef) -> str:
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
        state = switches.load(session, cfg)
    global _last_prune
    if _last_prune is None or now - _last_prune >= timedelta(hours=6):    # 順便清理沒用的歷史記錄
        _last_prune = now
        try:
            jobs.prune_history(now)
        except Exception:
            log.exception("清理任務歷史時出錯")
    if not state[switches.SCHEDULER]:                 # 總開關關著：只報心跳，什麼都不執行
        return []
    tz = ZoneInfo(cfg.schedule.timezone)
    results = []
    for job in cfg.schedule.jobs:
        slot = due_slot(job, now, tz)
        for stale in jobs.abandon_retries(job.code, slot):                 # 被下一個時點取代的待重試：定為失敗並通知
            results.append(_notify(cfg, job, stale, {"id": stale, "job": job.code, "status": "failed",
                                                     "message": "上次失敗後沒能重試"}))
        enabled = state[switches.job_key(job.code)]
        if not enabled or slot is None:
            continue
        with db.session_scope() as session:
            last = jobs.latest_attempt(session, job.code, slot)
            if last is None:                                                # 這個時點第一次：補跑期限、依賴
                attempt = 1 if should_run(job, slot, now, enabled) else None
                waiting, failed = blocking(session, cfg, job, slot, now, tz, state) if attempt else ([], [])
            else:                                                           # 同一個時點的重試：不受補跑期限限制
                attempt = jobs.retry_due(session, job, slot, now)
                waiting, failed = [], []
        if attempt is None:
            continue
        note = None
        if waiting:
            waited = now - slot
            if waited < timedelta(minutes=job.wait_minutes):
                continue                                                    # 下一輪再看
            minutes = int(waited.total_seconds() // 60)
            note = f"等了 {minutes} 分鐘，{'、'.join(_names(cfg, waiting))} 還沒完成，照常執行"
        elif failed:
            note = f"注意：{'、'.join(_names(cfg, failed))} 最近一次失敗了"
        run_id = jobs.claim(job.code, slot, "schedule", attempt, now)
        if run_id is None:                            # 這次嘗試已經執行過（或別的進程正在執行）
            continue
        result = jobs.execute(cfg, settings, job, run_id, retries_left=job.retries - attempt + 1, note=note)
        log.warning("定時任務 %s：%s　%s", job.code, result["status"], result["message"])
        results.append(_notify(cfg, job, run_id, result))
    return results


def _notify(cfg: Config, job: JobDef, run_id: int, result: dict) -> dict:
    result["notified"] = None
    try:
        result["notified"] = notifications.after_run(cfg, job, run_id)
    except Exception:                                 # 通知出錯不能影響調度
        log.exception("為任務 %s 發通知時出錯", job.code)
    return result


def _names(cfg: Config, codes: list[str]) -> list[str]:
    return [jobs.find_job(cfg, c).name_zh for c in codes]


def blocking(session, cfg: Config, job: JobDef, slot: datetime, now: datetime, tz: ZoneInfo,
             state: dict) -> tuple[list[str], list[str]]:
    """job 在 slot 這個時點的依賴：（還沒執行完的, 最終失敗了的）。

    每個依賴看它「在 slot 之前（含）最近一次該跑的時點」：那個時點之後（含）有沒有一次已經結束的執行
    （手動運行也算）。依賴被停用、或錯過了補跑期限不會再跑時，不等它。
    """
    waiting, failed = [], []
    for code in job.after:
        dep = jobs.find_job(cfg, code)
        if not state.get(switches.job_key(code), dep.enabled):
            continue
        dep_slot = due_slot(dep, slot, tz)
        if dep_slot is None:
            continue
        run = session.scalar(select(JobRun).where(JobRun.job_code == code, JobRun.slot >= dep_slot)
                             .order_by(JobRun.id.desc()).limit(1))
        if run is None:                  # 還沒跑：錯過了補跑期限就不會再跑，不等；否則等它
            if dep.every_minutes is not None or should_run(dep, dep_slot, now, True):
                waiting.append(code)
        elif run.status in ("running", "retry"):
            waiting.append(code)
        elif run.status == "failed":
            failed.append(code)
    return waiting, failed


def status(cfg: Config, now: datetime | None = None) -> dict:
    """給頁面用：調度器是否在運行、每個任務的時間安排和最近一次結果。"""
    now = now or utcnow()
    tz = ZoneInfo(cfg.schedule.timezone)
    with db.session_scope() as session:
        beat = session.get(SchedulerHeartbeat, 1)
        state, changed = switches.load(session, cfg), switches.overridden(session)
        master = state[switches.SCHEDULER]
        out_jobs = []
        for job in cfg.schedule.jobs:
            last = next(iter(jobs.recent_runs(session, 1, job.code)), None)
            key = switches.job_key(job.code)
            enabled = state[key]
            out_jobs.append({
                "code": job.code, "name_zh": job.name_zh, "enabled": enabled, "action": job.action,
                "switch": key, "default_enabled": job.enabled, "overridden": key in changed,
                "schedule": describe(job),
                "next_run": iso_utc(next_slot(job, now, tz)) if enabled and master else None,
                "last": None if last is None else {"status": last.status, "message": last.message,
                                                   "trigger": last.trigger, "at": iso_utc(last.started_at)}})
        return {"timezone": cfg.schedule.timezone, "now": iso_utc(now), "enabled": master,
                "failing": [j["code"] for j in out_jobs if j["last"] and j["last"]["status"] == "failed"],
                "running": beat is not None and now - beat.at <= HEARTBEAT_FRESH,
                "last_heartbeat": iso_utc(beat.at) if beat else None, "jobs": out_jobs}


def run_view(run: JobRun, tz: ZoneInfo | None = None) -> dict:
    seconds = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    return {"id": run.id, "job": run.job_code, "trigger": run.trigger, "status": run.status,
            "started_at": iso_utc(run.started_at), "seconds": seconds, "message": run.message, "detail": run.detail}
