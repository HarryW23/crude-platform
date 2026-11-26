"""定時任務的執行：各種動作，以及「認領 → 執行 → 記結果」的流程。什麼時候執行由 scheduler.py 決定。

每次執行都記一條 job_run，結果有四種：
  ok        做完了
  skipped   沒有可做的事（收件夾是空的、數據源還沒配密鑰）。不是錯誤
  failed    出錯了。訊息是脫敏過的一句話；意料之外的異常只記類型，原文進服務器日誌
  running   正在執行。進程被殺掉會留下這個狀態，下次啟動時由 scheduler.recover_stale 改成 failed
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import credentials, db
from . import reports as report_service
from .config import Config
from .connectors import ConnectorError, ConnectorNotReady, get_connector
from .imports import ImportFlowError, commit_batch, nothing_importable, preview_file
from .ingest import IngestError, write_observations
from .models import AuditLog, JobRun, Series, utcnow
from .schema import JobDef
from .settings import Settings

ACTOR = "scheduler"
IMPORTABLE = (".xlsx", ".xlsm", ".xls", ".csv", ".pdf")
PDF_SOURCE = "opec_momr"      # 收件夾裡的 PDF 只可能是 OPEC 月報（connectors/momr.py），按它的數據源記
KPLER_SOURCE = "kpler_xls"    # Kpler 終端下載的文件（文件名 World_monthly_exports_…，見 connectors/kpler.py）
KPLER_FILE = re.compile(r"^world_(monthly|weekly)_(exports|imports|inventories)_", re.I)


def inbox_source(path: Path, default: str) -> str:
    """收件夾裡的文件按什麼數據源記：OPEC 月報 PDF、Kpler 下載的表有自己的數據源，其他用任務設定的。"""
    if path.suffix.lower() == ".pdf":
        return PDF_SOURCE
    if KPLER_FILE.match(path.name):
        return KPLER_SOURCE
    return default
SETTLE_SECONDS = 30           # 文件最後一次修改後過這麼久才導入，避免讀到還沒複製完的文件
log = logging.getLogger("crudelab.jobs")

Result = tuple[str, str, dict]   # (status, message, detail)


class JobError(Exception):
    """任務出錯，訊息可以直接給人看。"""


# ------------------------------ 三種動作 ------------------------------

def _source(job: JobDef) -> str:
    if job.source is None:                      # JobDef 的校驗保證 pull / import_folder 一定有 source
        raise JobError(f"任務 {job.code} 沒有設定 source")
    return job.source


def _kind(job: JobDef) -> str:
    if job.kind is None:                        # JobDef 的校驗保證 report 一定有 kind
        raise JobError(f"任務 {job.code} 沒有設定 kind")
    return job.kind


def _pull(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    try:
        connector = get_connector(_source(job))
        observations = connector.fetch(list(session.scalars(select(Series).where(Series.active))), None, None)
    except KeyError:
        raise JobError(f"數據源 {job.source} 沒有對應的取數接口") from None
    except ConnectorNotReady as exc:
        return "skipped", str(exc), {}
    except ConnectorError as exc:
        raise JobError(str(exc)) from None
    stats = write_observations(session, observations, _source(job), actor=ACTOR)
    session.add(AuditLog(actor=ACTOR, action="pull", entity="data_source", entity_id=job.source, detail=stats))
    return "ok", f"新增 {stats['inserted']}，更新 {stats['updated']}，未變 {stats['unchanged']}", stats


def inbox_dir(settings: Settings, job: JobDef) -> Path:
    return settings.data_dir / job.folder


def _import_folder(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    """把收件夾裡的文件逐個導入（只入庫能自動識別的列），成功的移到 done/，失敗的移到 failed/。"""
    folder = inbox_dir(settings, job)
    for sub in (folder, folder / "done", folder / "failed"):
        sub.mkdir(parents=True, exist_ok=True)
    ready = [p for p in sorted(folder.iterdir())
             if p.is_file() and p.suffix.lower() in IMPORTABLE and time.time() - p.stat().st_mtime >= SETTLE_SECONDS]
    if not ready:
        return "skipped", "收件夾裡沒有待導入的文件", {}
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    files, failed = [], 0
    for path in ready:
        entry: dict = {"file": path.name}
        try:
            with session.begin_nested():
                source = inbox_source(path, _source(job))
                preview = preview_file(session, settings, path.read_bytes(), path.name, source, ACTOR)
                if (reason := nothing_importable(preview)) is not None:
                    raise ImportFlowError(reason)
                result = commit_batch(session, preview["batch_id"], actor=ACTOR)
            entry.update(inserted=result["inserted"], updated=result["updated"],
                         unmapped=result["unmapped_columns"], warnings=preview["warnings"])
            target = folder / "done"
        except (ImportFlowError, IngestError, OSError) as exc:
            entry["error"] = credentials.redact(exc)
            failed += 1
            target = folder / "failed"
        shutil.move(str(path), str(target / f"{stamp}_{path.name}"))
        files.append(entry)
    done = [f for f in files if "error" not in f]
    message = (f"導入 {len(done)} 個文件，新增 {sum(f['inserted'] for f in done)}，"
               f"更新 {sum(f['updated'] for f in done)}")
    unmapped = sum(f["unmapped"] for f in done)
    if unmapped:
        message += f"；有 {unmapped} 列沒有認出來，沒有導入（到「數據導入」頁手動上傳一次並指定對應，之後就能自動識別）"
    if failed:
        message += f"；{failed} 個文件失敗，已移到 failed/"
    return ("failed" if failed else "ok"), message, {"files": files}


def _report(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    try:
        report = report_service.generate(session, cfg, _kind(job), actor=ACTOR)
    except report_service.ReportError as exc:
        raise JobError(str(exc)) from None
    message, detail = f"已生成《{report.title}》", {"report_id": report.id}
    if job.narrative:
        from .llm import LLMError, LLMNotConfigured, get_provider
        from .llm.tasks import write_narrative
        try:
            provider = get_provider("narrative", cfg.llm)
            result = write_narrative(provider, report.markdown, report_service.KINDS[_kind(job)])
            report.narrative, report.narrative_meta = result.pop("text"), result
            message += "，含大模型解讀" + ("" if result["guard_passed"] else "（數字核對未通過）")
        except LLMNotConfigured:
            message += "；大模型未配置，沒有寫解讀"
        except LLMError as exc:
            message += f"；解讀失敗：{credentials.redact(exc)}"
    return "ok", message, detail


MAX_LISTED = 8


def _check_freshness(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    """有斷更就記為失敗：這樣斷更自動走失敗通知的流程（首次提醒、持續提醒、恢復）。"""
    from . import freshness
    result = freshness.check(session, cfg)
    if result["checked"] == 0:
        return "skipped", "還沒有任何數據，無從檢查", {}
    problems = [r for r in result["rows"] if r["status"] in ("stale", "never")]
    detail = {"stale": [{k: r[k] for k in ("code", "name_zh", "last", "lag", "max_days", "status")} for r in problems],
              "checked": result["checked"]}
    if not problems:
        return "ok", f"檢查了 {result['checked']} 條序列，都在期限內", detail
    lines = [freshness.summary_line(r) for r in problems[:MAX_LISTED]]
    more = f"\n……另有 {len(problems) - MAX_LISTED} 條" if len(problems) > MAX_LISTED else ""
    return "failed", f"{len(problems)} 條序列超過期限沒有更新：\n" + "\n".join(lines) + more, detail


def _backup(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    """做一份定時備份並校驗，再按保留策略清理舊的定時備份。"""
    from . import backup
    try:
        made = backup.create(settings, cfg, "scheduled")
        removed = backup.prune(settings, cfg)
    except backup.BackupError as exc:
        raise JobError(str(exc)) from None
    message = f"已備份並校驗：{made['name']}（{made['size'] / 1e6:.1f} MB）"
    if removed:
        message += f"；按保留策略清理了 {len(removed)} 份舊備份"
    if backup.same_disk(settings, cfg):
        message += "。注意：備份和數據在同一塊磁盤上，防不了硬盤損壞（見 backup.yaml）"
    return "ok", message, {"backup": made["name"], "size": made["size"], "removed": removed}


def _jin10(session: Session, cfg: Config, sync) -> tuple[str, str, dict] | dict:
    """跑一個金十同步。沒設 token、本地額度到了都記為「跳過」（明天自動恢復）；接口出錯記為失敗。"""
    from . import jin10
    from .connectors.mcp import MCPError
    try:
        client = jin10.Jin10(cfg)
    except jin10.Jin10NotReady as exc:
        return "skipped", str(exc), {}
    try:
        return sync(session, cfg, client)
    except jin10.Jin10LimitReached as exc:
        return "skipped", str(exc), {}
    except MCPError as exc:
        raise JobError(str(exc)) from None
    finally:
        client.close()


def _calendar(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    from . import jin10
    stats = _jin10(session, cfg, jin10.sync_calendar)
    if isinstance(stats, tuple):
        return stats
    message = f"日曆 {stats['items']} 項：新增 {stats['new']}，更新 {stats['updated']}，新公佈 {stats['released']}"
    if stats["observations"]:
        message += f"，寫入超預期 {stats['observations']} 個"
    return "ok", message, stats


def _news(session: Session, cfg: Config, settings: Settings, job: JobDef) -> Result:
    from . import jin10
    stats = _jin10(session, cfg, jin10.sync_flash)
    if isinstance(stats, tuple):
        return stats
    if not stats["kept"]:
        return "skipped", f"看了 {stats['seen']} 條快訊，沒有新的原油相關內容", stats
    return "ok", f"看了 {stats['seen']} 條快訊，收錄原油相關的 {stats['kept']} 條", stats


ACTIONS = {"pull": _pull, "import_folder": _import_folder, "report": _report, "check_freshness": _check_freshness,
           "backup": _backup, "calendar": _calendar, "news": _news}


# ------------------------------ 認領與執行 ------------------------------

def find_job(cfg: Config, code: str) -> JobDef:
    for job in cfg.schedule.jobs:
        if job.code == code:
            return job
    raise JobError(f"沒有名為 {code} 的定時任務")


def claim(code: str, slot: datetime, trigger: str, attempt: int = 1, now: datetime | None = None) -> int | None:
    """認領一次執行。這個時點的這次嘗試已經被別的進程認領過就回 None。now 是開始時刻（調度器傳入它的時鐘）。"""
    try:
        with db.session_scope() as session:
            run = JobRun(job_code=code, slot=slot, trigger=trigger, status="running", attempt=attempt,
                         started_at=now or utcnow())
            if attempt > 1:                          # 上一次嘗試從「待重試」變成「已重試」
                previous = session.scalar(select(JobRun).where(JobRun.job_code == code, JobRun.slot == slot,
                                                               JobRun.attempt == attempt - 1))
                if previous is not None and previous.status == "retry":
                    previous.status = "retried"
            session.add(run)
            session.flush()
            return run.id
    except IntegrityError:
        return None


def execute(cfg: Config, settings: Settings, job: JobDef, run_id: int, retries_left: int = 0,
            note: str | None = None) -> dict:
    """執行已認領的任務並記下結果。任何異常都被接住，不會把調度器打掉。

    retries_left > 0 時，失敗記為 retry（失敗，待重試）：不算最終失敗，不發通知。note 加在結果前面（例如依賴沒等到）。
    """
    try:
        with db.session_scope() as session:
            status, message, detail = ACTIONS[job.action](session, cfg, settings, job)
    except JobError as exc:
        status, message, detail = "failed", credentials.redact(exc), {}
    except Exception as exc:      # 意料之外：不把原文給頁面（可能含網址或路徑），詳情進日誌（日誌有脫敏）
        log.exception("定時任務 %s 發生未預期的錯誤", job.code)
        status, message, detail = "failed", f"未預期的錯誤（{type(exc).__name__}），詳情見服務器日誌", {}
    if status == "failed" and retries_left > 0:
        status = "retry"
        message = f"{message}（{job.retry_minutes} 分鐘後自動重試，還剩 {retries_left} 次）"
    if note:
        message = f"{note}；{message}"
    with db.session_scope() as session:
        run = db.require(session, JobRun, run_id)
        run.status, run.message, run.detail, run.finished_at = status, message, detail, utcnow()
        return {"id": run.id, "job": run.job_code, "status": status, "message": message, "attempt": run.attempt}


def latest_attempt(session: Session, code: str, slot: datetime) -> JobRun | None:
    """某個時點最近的一次嘗試。"""
    return session.scalar(select(JobRun).where(JobRun.job_code == code, JobRun.slot == slot)
                          .order_by(JobRun.attempt.desc()).limit(1))


def abandon_retries(code: str, current_slot: datetime | None) -> list[int]:
    """更早時點還掛著「待重試」的（程序關掉了、沒來得及重試，已經到了下一次）：定為失敗，回傳它們的編號。
    不能讓它們一直掛著：那樣既不算失敗，也不會發通知。

    逐條用「狀態仍是 retry 才改」的條件更新：好幾個後台進程同時跑調度時，只有真正改到的那個回傳它，
    通知因此只發一次。"""
    with db.session_scope() as session:
        stmt = select(JobRun.id, JobRun.message).where(JobRun.job_code == code, JobRun.status == "retry")
        if current_slot is not None:
            stmt = stmt.where(JobRun.slot < current_slot)
        candidates = list(session.execute(stmt))
    changed = []
    for run_id, message in candidates:
        with db.session_scope() as session:
            done = session.execute(update(JobRun).where(JobRun.id == run_id, JobRun.status == "retry").values(
                status="failed", message=f"{message}；沒能重試：已經到了下一次執行時間（程序可能關閉過）"))
            if db.rowcount(done) == 1:
                changed.append(run_id)
    return changed


def retry_due(session: Session, job: JobDef, slot: datetime, now: datetime) -> int | None:
    """這個時點該不該重試：上一次是 retry（失敗，待重試），且離它開始已經過了 retry_minutes。回傳下一次的嘗試序號。"""
    last = latest_attempt(session, job.code, slot)
    if last is None or last.status != "retry" or last.attempt > job.retries:
        return None
    if now - last.started_at < timedelta(minutes=job.retry_minutes):
        return None
    return last.attempt + 1


def run_now(cfg: Config, settings: Settings, code: str) -> dict:
    """手動立即執行一個任務（不管它是否啟用、是否到點）。"""
    job = find_job(cfg, code)
    run_id = claim(code, utcnow(), "manual")
    if run_id is None:
        raise JobError("同一時刻已有一次執行，請稍後再試")
    return execute(cfg, settings, job, run_id)


def recent_runs(session: Session, limit: int = 100, code: str | None = None) -> list[JobRun]:
    stmt = select(JobRun).order_by(JobRun.id.desc()).limit(limit)
    if code:
        stmt = stmt.where(JobRun.job_code == code)
    return list(session.scalars(stmt))


SKIPPED_KEEP_DAYS = 30


def prune_history(now: datetime | None = None) -> int:
    """刪掉 30 天前的「跳過」記錄：收件夾任務每 10 分鐘一條，一年五萬條，沒有保留價值。
    完成、失敗的記錄全部保留（它們是執行歷史和通知的依據）。"""
    from .models import UserSession
    cutoff = (now or utcnow()) - timedelta(days=SKIPPED_KEEP_DAYS)
    with db.session_scope() as session:
        result = session.execute(delete(JobRun).where(JobRun.status == "skipped", JobRun.started_at < cutoff))
        # 過期或登出超過 30 天的登錄記錄也沒用了（只存令牌的雜湊）
        session.execute(delete(UserSession).where(UserSession.expires_at < cutoff))
        from .jin10 import prune_flash
        prune_flash(session, now)                                    # 快訊保留 90 天
        return db.rowcount(result)


def recover_stale(older_than: timedelta = timedelta(hours=1)) -> int:
    """把卡在「執行中」太久的記錄改成失敗（通常是執行到一半進程被關掉了）。

    用條件更新（狀態仍是 running 才改）：剛好在這時做完的任務不會被改回失敗。
    注意：好幾個後台進程時，一個進程啟動會把別的進程裡已經跑了超過 older_than 的任務也標成中斷；
    那個任務做完時會照常寫回真正的結果。"""
    now = utcnow()
    with db.session_scope() as session:
        done = session.execute(update(JobRun).where(JobRun.status == "running", JobRun.started_at < now - older_than)
                               .values(status="failed", finished_at=now, message="執行中斷：程序在任務完成前被關閉了"))
        return db.rowcount(done)