"""後台接口。頁面（web/index.html）和將來的前台、大模型工具調用都走這一套接口。

登錄與權限：第一期單人本機使用，操作者固定記為 "local"；
所有寫操作都已經帶 actor 字段，之後接登錄只需要在 get_actor 裡換成真實用戶。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import db
from ..connectors import ConnectorNotReady, get_connector, list_connectors
from ..imports import ImportFlowError, commit_batch, preview_file
from ..indicators import IndicatorEngine
from ..ingest import IngestError, Obs, load_points, write_observations
from ..master import ConfigError, MasterConfig, load_config, sync_to_db
from ..models import (AuditLog, Commodity, DataSource, ImportBatch, Location, Observation, Parameter,
                      ParameterHistory, Series, utcnow)
from ..settings import Settings, get_settings

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class ManualPoint(BaseModel):
    series_code: str
    obs_date: date
    value: float


class ManualEntry(BaseModel):
    points: list[ManualPoint]


class CommitRequest(BaseModel):
    mapping: dict[str, str | None] = {}
    learn: bool = True


class ParameterUpdate(BaseModel):
    value: float
    note: str | None = None


class PullRequest(BaseModel):
    start: date | None = None
    end: date | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db.init_engine(settings)
    state: dict[str, MasterConfig] = {}

    def reload_config() -> dict:
        cfg = load_config(settings.config_dir)
        with db.session_scope() as session:
            stats = sync_to_db(session, cfg)
        state["cfg"] = cfg
        return stats

    reload_config()
    app = FastAPI(title="crudelab 後台", version="0.1.0")

    def get_db():
        with db.session_scope() as session:
            yield session

    def get_actor() -> str:
        return "local"

    def engine_for(session: Session) -> IndicatorEngine:
        return IndicatorEngine(session, state["cfg"].indicators)

    # ------------------------------ 頁面 ------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # ------------------------------ 總覽 ------------------------------
    @app.get("/api/overview")
    def overview(session: Session = Depends(get_db)):
        engine = engine_for(session)
        summaries = [engine.summary(code) for code in engine.defs]
        with_data = session.scalar(select(func.count(func.distinct(Observation.series_code)))) or 0
        return {
            "series_total": session.scalar(select(func.count()).select_from(Series)),
            "series_with_data": with_data,
            "observations": session.scalar(select(func.count()).select_from(Observation)),
            "last_obs_date": session.scalar(select(func.max(Observation.obs_date))),
            "indicators_total": len(summaries),
            "indicators_ok": sum(s["status"] == "ok" for s in summaries),
            "batches": session.scalar(select(func.count()).select_from(ImportBatch).where(
                ImportBatch.status == "committed")),
        }

    @app.get("/api/meta")
    def meta(session: Session = Depends(get_db)):
        return {
            "commodities": {c.code: c.name_zh for c in session.scalars(select(Commodity))},
            "locations": {loc.code: {"name_zh": loc.name_zh, "region": loc.region}
                          for loc in session.scalars(select(Location))},
        }

    # ------------------------------ 序列 ------------------------------
    @app.get("/api/series")
    def series_list(session: Session = Depends(get_db)):
        coverage = {code: (n, first, last) for code, n, first, last in session.execute(
            select(Observation.series_code, func.count(func.distinct(Observation.obs_date)),
                   func.min(Observation.obs_date), func.max(Observation.obs_date))
            .group_by(Observation.series_code))}
        points = load_points(session, list(coverage))
        latest = points.sort_values("obs_date").groupby("series_code").last() if not points.empty else None
        out = []
        for s in session.scalars(select(Series).order_by(Series.module, Series.code)):
            n, first, last = coverage.get(s.code, (0, None, None))
            out.append({"code": s.code, "name_zh": s.name_zh, "commodity": s.commodity_code,
                        "location": s.location_code, "kind": s.kind, "unit": s.unit_code,
                        "frequency": s.frequency, "module": s.module, "benchmark": s.benchmark,
                        "n": n, "first_date": first, "last_date": last,
                        "last_value": float(latest.loc[s.code, "value"]) if n else None,
                        "last_source": latest.loc[s.code, "source_code"] if n else None})
        return out

    @app.get("/api/series/{code}/data")
    def series_data(code: str, start: date | None = None, end: date | None = None,
                    session: Session = Depends(get_db)):
        if session.get(Series, code) is None:
            raise HTTPException(404, f"沒有名為 {code} 的序列")
        df = load_points(session, [code], start, end)
        return [{"date": r.obs_date, "value": r.value, "source": r.source_code, "ref": r.source_ref}
                for r in df.itertuples()]

    @app.post("/api/observations")
    def manual_entry(body: ManualEntry, session: Session = Depends(get_db), actor: str = Depends(get_actor)):
        try:
            stats = write_observations(
                session, [Obs(p.series_code, p.obs_date, p.value, "後台手動錄入") for p in body.points],
                "manual", actor=actor)
        except IngestError as exc:
            raise HTTPException(400, str(exc)) from exc
        session.add(AuditLog(actor=actor, action="manual_entry", entity="observation",
                             entity_id=",".join(sorted({p.series_code for p in body.points}))[:128], detail=stats))
        return stats

    # ------------------------------ 導入 ------------------------------
    @app.post("/api/imports/preview")
    async def import_preview(file: UploadFile = File(...), source_code: str = Form("reuters_xls"),
                             session: Session = Depends(get_db), actor: str = Depends(get_actor)):
        try:
            return preview_file(session, settings, await file.read(), file.filename or "upload", source_code, actor)
        except ImportFlowError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/imports/{batch_id}/commit")
    def import_commit(batch_id: int, body: CommitRequest, session: Session = Depends(get_db),
                      actor: str = Depends(get_actor)):
        try:
            return commit_batch(session, batch_id, body.mapping, body.learn, actor)
        except (ImportFlowError, IngestError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/imports")
    def import_list(session: Session = Depends(get_db)):
        rows = session.scalars(select(ImportBatch).where(ImportBatch.status == "committed")
                               .order_by(ImportBatch.id.desc()).limit(100))
        return [{"id": b.id, "filename": b.filename, "source_code": b.source_code, "created_at": b.created_at,
                 "created_by": b.created_by, "stats": b.stats} for b in rows]

    # ------------------------------ 指標 ------------------------------
    @app.get("/api/indicators")
    def indicator_list(session: Session = Depends(get_db)):
        engine = engine_for(session)
        return [engine.summary(code) for code in engine.defs]

    @app.get("/api/indicators/{code}/data")
    def indicator_data(code: str, session: Session = Depends(get_db)):
        engine = engine_for(session)
        if code not in engine.defs:
            raise HTTPException(404, f"沒有名為 {code} 的指標")
        return [{"date": ts.date(), "value": float(v)} for ts, v in engine.compute(code).items()]

    @app.get("/api/indicators/{code}/trace")
    def indicator_trace(code: str, on: date | None = None, session: Session = Depends(get_db)):
        engine = engine_for(session)
        if code not in engine.defs:
            raise HTTPException(404, f"沒有名為 {code} 的指標")
        values = engine.compute(code)
        if on is None:
            if values.empty:
                raise HTTPException(400, "這個指標目前沒有任何結果可以追溯")
            on = values.index[-1].date()
        return {"date": on, "tree": engine.trace(code, on)}

    # ------------------------------ 參數 ------------------------------
    @app.get("/api/parameters")
    def parameter_list(session: Session = Depends(get_db)):
        used: dict[str, list[str]] = {}
        engine = engine_for(session)
        from ..indicators import formula_names
        for code, d in engine.defs.items():
            for name in formula_names(d["formula"]):
                if name in engine.params:
                    used.setdefault(name, []).append(code)
        return [{"key": p.key, "name_zh": p.name_zh, "value": p.value, "unit": p.unit, "group": p.group,
                 "description": p.description, "updated_at": p.updated_at, "used_by": used.get(p.key, [])}
                for p in session.scalars(select(Parameter).order_by(Parameter.group, Parameter.key))]

    @app.put("/api/parameters/{key}")
    def parameter_update(key: str, body: ParameterUpdate, session: Session = Depends(get_db),
                         actor: str = Depends(get_actor)):
        param = session.get(Parameter, key)
        if param is None:
            raise HTTPException(404, f"沒有名為 {key} 的參數")
        if body.value != body.value or abs(body.value) == float("inf"):
            raise HTTPException(400, "參數值必須是有效數字")
        session.add(ParameterHistory(key=key, old_value=param.value, new_value=body.value,
                                     note=body.note, changed_by=actor))
        param.value, param.updated_at = body.value, utcnow()
        return {"key": key, "value": param.value}

    @app.get("/api/parameters/{key}/history")
    def parameter_history(key: str, session: Session = Depends(get_db)):
        rows = session.scalars(select(ParameterHistory).where(ParameterHistory.key == key)
                               .order_by(ParameterHistory.id.desc()))
        return [{"old_value": h.old_value, "new_value": h.new_value, "note": h.note,
                 "changed_by": h.changed_by, "changed_at": h.changed_at} for h in rows]

    # ------------------------------ 數據源 ------------------------------
    @app.get("/api/sources")
    def source_list(session: Session = Depends(get_db)):
        connectors = list_connectors()
        counts = dict(session.execute(select(Observation.source_code, func.count())
                                      .group_by(Observation.source_code)).all())
        all_series = list(session.scalars(select(Series)))
        out = []
        for s in session.scalars(select(DataSource).order_by(DataSource.priority)):
            conn = connectors.get(s.code)
            status = conn.status() if conn else None
            out.append({"code": s.code, "name": s.name, "kind": s.kind, "status": s.status, "priority": s.priority,
                        "notes": s.notes, "observations": counts.get(s.code, 0),
                        "pullable": conn is not None, "ready": status.ready if status else None,
                        "message": status.message if status else None,
                        "series_supported": len(conn.supported(all_series)) if conn else None})
        return out

    @app.post("/api/sources/{code}/pull")
    def source_pull(code: str, body: PullRequest, session: Session = Depends(get_db),
                    actor: str = Depends(get_actor)):
        try:
            conn = get_connector(code)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            observations = conn.fetch(list(session.scalars(select(Series))), body.start, body.end)
        except ConnectorNotReady as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # 對方接口的錯誤原樣報給使用者
            raise HTTPException(502, f"從 {code} 取數失敗：{exc}") from exc
        stats = write_observations(session, observations, code, actor=actor)
        session.add(AuditLog(actor=actor, action="pull", entity="data_source", entity_id=code, detail=stats))
        return stats

    # ------------------------------ 日誌與維護 ------------------------------
    @app.get("/api/audit")
    def audit(limit: int = 200, session: Session = Depends(get_db)):
        rows = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000)))
        return [{"ts": a.ts, "actor": a.actor, "action": a.action, "entity": a.entity,
                 "entity_id": a.entity_id, "detail": a.detail} for a in rows]

    @app.post("/api/admin/reload-config")
    def admin_reload():
        """改完 config/ 下的 YAML 後調用，不用重啟服務。配置有錯時保持原配置不變。"""
        try:
            return reload_config()
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app
