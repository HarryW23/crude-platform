from pathlib import Path

import pytest

from crudelab import db
from crudelab.config import load_config
from crudelab.imports import commit_batch, preview_file
from crudelab.master import sync_to_db
from crudelab.sample import write_sample_workbook
from crudelab.settings import PROJECT_ROOT, Settings

CONFIG_DIR = PROJECT_ROOT / "config"


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    """測試不讀、不寫使用者真正的密鑰文件，也不受外面環境變量裡的密鑰影響。"""
    from crudelab import credentials
    monkeypatch.setenv("CRUDELAB_SECRETS_FILE", str(tmp_path / "home" / "secrets.env"))
    for name in credentials.KNOWN:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credentials, "_cache", None)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=CONFIG_DIR, data_dir=tmp_path / "data",
                    database_url=f"sqlite:///{tmp_path / 'data' / 'test.db'}")


@pytest.fixture(scope="session")
def cfg():
    return load_config(CONFIG_DIR)


@pytest.fixture()
def session(settings, cfg):
    db.init_engine(settings)
    with db.session_scope() as s:
        sync_to_db(s, cfg)
    s = db.get_session()
    yield s
    s.close()


@pytest.fixture()
def sample_path(tmp_path) -> Path:
    path = tmp_path / "sample.xlsx"
    write_sample_workbook(path)
    return path


@pytest.fixture()
def loaded(session, settings, sample_path):
    """已導入演示數據的會話。"""
    preview = preview_file(session, settings, sample_path.read_bytes(), "sample.xlsx", "demo")
    commit_batch(session, preview["batch_id"])
    session.commit()
    return session


@pytest.fixture()
def sites(settings):
    """前台和後台兩個站點的測試客戶端，共用同一個數據庫。"""
    from fastapi.testclient import TestClient

    from crudelab.api.app import create_app
    return {"front": TestClient(create_app(settings, "front")), "admin": TestClient(create_app(settings, "admin"))}


@pytest.fixture()
def client(sites):
    return sites["front"]


@pytest.fixture()
def admin(sites):
    return sites["admin"]
