"""報告：生成快照、查看、下載、加大模型解讀。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import reports as service
from ...llm import LLMError, LLMNotConfigured, get_provider, status
from ...llm.tasks import write_narrative
from ...models import Report
from ..deps import AppState, get_actor, get_db, get_state

router = APIRouter(tags=["reports"])


class NewReport(BaseModel):
    kind: str = "daily"
    projection_id: int | None = None       # 不給 = 用最新建立的推演


def _require(session: Session, report_id: int) -> Report:
    row = session.get(Report, report_id)
    if row is None:
        raise HTTPException(404, f"沒有編號為 {report_id} 的報告")
    return row


def _head(r: Report) -> dict:
    return {"id": r.id, "kind": r.kind, "kind_name": service.KINDS.get(r.kind, r.kind), "title": r.title,
            "data_date": r.data_date, "created_at": r.created_at, "has_narrative": r.narrative is not None}


@router.get("/llm")
def llm_status(state: AppState = Depends(get_state)):
    return status(state.cfg.llm)


@router.get("/reports")
def list_reports(session: Session = Depends(get_db)):
    return [_head(r) for r in session.scalars(select(Report).order_by(Report.id.desc()).limit(200))]


@router.post("/reports")
def create_report(body: NewReport, session: Session = Depends(get_db), state: AppState = Depends(get_state),
                  actor: str = Depends(get_actor)):
    try:
        return _head(service.generate(session, state.cfg, body.kind, body.projection_id, actor))
    except service.ReportError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/reports/{report_id}")
def get_report(report_id: int, session: Session = Depends(get_db)):
    r = _require(session, report_id)
    return {**_head(r), "markdown": r.markdown, "narrative": r.narrative, "narrative_meta": r.narrative_meta,
            "facts": r.facts}


@router.get("/reports/{report_id}/markdown", response_class=PlainTextResponse)
def download_markdown(report_id: int, session: Session = Depends(get_db)):
    """正文加解讀的 Markdown 全文，可直接存檔或貼到別處。"""
    r = _require(session, report_id)
    text = r.markdown
    if r.narrative:
        meta = r.narrative_meta or {}
        text += (f"\n## 解讀\n\n由大模型（{meta.get('provider')} / {meta.get('model')}）"
                 "根據以上正文撰寫，不是系統計算的結果。")
        if not meta.get("guard_passed", False):
            text += f"其中這些數字在正文裡找不到，請勿採信：{'、'.join(meta.get('unknown_numbers', []))}。"
        text += f"\n\n{r.narrative}\n"
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="report-{r.id}.md"'})


@router.post("/reports/{report_id}/narrative")
def add_narrative(report_id: int, session: Session = Depends(get_db), state: AppState = Depends(get_state)):
    """讓大模型根據報告正文寫解讀。寫出的每個數字都會和正文核對，對不上的會被列出。"""
    r = _require(session, report_id)
    try:
        provider = get_provider("narrative", state.cfg.llm)
        result = write_narrative(provider, r.markdown, service.KINDS.get(r.kind, r.kind))
    except LLMNotConfigured as exc:
        raise HTTPException(409, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    r.narrative = result.pop("text")
    r.narrative_meta = result
    return {"narrative": r.narrative, "narrative_meta": r.narrative_meta}
