"""文件導入：預覽 → 確認入庫。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...imports import ImportFlowError, commit_batch, preview_file
from ...ingest import IngestError
from ...models import ImportBatch
from ..deps import DB, AppState, get_actor, get_state

MAX_UPLOAD = 50_000_000          # 上傳文件上限：整個文件會讀進內存
admin_router = APIRouter(tags=["imports"])         # 只在後台


class CommitRequest(BaseModel):
    mapping: dict[str, str | None] = {}     # 只需要給和自動識別結果不同的列；值為 null = 不導入
    learn: bool = True


@admin_router.post("/imports/preview")
async def preview(file: UploadFile = File(...), source_code: str = Form("reuters_xls"),
                  vintage: str | None = Form(None), session: Session = DB,
                  state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, f"文件超過 {MAX_UPLOAD // 1_000_000} MB，請拆開再導入")
    try:
        issued = date.fromisoformat(vintage) if vintage else None
    except ValueError:
        raise HTTPException(400, "報告期要寫成 2026-11-12 這樣的日期") from None
    try:
        return preview_file(session, state.settings, content, file.filename or "upload", source_code, actor, issued)
    except ImportFlowError as exc:
        raise HTTPException(400, str(exc)) from exc


@admin_router.post("/imports/{batch_id}/commit")
def commit(batch_id: int, body: CommitRequest, session: Session = DB,
           actor: str = Depends(get_actor)):
    try:
        return commit_batch(session, batch_id, body.mapping, body.learn, actor)
    except (ImportFlowError, IngestError) as exc:
        raise HTTPException(400, str(exc)) from exc


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@admin_router.get("/imports/templates/opec_production.xlsx")
def opec_template(state: AppState = Depends(get_state)):
    """OPEC 月報產量的導入模板（從配置生成）。"""
    from ...templates import opec_production
    return Response(opec_production(state.cfg), media_type=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="opec_production.xlsx"'})


@admin_router.get("/imports/templates/kpler.xlsx")
def kpler_template(state: AppState = Depends(get_state)):
    """Kpler 取數模板（從配置生成）。"""
    from ...templates import kpler
    return Response(kpler(state.cfg), media_type=XLSX,
                    headers={"Content-Disposition": 'attachment; filename="kpler.xlsx"'})


@admin_router.get("/imports")
def list_imports(session: Session = DB):
    rows = session.scalars(select(ImportBatch).where(ImportBatch.status == "committed")
                           .order_by(ImportBatch.id.desc()).limit(100))
    return [{"id": b.id, "filename": b.filename, "source_code": b.source_code, "created_at": b.created_at,
             "created_by": b.created_by, "stats": b.stats} for b in rows]
