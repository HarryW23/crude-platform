"""對話式查詢。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...llm import LLMError, LLMNotConfigured, get_provider
from ...llm import chat as chat_service
from ...models import AuditLog
from ..deps import AppState, get_actor, get_db, get_state

router = APIRouter(tags=["chat"])


class Turn(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=8000)       # 限長：歷史由頁面帶上，不能讓它變成一次發出大量 token 的口子


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default=[], max_length=40)    # 之前的問答（只要文字），由頁面保存並帶上


@router.get("/chat/tools")
def tools():
    """問答能用的工具清單。全部唯讀。"""
    return [{"name": name, "usage": doc} for name, (_, doc) in chat_service.TOOLS.items()]


@router.post("/chat")
def chat(body: ChatRequest, session: Session = Depends(get_db), state: AppState = Depends(get_state),
         actor: str = Depends(get_actor)):
    """回答一個問題。回傳回答、完整的取數過程（steps），以及數字核對的結果。問答不會修改任何數據。"""
    try:
        result = chat_service.answer(get_provider("chat", state.cfg.llm), session, state.cfg, body.question,
                                     [t.model_dump() for t in body.history])
    except LLMNotConfigured as exc:
        raise HTTPException(409, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    session.add(AuditLog(actor=actor, action="chat", entity="llm",
                         entity_id=f"{result['provider']}/{result['model']}"[:128],
                         detail={"question": body.question[:300], "tools": [s["tool"] for s in result["steps"]],
                                 "guard_passed": result["guard_passed"]}))
    return result
