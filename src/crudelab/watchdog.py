"""看門狗（#33）：從另一台機器定時檢查平台的健康檢查接口，掛了就發群通知，恢復了再發一條。

平台自己的通知只能在它活着時發；程序崩潰、機器關機、斷網時它什麼都發不出來。所以要由外面來看：
  crudelab watchdog http://平台地址:8001 --require-scheduler
放在另一台機器（或至少另一個進程管理器）上，用 cron / launchd / 計劃任務每隔幾分鐘跑一次，或加 --loop 常駐。

判斷：
  - 連不上、超時、回應不是 200、或 status 不是 ok，都算一次失敗
  - 連續失敗 failures 次（預設 2，避免重啟、網絡抖動誤報）才告警；之後每 repeat_hours 小時提醒一次
  - 恢復時發一條「已恢復」，附停機多久
  - 狀態（連續失敗幾次、是否已告警）記在本機的小文件裡，每次運行之間靠它記住；不碰被監控的數據庫

通知渠道用這台機器上的 config/notifications.yaml 和密鑰文件（crudelab secret set …），和平台本身的設定方法相同。
告警沒能送達任何渠道時不記為「已告警」，下次運行會再試，並以退出碼 2 讓外層知道。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import credentials, notifications
from .config import Config

TIMEOUT = 10.0


@dataclass
class Probe:
    ok: bool
    reason: str


def probe(url: str, require_scheduler: bool, timeout: float = TIMEOUT) -> Probe:
    target = url.rstrip("/") + "/api/health" + ("?require=scheduler" if require_scheduler else "")
    try:
        response = httpx.get(target, timeout=timeout)
    except httpx.TimeoutException:
        return Probe(False, f"{timeout:.0f} 秒內沒有回應")
    except httpx.HTTPError as exc:
        return Probe(False, f"連不上（{type(exc).__name__}）：程序沒在運行、機器關機或網絡不通")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code == 200 and data.get("status") == "ok":
        return Probe(True, "正常")
    checks = data.get("checks") or {}
    bad = [f"{k}={v}" for k, v in checks.items() if v != "ok" and k in (data.get("required") or checks)]
    names = {"database=error": "數據庫連不上", "config=error": "配置文件有錯",
             "scheduler=stale": "調度器超過兩分鐘沒有心跳", "scheduler=never_ran": "調度器從來沒有運行過"}
    detail = "、".join(names.get(b, b) for b in bad)
    return Probe(False, f"健康檢查回報 {response.status_code}" + (f"：{detail}" if detail else ""))


def default_state_path(url: str) -> Path:
    digest = hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:12]
    return Path.home() / ".crudelab" / f"watchdog-{digest}.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".watchdog-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(temp, path)


def _local(when: datetime, tz: ZoneInfo) -> str:
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60} 小時 {minutes % 60} 分鐘" if minutes >= 60 else f"{max(minutes, 1)} 分鐘"


def run_once(cfg: Config, url: str, *, require_scheduler: bool = False, failures: int = 2,
             channels: list[str] | None = None, state_path: Path | None = None, notify: bool = True,
             now: datetime | None = None) -> dict:
    """檢查一次並按需通知。回傳 {"ok", "reason", "event", "results", "delivered"}。"""
    now = now or datetime.now(UTC)
    tz = ZoneInfo(cfg.schedule.timezone)
    path = state_path or default_state_path(url)
    state = _load(path)
    result = probe(url, require_scheduler)
    event, title, body = None, "", ""
    if result.ok:
        if state.get("alerted"):
            since = datetime.fromisoformat(state["since"])
            event, title = "recovery", "平台已恢復"
            body = (f"{url} 恢復正常（{_local(now, tz)}）。\n"
                    f"停機約 {_duration(now - since)}，從 {_local(since, tz)} 起。")
        state = {}
    else:
        count = int(state.get("failures", 0)) + 1
        first_failure = str(state.get("since") or now.isoformat())
        state.update(failures=count, since=first_failure, reason=result.reason)
        last = state.get("alerted_at")
        if count >= failures and not state.get("alerted"):
            event, title = "failure", "平台無法訪問"
        elif state.get("alerted") and last and now - datetime.fromisoformat(last) >= timedelta(
                hours=cfg.notifications.repeat_hours):
            event, title = "reminder", "平台仍無法訪問"
        if event:
            start = datetime.fromisoformat(first_failure)
            body = (f"{url}：{result.reason}。\n從 {_local(start, tz)} 起已連續 {count} 次檢查失敗"
                    f"（持續約 {_duration(now - start)}）。\n請檢查程序是否在運行、機器是否開機、網絡是否通。")
    results: dict[str, str] = {}
    delivered = True
    if event and notify:
        results = notifications.deliver(cfg, title, body, event=f"watchdog_{event}", channels=channels)
        delivered = any(v == "ok" for v in results.values())
    if event in ("failure", "reminder") and delivered:
        state.update(alerted=True, alerted_at=now.isoformat())
    if event == "recovery" and not delivered:           # 「已恢復」沒送達：保留告警狀態，下次再發
        state = _load(path)
    _save(path, state)
    return {"ok": result.ok, "reason": credentials.redact(result.reason), "event": event, "title": title,
            "body": body, "results": results, "delivered": delivered}
