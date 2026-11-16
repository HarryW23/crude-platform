"""數據庫連接與表結構升級。SQLite（本機預設）與 PostgreSQL 共用同一套模型和遷移腳本。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import Settings, get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def migrate(engine: Engine) -> None:
    """把表結構升級到最新版本。已是最新時什麼都不做。"""
    cfg = alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def init_engine(settings: Settings | None = None) -> Engine:
    """建立連接並升級表結構。重複調用會重建（測試時用來切換數據庫）。"""
    global _engine, _factory
    settings = settings or get_settings()
    is_sqlite = settings.database_url.startswith("sqlite")
    if is_sqlite:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(settings.database_url)
    if is_sqlite:
        @event.listens_for(_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):   # SQLite 預設不檢查外鍵
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    _factory = sessionmaker(_engine, expire_on_commit=False)
    migrate(_engine)
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
