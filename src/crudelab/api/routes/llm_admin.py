"""大模型的配置、用量和自動暫停。只在後台。不返回發出去的內容和回覆（本來也不存）。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...llm import LLMError, LLMNotConfigured, get_provider, limits, status
from ...models import AuditLog, LLMCall, iso_utc, utcnow
from ..deps import AppState, get_actor, get_db, get_state

admin_router = APIRouter(tags=["llm"])


@admin_router.get("/llm/usage")
def llm_usage(session: Session = Depends(get_db), state: AppState = Depends(get_state)):
    st = status(state.cfg.llm)
    pause = limits.active_pause(session, st["provider"]) if st["provider"] != "none" else None
    since = utcnow() - timedelta(hours=24)
    by_purpose = session.execute(
        select(LLMCall.purpose, func.count(), func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
               func.coalesce(func.sum(LLMCall.completion_tokens), 0))
        .where(LLMCall.started_at >= since).group_by(LLMCall.purpose)).all()
    return {
        "status": st, "settings": state.cfg.llm.model_dump(), "timezone": state.cfg.schedule.timezone,
        "usage": limits.usage(session),
        "pause": None if pause is None else {"until": iso_utc(pause.until), "reason": pause.reason},
        "by_purpose": [{"purpose": p, "name_zh": limits.PURPOSES.get(p, p), "calls": n, "prompt_tokens": int(a),
                        "completion_tokens": int(b)} for p, n, a, b in by_purpose],
        "recent": [{"at": iso_utc(c.started_at), "provider": c.provider, "model": c.model, "purpose": c.purpose,
                    "purpose_zh": limits.PURPOSES.get(c.purpose, c.purpose), "status": c.status,
                    "http_status": c.http_status, "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens, "message": c.message,
                    "seconds": (c.finished_at - c.started_at).total_seconds() if c.finished_at else None}
                   for c in limits.recent_calls(session, 50)],
    }


@admin_router.post("/llm/reset")
def llm_reset(session: Session = Depends(get_db), actor: str = Depends(get_actor)):
    n = limits.reset()
    session.add(AuditLog(actor=actor, action="llm_reset", entity="llm", entity_id="pause", detail={"cleared": n}))
    return {"cleared": n}


@admin_router.post("/llm/test")
def llm_test(state: AppState = Depends(get_state)):
    """發一次很短的請求，確認密鑰、模型名、網絡都通。同樣受調用限制約束。"""
    try:
        provider = get_provider("test", state.cfg.llm)
        reply = provider.complete("你是連通測試。只回覆兩個字：正常", "連通測試", max_tokens=50)
    except LLMNotConfigured as exc:
        raise HTTPException(409, str(exc)) from None
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from None
    return {"ok": True, "provider": provider.name, "model": provider.model, "reply": reply.strip()[:40],
            "tokens": list(provider.inner.last_usage)}
