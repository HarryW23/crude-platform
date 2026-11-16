from pathlib import Path

import pytest

from crudelab import db
from crudelab.master import load_config, sync_to_db
from crudelab.settings import PROJECT_ROOT, Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path,
                    database_url=f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture()
def cfg(settings):
    return load_config(settings.config_dir)


@pytest.fixture()
def session(settings, cfg):
    db.init_engine(settings)
    with db.session_scope() as s:
        sync_to_db(s, cfg)
    s = db.get_session()
    yield s
    s.close()
