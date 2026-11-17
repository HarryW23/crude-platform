"""文件導入：預覽 → 確認入庫。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...imports import ImportFlowError, commit_batch, preview_file
from ...ingest import IngestError
from ...models import ImportBatch
from ..deps import AppState, get_actor, get_db, get_state

admin_router = APIRouter(tags=["imports"])         # 只在後台


class CommitRequest(BaseModel):
    mapping: dict[str, str | None] = {}     # 只需要給和自動識別結果不同的列；值為 null = 不導入
    learn: bool = True


@admin_router.post("/imports/preview")
async def preview(file: UploadFile = File(...), source_code: str = Form("reuters_xls"),
                  session: Session = Depends(get_db), state: AppState = Depends(get_state),
                  actor: str = Depends(get_actor)):
    try:
        return preview_file(session, state.settings, await file.read(), file.filename or "upload", source_code, actor)
    except ImportFlowError as exc:
        raise HTTPException(400, str(exc)) from exc


@admin_router.post("/imports/{batch_id}/commit")
def commit(batch_id: int, body: CommitRequest, session: Session = Depends(get_db),
           actor: str = Depends(get_actor)):
    try:
        return commit_batch(session, batch_id, body.mapping, body.learn, actor)
    except (ImportFlowError, IngestError) as exc:
        raise HTTPException(400, str(exc)) from exc


@admin_router.get("/imports")
def list_imports(session: Session = Depends(get_db)):
    rows = session.scalars(select(ImportBatch).where(ImportBatch.status == "committed")
                           .order_by(ImportBatch.id.desc()).limit(100))
    return [{"id": b.id, "filename": b.filename, "source_code": b.source_code, "created_at": b.created_at,
             "created_by": b.created_by, "stats": b.stats} for b in rows]
