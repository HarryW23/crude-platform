"""SQLite 遷移的安全性：有數據的庫能升級；中途失敗全部回滾，不留半截狀態（0.30.0 的 0016 就這樣出過事）。"""
import pytest
import sqlalchemy as sa
from alembic import command

from crudelab import db


def old_db(tmp_path, revision: str, leftover: bool = False) -> sa.Engine:
    """停在 revision 的庫，帶一條被別的表引用著的序列（外鍵檢查開著，和平台運行時一樣）。"""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    sa.event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys=ON"))
    alembic = db.alembic_config()
    with engine.begin() as conn:
        alembic.attributes["connection"] = conn
        command.upgrade(alembic, revision)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO unit (code, name_zh, dimension) VALUES ('x', 'x', 'x')"))
        conn.execute(sa.text("INSERT INTO commodity (code, name_zh, category) VALUES ('X', 'x', 'crude')"))
        conn.execute(sa.text("INSERT INTO location (code, name_zh, region) VALUES ('X', 'x', 'x')"))
        conn.execute(sa.text("INSERT INTO series (code, name_zh, commodity_code, location_code, kind, unit_code, "
                             "frequency, valid_days, module, external_ids, active) "
                             "VALUES ('A', 'a', 'X', 'X', 'spot', 'x', 'daily', 0, 'common', '{}', 1)"))
        conn.execute(sa.text("INSERT INTO series_alias (alias_norm, alias, series_code, origin) "
                             "VALUES ('a', 'a', 'A', 'config')"))
        conn.execute(sa.text("INSERT INTO job_run (job_code, slot, trigger, status, started_at) "   # 有執行歷史
                             "VALUES ('PULL_EIA', '2026-11-16 23:30:00', 'schedule', 'ok', '2026-11-16 23:30:01')"))
        if leftover:                                   # 0.30.0 失敗後留下的臨時表
            conn.execute(sa.text("CREATE TABLE _alembic_tmp_series (code VARCHAR(64))"))
    return engine


def state(engine: sa.Engine) -> dict:
    with engine.connect() as conn:
        return {"version": conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar(),
                "series": conn.execute(sa.text("SELECT code FROM series")).scalars().all(),
                "aliases": conn.execute(sa.text("SELECT series_code FROM series_alias")).scalars().all(),
                "tables": set(conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")).scalars())}


@pytest.mark.parametrize("leftover", [False, True])
def test_upgrade_keeps_data_when_series_is_referenced(tmp_path, leftover):
    engine = old_db(tmp_path, "0015", leftover)
    db.migrate(engine)
    got = state(engine)
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(db.alembic_config()).get_current_head()
    assert got["version"] == head and got["series"] == ["A"] and got["aliases"] == ["A"]
    assert "_alembic_tmp_series" not in got["tables"]
    with engine.connect() as conn:                                            # 0018 重建 job_run：歷史保留，attempt = 1
        assert conn.execute(sa.text("SELECT job_code, status, attempt FROM job_run")).all() == [("PULL_EIA", "ok", 1)]


def test_failed_migration_rolls_back_everything(tmp_path, monkeypatch):
    engine = old_db(tmp_path, "0015")

    def half_done(cfg, _target):
        conn = cfg.attributes["connection"]
        conn.exec_driver_sql("CREATE TABLE half_done (x INTEGER)")
        conn.exec_driver_sql("UPDATE alembic_version SET version_num = '0016'")
        raise RuntimeError("中途出錯")
    monkeypatch.setattr(db.command, "upgrade", half_done)
    with pytest.raises(RuntimeError, match="中途出錯"):
        db.migrate(engine)
    got = state(engine)
    assert got["version"] == "0015" and "half_done" not in got["tables"]     # 建表也回滾了


def test_migration_that_breaks_references_is_rejected(tmp_path, monkeypatch):
    engine = old_db(tmp_path, "0015")

    def dangling(cfg, _target):
        cfg.attributes["connection"].exec_driver_sql(
            "INSERT INTO series_alias (alias_norm, alias, series_code, origin) VALUES ('b', 'b', 'NOPE', 'config')")
    monkeypatch.setattr(db.command, "upgrade", dangling)
    with pytest.raises(db.MigrationError, match="斷掉的引用"):
        db.migrate(engine)
    assert state(engine)["aliases"] == ["A"]                                  # 插入的壞數據已回滾


def test_check_constraints_refuse_bad_existing_data_without_changing_it(tmp_path):
    """0021 加取值約束前先檢查已有數據：有不合規的值就停下、列出來，整個遷移回滾，數據不變。"""
    engine = old_db(tmp_path, "0020")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE job_run SET status = 'okay'"))                  # 拼錯的狀態
    with pytest.raises(Exception, match=r"job_run.status = 'okay'（1 行）"):
        db.migrate(engine)
    assert state(engine)["version"] == "0020"
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT status FROM job_run")).scalar() == "okay"
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE job_run SET status = 'ok'"))
    db.migrate(engine)
    with engine.begin() as conn, pytest.raises(sa.exc.IntegrityError):                # 之後數據庫直接拒絕
        conn.execute(sa.text("UPDATE job_run SET status = 'okay'"))
