"""表結構：遷移腳本建出來的庫必須和 models.py 一致。改了模型忘了寫遷移，這個測試會失敗。"""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from crudelab import db
from crudelab.models import Base


def test_migrations_match_models(settings):
    engine = db.init_engine(settings)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_migrate_is_repeatable(settings):
    engine = db.init_engine(settings)
    db.migrate(engine)
    db.init_engine(settings)
