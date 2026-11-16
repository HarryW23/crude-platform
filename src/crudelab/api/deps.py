"""接口層的公共依賴：應用狀態、數據庫會話、操作者、指標引擎。"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from .. import db
from ..config import Config
from ..indicators import IndicatorEngine
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


def get_engine(session: Session = Depends(get_db), state: AppState = Depends(get_state),
               as_of: datetime | None = Query(None, description="還原這個時點（UTC）當時的數據"),
               ) -> IndicatorEngine:
    return IndicatorEngine(session, state.cfg, as_of=as_of.replace(tzinfo=None) if as_of else None)
