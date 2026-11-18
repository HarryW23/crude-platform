"""兩個站點：前台（研究）和後台（數據與參數管理）。

它們是兩個獨立的應用，共用同一個數據庫和同一套計算核心，但接口不同：

  前台  只讀的數據和指標 + 研究用的功能（情境、事件、推演、報告、問答）。
        它的接口裡**沒有**導入數據、錄入數據、改參數、取數這些操作。
  後台  只讀的數據和指標 + 數據與參數的管理。
        它的接口裡**沒有**情境、事件、推演、報告、問答。

分開的原因：兩邊的使用者和權限不同。部署時可以只把前台開放出去，後台只對內網或本機開放；
以後加登錄，兩邊也可以用不同的權限。兩個應用可以在同一個進程裡各佔一個端口，也可以分開跑。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import auth, credentials, db
from ..settings import Settings, get_settings
from .deps import AppState, Site
from .routes import (
    analysis,
    backtest,
    backups,
    balances,
    chat,
    core,
    geo,
    imports,
    indicators,
    journal,
    llm_admin,
    outlook,
    parameters,
    refineries,
    reports,
    scenarios,
    schedule,
    series,
    sources,
)
from .routes import auth as auth_routes
from .security import guard

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

_COMMON = [core.router, series.router, indicators.router, parameters.router, auth_routes.router]
ROUTERS = {
    "front": [*_COMMON, refineries.router, balances.router, analysis.router, scenarios.router, geo.router,
              outlook.router, reports.router, chat.router, backtest.router, journal.router],
    "admin": [*_COMMON, core.admin_router, series.admin_router, imports.admin_router, parameters.admin_router,
              sources.admin_router, auth_routes.admin_router, schedule.admin_router, backups.admin_router,
              llm_admin.admin_router],
}
_TITLES = {"front": "crudelab 前台", "admin": "crudelab 後台"}
_PAGES = {"front": "index.html", "admin": "admin.html"}


class NoAdminAccount(RuntimeError):
    """開了登錄卻沒有管理員：拒絕啟動，否則誰也進不去後台。"""


def create_app(settings: Settings | None = None, site: Site = "front") -> FastAPI:
    settings = settings or get_settings()
    db.init_engine(settings)
    credentials.install_log_redaction()
    app = FastAPI(title=_TITLES[site], version="0.6.0")

    @app.exception_handler(HTTPException)
    async def _redacted_http_error(_request, exc: HTTPException):
        """所有錯誤回應出門前再過一遍脫敏。各處本來就不該把密鑰放進錯誤訊息，這是最後一道保險。"""
        detail = credentials.redact(exc.detail) if isinstance(exc.detail, str) else exc.detail
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)
    app.state.crudelab = AppState(settings, site)
    if settings.auth_enabled:
        with db.session_scope() as session:
            if auth.count_admins(session) == 0:
                raise NoAdminAccount("登錄功能已開啟，但還沒有任何管理員帳號。"
                                     "請先運行：crudelab user add 用戶名 --role admin")
    for router in ROUTERS[site]:
        app.include_router(router, prefix="/api", dependencies=[Depends(guard)])   # 每個接口都先過訪問控制

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / _PAGES[site])

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
