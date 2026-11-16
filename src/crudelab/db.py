"""數據庫連接。SQLite（本機預設）與 PostgreSQL 共用同一套模型。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import Settings, get_settings

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def init_engine(settings: Settings | None = None) -> Engine:
    """建立引擎並確保表存在。重複調用會重建（測試時用來切換數據庫）。"""
    global _engine, _factory
    settings = settings or get_settings()
    if settings.database_url.startswith("sqlite"):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(settings.database_url, future=True)
    _factory = sessionmaker(_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    if _factory is None:
        init_engine()
    assert _factory is not None
    return _factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
