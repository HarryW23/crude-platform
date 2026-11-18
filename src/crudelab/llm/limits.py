"""大模型調用的限制與記錄：保護帳號，也保護錢包。

每一次調用（問答的每一步、抽取事件、寫報告解讀、連通測試）都經過這裡：

  調用前  1. 這家是否被自動暫停了（見下）
          2. 最近 60 秒的請求數、最近 24 小時的請求數和 token 數、正在進行的請求數是否超過 config/llm.yaml 的上限
          3. 和上一次請求至少隔 min_interval_seconds 秒（不夠就稍等，最多等這麼久）
          任何一項不過就不發請求，直接報錯（LLMLimitError），說明是本地限制擋下的。
  調用後  記一條 llm_call：時間、用途、模型、狀態、HTTP 狀態碼、token 數。不記發出去的內容和回覆。

自動暫停（記在 llm_pause，到期自動恢復；確認處理好了也可以手動解除）：
  401 / 403   密鑰無效或沒有權限            暫停 24 小時：避免拿著錯的密鑰反覆請求
  402         餘額不足                      暫停 6 小時
  429         觸發對方的頻率限制            暫停 10 分鐘，連續再觸發每次加倍，最長 4 小時
  400         1 小時內被拒絕 3 次（常見原因是內容審核）   暫停 1 小時：避免反覆觸發對方的審核
  5xx / 連不上  10 分鐘內連續 3 次            暫停 10 分鐘

不做自動重試：失敗就停，交給人決定。重試是把小問題放大成封號的常見原因。
這些計數存在數據庫裡，所以前台、後台、調度器幾個進程共用同一套限制。
"""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import func, select

from .. import credentials, db
from ..models import LLMCall, LLMPause, utcnow
from ..schema import LLMSettings
from .base import LLMError, LLMProvider

RUNNING_STALE = timedelta(minutes=15)      # 「進行中」的記錄超過這麼久就不再算作進行中（進程被殺掉的情況）
PURPOSES = {"chat": "問答", "extract": "抽取事件", "narrative": "報告解讀", "test": "連通測試", "models": "查模型列表",
            "other": "其他"}


class LLMLimitError(LLMError):
    """被本地的限制擋下，請求沒有發出去。"""


def _pause_rule(status: int | None, strikes: int, session, provider: str) -> tuple[timedelta, str, int] | None:
    """按這次失敗決定要不要暫停：回傳（多久, 原因, 新的連續次數）或 None。"""
    if status in (401, 403):
        return timedelta(hours=24), "密鑰無效或沒有權限", strikes
    if status == 402:
        return timedelta(hours=6), "帳戶餘額不足", strikes
    if status == 429:
        minutes = min(10 * 2 ** strikes, 240)
        return timedelta(minutes=minutes), f"觸發了對方的頻率限制（連續第 {strikes + 1} 次）", strikes + 1
    since = utcnow() - timedelta(hours=1)
    if status == 400:
        n = session.scalar(select(func.count()).select_from(LLMCall).where(
            LLMCall.provider == provider, LLMCall.http_status == 400, LLMCall.started_at >= since))
        if n >= 3:
            return timedelta(hours=1), "1 小時內被拒絕了 3 次（常見原因是內容審核），先停一下", strikes
        return None
    if status is None or status >= 500:
        recent = list(session.scalars(select(LLMCall).where(
            LLMCall.provider == provider, LLMCall.started_at >= utcnow() - timedelta(minutes=10),
            LLMCall.status != "running").order_by(LLMCall.id.desc()).limit(3)))
        if len(recent) == 3 and all(c.status == "failed" and (c.http_status is None or c.http_status >= 500)
                                    for c in recent):
            return timedelta(minutes=10), "連續 3 次連不上或對方服務出錯", strikes
    return None


def active_pause(session, provider: str) -> LLMPause | None:
    pause = session.get(LLMPause, provider)
    return pause if pause is not None and pause.until > utcnow() else None


def usage(session, provider: str | None = None) -> dict:
    """最近 60 秒 / 24 小時的用量。"""
    now = utcnow()

    def where(stmt, since):
        stmt = stmt.where(LLMCall.started_at >= since)
        return stmt.where(LLMCall.provider == provider) if provider else stmt
    day = now - timedelta(hours=24)
    tokens = session.execute(where(select(func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
                                          func.coalesce(func.sum(LLMCall.completion_tokens), 0)), day)).one()
    return {
        "minute": session.scalar(where(select(func.count()).select_from(LLMCall), now - timedelta(seconds=60))),
        "day": session.scalar(where(select(func.count()).select_from(LLMCall), day)),
        "failed_day": session.scalar(where(select(func.count()).select_from(LLMCall), day)
                                     .where(LLMCall.status == "failed")),
        "prompt_tokens_day": int(tokens[0]), "completion_tokens_day": int(tokens[1]),
        "running": session.scalar(where(select(func.count()).select_from(LLMCall), now - RUNNING_STALE)
                                  .where(LLMCall.status == "running")),
    }


