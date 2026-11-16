from pathlib import Path

import pytest

from crudelab import db
from crudelab.config import load_config
from crudelab.imports import commit_batch, preview_file
from crudelab.master import sync_to_db
from crudelab.sample import write_sample_workbook
from crudelab.settings import PROJECT_ROOT, Settings

CONFIG_DIR = PROJECT_ROOT / "config"


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
