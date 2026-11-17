"""後台與前台共用的接口。頁面、命令行以外的任何調用方（包括之後的大模型工具調用）都走這一套。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from ..config import load_config
from ..master import sync_to_db
from ..settings import Settings, get_settings
from .deps import AppState
from .routes import (
    analysis,
    balances,
    core,
    geo,
    imports,
    indicators,
    outlook,
    parameters,
    refineries,
    reports,
    scenarios,
    series,
    sources,
)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def load_and_sync(settings: Settings):
    """載入配置並同步到數據庫。配置有錯時拋 ConfigError，數據庫不變。"""
    cfg = load_config(settings.config_dir)
    with db.session_scope() as session:
        stats = sync_to_db(session, cfg)
    return cfg, stats


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db.init_engine(settings)
    cfg, _ = load_and_sync(settings)

    app = FastAPI(title="crudelab", version="0.4.0")
    app.state.crudelab = AppState(settings=settings, cfg=cfg)
    for module in (core, series, imports, indicators, parameters, refineries, balances, scenarios, analysis,
                   geo, outlook, reports, sources):
        app.include_router(module.router, prefix="/api")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
