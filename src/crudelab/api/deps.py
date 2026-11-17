"""接口層的公共依賴：應用狀態、數據庫會話、操作者、指標引擎。"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from .. import db
from ..config import Config
from ..indicators import IndicatorEngine
from ..scenarios import ScenarioError
from ..scenarios import load as load_scenario
from ..settings import Settings


@dataclass
class AppState:
    settings: Settings
    cfg: Config


def get_state(request: Request) -> AppState:
    return request.app.state.crudelab


def get_db() -> Iterator[Session]:
    with db.session_scope() as session:
        yield session


def get_actor() -> str:
    """當前操作者。第一期單人本機使用，固定為 local；接登錄時只改這一處。"""
    return "local"


def get_as_of(as_of: datetime | None = Query(None, description="還原這個時點（UTC）當時的數據")) -> datetime | None:
    return as_of.replace(tzinfo=None) if as_of else None


def get_engine(session: Session = Depends(get_db), state: AppState = Depends(get_state),
               as_of: datetime | None = Depends(get_as_of),
               scenario: str | None = Query(None, description="套用這個已保存的情境")) -> IndicatorEngine:
    """按請求參數建引擎：as_of 還原歷史時點，scenario 套用已保存的情境。兩者都不給就是當前的基準視圖。"""
    try:
        loaded = load_scenario(session, state.cfg, scenario, as_of) if scenario else None
    except ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc
    return IndicatorEngine(session, state.cfg, as_of=as_of, scenario=loaded)


def get_base_engine(session: Session = Depends(get_db), state: AppState = Depends(get_state),
                    as_of: datetime | None = Depends(get_as_of)) -> IndicatorEngine:
    """不套情境的基準引擎。用於需要「基準 vs 情境」對照的接口。"""
    return IndicatorEngine(session, state.cfg, as_of=as_of)
