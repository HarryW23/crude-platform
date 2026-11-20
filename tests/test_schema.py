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


def test_every_migration_rolls_back_and_forward(settings):
    """升級到最新 → 一步步降級到最初 → 再升級：每個 downgrade 都能跑（出事時要能回滾），
    回來之後的表結構仍和 models.py 完全一致。SQLite 和 PostgreSQL（CRUDELAB_TEST_PG）都跑。"""
    import sqlalchemy as sa
    from alembic import command
    from alembic.script import ScriptDirectory
    engine = db.init_engine(settings)
    cfg = db.alembic_config()
    revisions = [r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()]   # 新 → 舊
    for revision in revisions:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert current == revision, f"降級順序不對：期望 {revision}，實際 {current}"
            command.downgrade(cfg, "-1")
    left = set(sa.inspect(engine).get_table_names()) - {"alembic_version"}
    assert left == set(), f"全部降級後還剩這些表：{left}"
    db.migrate(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


# 有意不還原的遷移（只改數據、單向的）：必須在這裡寫明理由，新加的空 downgrade 一律算錯
NO_DOWNGRADE = {"0015": "快訊改用網址做唯一鍵，舊編號是內容雜湊，表結構不變，無法也不需要還原",
                "0017": "別名轉簡體是單向的數據歸一化，表結構不變，舊值無法也不需要還原"}


def test_every_migration_has_a_real_downgrade():
    from alembic.script import ScriptDirectory
    for script in ScriptDirectory.from_config(db.alembic_config()).walk_revisions():
        if script.revision in NO_DOWNGRADE:
            continue
        source = open(script.path, encoding="utf-8").read()
        body = source.split("def downgrade()", 1)[1]
        assert "op." in body or "_drop" in body, f"{script.revision} 的 downgrade 是空的：出事時沒法回滾"
