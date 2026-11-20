"""大模型的配置、用量和自動暫停。只在後台。不返回發出去的內容和回覆（本來也不存）。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...llm import LLMError, LLMNotConfigured, get_provider, limits, status
from ...llm.registry import PROVIDERS, SUPPORTS_THINKING, model_lister, save_choice
from ...models import AuditLog, LLMCall, iso_utc, utcnow
from ..deps import DB, AppState, get_actor, get_state

admin_router = APIRouter(tags=["llm"])


class ChoiceBody(BaseModel):
    provider: str
    model: str = Field(default="", max_length=128)
    thinking: bool = False
    base_url: str | None = Field(default=None, max_length=300)


class ModelsBody(BaseModel):
    provider: str | None = None


@admin_router.get("/llm/usage")
def llm_usage(session: Session = DB, state: AppState = Depends(get_state)):
    st = status(state.cfg.llm)
    pause = limits.active_pause(session, st["provider"]) if st["provider"] != "none" else None
    since = utcnow() - timedelta(hours=24)
    by_purpose = session.execute(
        select(LLMCall.purpose, func.count(), func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
               func.coalesce(func.sum(LLMCall.completion_tokens), 0))
        .where(LLMCall.started_at >= since).group_by(LLMCall.purpose)).all()
    return {
        "status": st, "settings": state.cfg.llm.model_dump(), "timezone": state.cfg.schedule.timezone,
        "providers": [{"code": k, "name": v, "thinking": k in SUPPORTS_THINKING} for k, v in PROVIDERS.items()],
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
def llm_reset(session: Session = DB, actor: str = Depends(get_actor)):
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
    return {"ok": True, "provider": provider.name, "model": provider.model, "thinking": provider.thinking,
            "reply": reply.strip()[:40],
            "tokens": list(provider.inner.last_usage)}


@admin_router.put("/llm/choice")
def llm_choice(body: ChoiceBody, session: Session = DB, actor: str = Depends(get_actor)):
    """選擇用哪一家、哪個型號、開不開思考模式。立即生效（下一次調用就用新的設定）。"""
    try:
        return save_choice(session, body.provider, body.model, body.thinking, actor, body.base_url)
    except LLMNotConfigured as exc:
        raise HTTPException(400, str(exc)) from None


@admin_router.post("/llm/models")
def llm_models(body: ModelsBody, state: AppState = Depends(get_state)):
    """向供應商查這個密鑰能用的模型名（發一次不耗 token 的請求，同樣受調用限制約束）。"""
    try:
        return {"models": model_lister(state.cfg.llm, body.provider).list_models()}
    except LLMNotConfigured as exc:
        raise HTTPException(409, str(exc)) from None
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from None
