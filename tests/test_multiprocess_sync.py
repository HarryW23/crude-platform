"""幾個進程同時同步同一份新配置（#35 查出的問題）：撞上唯一鍵時重試；一直失敗就保持舊配置，下次檢查再試。"""
import shutil

import pytest
from sqlalchemy.exc import IntegrityError

from crudelab import db
from crudelab.api import deps
from crudelab.settings import PROJECT_ROOT, Settings


@pytest.fixture()
def state(tmp_path, settings, monkeypatch):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    local = Settings(config_dir=tmp_path / "config", data_dir=settings.data_dir, database_url=settings.database_url)
    db.init_engine(local)
    monkeypatch.setattr(deps, "CONFIG_CHECK_INTERVAL", 0)
    monkeypatch.setattr(deps.time, "sleep", lambda s: None)
    return deps.AppState(local, "front")


def rename_espo(state, name):
    path = state.settings.config_dir / "parameters" / "conversion.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f"name_zh: {state.cfg.parameters['BBL_PER_T_ESPO'].name_zh}", f"name_zh: {name}"),
                    encoding="utf-8")


def conflict(times, real):
    calls = {"n": 0}

    def sync(session, cfg):
        calls["n"] += 1
        if calls["n"] <= times:
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))   # 另一個進程先寫了
        return real(session, cfg)
    return sync, calls


def test_sync_conflict_is_retried(state, monkeypatch):
    sync, calls = conflict(1, deps.sync_to_db)
    monkeypatch.setattr(deps, "sync_to_db", sync)
    rename_espo(state, "ESPO 一")
    assert state.cfg.parameters["BBL_PER_T_ESPO"].name_zh == "ESPO 一" and calls["n"] == 2


def test_persistent_db_error_keeps_old_config_and_tries_again(state, monkeypatch):
    real = deps.sync_to_db
    sync, calls = conflict(deps.SYNC_ATTEMPTS, real)
    monkeypatch.setattr(deps, "sync_to_db", sync)
    rename_espo(state, "ESPO 二")
    assert state.cfg.parameters["BBL_PER_T_ESPO"].name_zh != "ESPO 二"      # 沒同步進數據庫：不換配置
    assert state.cfg.parameters["BBL_PER_T_ESPO"].name_zh == "ESPO 二"      # 下一次檢查再試，這次成功
