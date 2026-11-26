"""數據庫連接與表結構升級。SQLite（本機預設）與 PostgreSQL 共用同一套模型和遷移腳本。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar, cast

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import Settings, get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


class MigrationError(RuntimeError):
    pass


def migrate(engine: Engine) -> None:
    """把表結構升級到最新版本。已是最新時什麼都不做。

    SQLite 上單獨開一個連接來遷移：
      - 整個遷移在一個事務裡（建表、改表也在內）：中途失敗就全部回滾，不會留下改了一半的庫
        （Python 的 sqlite3 默認不給建表語句開事務，所以要自己發 BEGIN）
      - 遷移期間關閉外鍵檢查：SQLite 改表常要「建新表 → 複製 → 刪舊表」，舊表被別的表引用時刪不掉
      - 遷移完、提交前做一次外鍵完整性檢查，比遷移前多出斷掉的引用就回滾並報錯
    """
    cfg = alembic_config()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        return
    migrator = create_engine(engine.url)

    @event.listens_for(migrator, "connect")
    def _connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None                    # 由下面的 BEGIN 自己管事務
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")        # 必須在事務外設置才生效
        dbapi_connection.execute("PRAGMA busy_timeout=5000")

    @event.listens_for(migrator, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    try:
        with migrator.begin() as connection:
            cfg.attributes["connection"] = connection
            before = len(connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall())
            command.upgrade(cfg, "head")
            after = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if len(after) > before:                     # 只攔遷移新造成的；庫裡原有的不擋啟動
                raise MigrationError(f"升級表結構後多了 {len(after) - before} 處斷掉的引用"
                                     f"（例如表 {after[0][0]}），已全部回滾，數據沒有改動")
    finally:
        migrator.dispose()


def schema_revision(engine: Engine) -> str | None:
    """數據庫現在的表結構版本（alembic revision）；還沒建過表時是 None。"""
    from alembic.runtime.migration import MigrationContext
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str:
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


class SchemaOutdated(RuntimeError):
    pass


def init_engine(settings: Settings | None = None, upgrade: bool = True, require_current: bool = True) -> Engine:
    """建立連接並升級表結構。重複調用會重建（測試時用來切換數據庫）。

    upgrade=False：只讀的命令用（查統計、做備份）。不升級表結構；表結構不是最新時報 SchemaOutdated，
    讓使用者先自己運行 crudelab init——升級可能按新的規則清理數據，不能在查個數的時候順帶發生。
    require_current=False：連表結構舊也照樣打開（備份：升級前先備份舊庫，正是最需要備份的時候）。"""
    global _engine, _factory
    settings = settings or get_settings()
    is_sqlite = settings.database_url.startswith("sqlite")
    settings.data_dir.mkdir(parents=True, exist_ok=True)       # 原始文件、收件夾、備份都在這裡，用哪種數據庫都要有
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(settings.database_url)
    if is_sqlite:
        @event.listens_for(_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):   # SQLite 預設不檢查外鍵
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.execute("PRAGMA journal_mode=WAL")      # 前台、後台兩個進程同時讀寫
            dbapi_connection.execute("PRAGMA busy_timeout=5000")
    _factory = sessionmaker(_engine, expire_on_commit=False)
    if upgrade:
        migrate(_engine)
        return _engine
    if not require_current:
        return _engine
    current, head = schema_revision(_engine), head_revision()
    if current != head:
        raise SchemaOutdated(f"數據庫的表結構是 {current or '（空庫）'}，程序需要 {head}。這個命令只讀、不升級：先運行 "
                             "crudelab init（會升級表結構、按配置同步，並列出清理了哪些數據點），再重試")
    return _engine


_T = TypeVar("_T")


def require(session: Session, model: type[_T], key) -> _T:
    """按主鍵取一條「一定存在」的記錄（剛建立的、或剛查到編號的）。真的不在時報出是哪張表哪個編號，
    而不是之後在別處出現看不懂的 'NoneType' object has no attribute。"""
    row = session.get(model, key)
    if row is None:
        raise LookupError(f"{getattr(model, '__tablename__', model.__name__)} 裡找不到 {key}")
    return row


def rowcount(result) -> int:
    """UPDATE / DELETE 實際改了幾行（條件更新用它判斷「是不是我改到的」）。"""
    return cast(CursorResult, result).rowcount or 0


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