def _check(session, limits: LLMSettings, provider: str) -> float:
    """不過就拋 LLMLimitError；過了回傳還要等幾秒才能發。"""
    pause = active_pause(session, provider)
    if pause is not None:
        raise LLMLimitError(f"{provider} 已自動暫停到 {pause.until:%Y-%m-%d %H:%M}（UTC）：{pause.reason}。"
                            "處理好之後可以在後台「大模型」頁或用 crudelab llm reset 解除")
    u = usage(session, provider)
    if u["running"] >= limits.max_concurrent:
        raise LLMLimitError(f"已經有 {u['running']} 個大模型請求在進行中（上限 {limits.max_concurrent}），請稍後再試")
    if u["minute"] >= limits.max_requests_per_minute:
        raise LLMLimitError(f"最近一分鐘已經請求了 {u['minute']} 次"
                            f"（上限 {limits.max_requests_per_minute}），請稍後再試")
    if u["day"] >= limits.max_requests_per_day:
        raise LLMLimitError(f"最近 24 小時已經請求了 {u['day']} 次，達到上限 {limits.max_requests_per_day}"
                            "（在 config/llm.yaml 調整）")
    if u["prompt_tokens_day"] + u["completion_tokens_day"] >= limits.max_tokens_per_day:
        raise LLMLimitError(f"最近 24 小時的 token 用量達到上限 {limits.max_tokens_per_day}（在 config/llm.yaml 調整）")
    last = session.scalar(select(func.max(LLMCall.started_at)).where(LLMCall.provider == provider))
    if last is None:
        return 0.0
    gap = (utcnow() - last).total_seconds()
    return max(0.0, limits.min_interval_seconds - gap)


class LimitedProvider(LLMProvider):
    """套在任何一家外面：調用前檢查限制，調用後記錄、按失敗類型自動暫停。"""

    def __init__(self, inner: LLMProvider, limits: LLMSettings, purpose: str = "other"):
        self.inner, self.limits = inner, limits
        self.purpose = purpose if purpose in PURPOSES else "other"
        self.name, self.model = inner.name, inner.model

    def complete(self, system: str, user: str, *, json: bool = False, max_tokens: int = 4000) -> str:
        return self._run(lambda: self.inner.complete(system, user, json=json, max_tokens=max_tokens))

    def list_models(self) -> list[str]:
        return self._run(self.inner.list_models)

    @property
    def thinking(self) -> bool:
        return bool(getattr(self.inner, "thinking", False))

    def _run(self, call_inner):
        with db.session_scope() as session:
            wait = _check(session, self.limits, self.name)
        if wait:
            time.sleep(wait)
        with db.session_scope() as session:
            _check(session, self.limits, self.name)          # 等待期間別的進程可能已經用掉了額度
            model = (self.model or "—") + ("（思考）" if self.thinking else "")
            call = LLMCall(provider=self.name, model=model[:128], purpose=self.purpose, status="running")
            session.add(call)
            session.flush()
            call_id = call.id
        self.inner.last_usage = (None, None)
        try:
            result = call_inner()
        except LLMError as exc:
            self._finish(call_id, "failed", exc.status, credentials.redact(exc))
            raise
        except Exception as exc:
            self._finish(call_id, "failed", None, f"未預期的錯誤（{type(exc).__name__}）")
            raise
        self._finish(call_id, "ok", 200, None)
        return result

    def _finish(self, call_id: int, status: str, http_status: int | None, message: str | None) -> None:
        with db.session_scope() as session:
            call = session.get(LLMCall, call_id)
            call.status, call.http_status, call.message, call.finished_at = status, http_status, message, utcnow()
            call.prompt_tokens, call.completion_tokens = self.inner.last_usage
            pause = session.get(LLMPause, self.name)
            if status == "ok":
                if pause is not None and pause.until <= utcnow():
                    session.delete(pause)                    # 成功一次：清掉過期的暫停和連續次數
                return
            session.flush()
            rule = _pause_rule(http_status, pause.strikes if pause else 0, session, self.name)
            if rule is None:
                return
            length, reason, strikes = rule
            if pause is None:
                pause = LLMPause(provider=self.name, until=utcnow(), reason="", strikes=0)
                session.add(pause)
            pause.until, pause.reason, pause.strikes = utcnow() + length, reason, strikes


def reset(provider: str | None = None) -> int:
    """手動解除暫停。回傳解除了幾家。"""
    with db.session_scope() as session:
        rows = list(session.scalars(select(LLMPause))) if provider is None else \
            [p for p in [session.get(LLMPause, provider)] if p is not None]
        for row in rows:
            session.delete(row)
        return len(rows)


def recent_calls(session, limit: int = 50) -> list[LLMCall]:
    return list(session.scalars(select(LLMCall).order_by(LLMCall.started_at.desc(), LLMCall.id.desc()).limit(limit)))
