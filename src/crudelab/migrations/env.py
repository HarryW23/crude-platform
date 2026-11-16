"""Alembic 環境。程序啟動時由 db.migrate() 帶著現成的連接調用；
開發時用命令行生成新遷移（見 docs/architecture.md「改表結構」）則自己按 DATABASE_URL 連接。"""
from alembic import context
from sqlalchemy import create_engine

from crudelab.models import Base
from crudelab.settings import get_settings

target_metadata = Base.metadata


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with create_engine(settings.database_url).connect() as conn:
        run(conn)
        conn.commit()
