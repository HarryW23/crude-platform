import os
import shutil
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
    monkeypatch.setenv("CRUDELAB_LOCAL_FILE", str(tmp_path / "home" / "local.env"))     # 也不讀使用者的本機設定
    monkeypatch.setenv("CRUDELAB_DATA_DIR", str(tmp_path / "data"))      # 走命令行的測試也不能碰項目裡的 data/
    monkeypatch.delenv("CRUDELAB_BACKUP_DIR", raising=False)
    for name in [*credentials.KNOWN, *(n for n in os.environ if credentials.is_known(n))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credentials, "_cache", None)
    # 數據庫連接是模塊級的：每個測試開始時清掉，免得讀到上一個測試的數據庫（需要數據庫的測試用 session 夾具重新連）
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_factory", None)


# 在 PostgreSQL 上跑全部測試（#34）：設 CRUDELAB_TEST_PG 為不含庫名的連接串，例如
#   CRUDELAB_TEST_PG=postgresql+psycopg://postgres@127.0.0.1:5432 pytest
# 不設就用 SQLite。
#
# 模板（兩種數據庫都一樣）：整個測試會話只做一次「遷移到最新 + 同步配置」，以及「再導入演示數據」，
# 每個測試從模板複製一份（PostgreSQL 用 CREATE DATABASE … TEMPLATE，SQLite 複製文件，都是毫秒級）。
# 用到 loaded 夾具的測試直接從帶演示數據的模板開始，不必每個都重新導入一遍（原來每個測試準備要 1.5 秒）。
TEST_PG = os.environ.get("CRUDELAB_TEST_PG", "").rstrip("/")
# 多核並行（pytest -n）時每個進程各建一套模板：名字帶上進程編號，互不刪除對方的模板
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
PG_TEMPLATE = f"crudelab_test_template_{_WORKER}"
PG_TEMPLATE_LOADED = f"crudelab_test_template_loaded_{_WORKER}"


def _pg_admin():
    from sqlalchemy import create_engine
    return create_engine(f"{TEST_PG}/postgres", isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def cfg():
    return load_config(CONFIG_DIR)


def _build(settings: Settings, cfg, with_sample: bool) -> None:
    """在 settings 指向的庫裡：遷移、同步配置，需要時導入演示數據。用完把連接還原成「沒有連接」。"""
    saved = db._engine, db._factory
    try:
        db.init_engine(settings)
        with db.session_scope() as s:
            sync_to_db(s, cfg)
        if with_sample:
            sample = settings.data_dir / "sample.xlsx"
            write_sample_workbook(sample)
            with db.session_scope() as s:
                preview = preview_file(s, settings, sample.read_bytes(), "sample.xlsx", "demo")
                commit_batch(s, preview["batch_id"])
        if settings.database_url.startswith("sqlite"):            # 把 WAL 併回主文件，模板才是一個完整的文件
            with db._engine.connect() as c:
                c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        db._engine.dispose()
    finally:
        db._engine, db._factory = saved


@pytest.fixture(scope="session")
def templates(tmp_path_factory, cfg):
    """{"base": (數據目錄, 庫), "loaded": 同上，帶演示數據}。數據目錄裡有導入過的原始文件（SQLite 的庫文件也在裡面）；
    庫：SQLite 為 None，PostgreSQL 是模板庫的名字。"""
    if not TEST_PG:
        out = {}
        for kind in ("base", "loaded"):
            root = tmp_path_factory.mktemp(f"template_{kind}") / "data"
            _build(Settings(config_dir=CONFIG_DIR, data_dir=root, database_url=f"sqlite:///{root / 'test.db'}"),
                   cfg, kind == "loaded")
            out[kind] = (root, None)
        yield out
        return
    from sqlalchemy import text
    admin = _pg_admin()
    names = {"base": PG_TEMPLATE, "loaded": PG_TEMPLATE_LOADED}
    with admin.connect() as c:                   # 上次被中途殺掉（Ctrl+C、超時）沒來得及刪的本進程測試庫
        leftovers = c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                              {"p": f"t\\_{_WORKER}\\_%"}).scalars().all()
        for name in leftovers:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    out = {}
    for kind, name in names.items():
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            c.execute(text(f"CREATE DATABASE {name}"))
        data = tmp_path_factory.mktemp(f"template_{kind}") / "data"
        _build(Settings(config_dir=CONFIG_DIR, data_dir=data, database_url=f"{TEST_PG}/{name}"), cfg, kind == "loaded")
        out[kind] = (data, name)
    yield out
    with admin.connect() as c:
        for name in names.values():
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture()
def settings(request, tmp_path: Path, templates) -> Settings:
    empty = request.node.get_closest_marker("empty_db") is not None
    kind = "loaded" if "loaded" in request.fixturenames else "base"
    data = tmp_path / "data"
    template_dir, template_db = templates[kind]
    if empty:
        data.mkdir(parents=True)
    else:
        shutil.copytree(template_dir, data)                       # 導入過的原始文件（SQLite 連同庫文件）
    if not TEST_PG:
        yield Settings(config_dir=CONFIG_DIR, data_dir=data, database_url=f"sqlite:///{data / 'test.db'}")
        return
    import uuid

    from sqlalchemy import text
    name = f"t_{_WORKER}_{uuid.uuid4().hex[:12]}"
    admin = _pg_admin()
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {name}" + ("" if empty else f" TEMPLATE {template_db}")))
    yield Settings(config_dir=CONFIG_DIR, data_dir=data, database_url=f"{TEST_PG}/{name}")
    if db._engine is not None:
        db._engine.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture()
def session(settings, cfg):
    db.init_engine(settings)                                       # 模板已經遷移、同步過配置
    s = db.get_session()
    yield s
    s.close()


@pytest.fixture()
def sample_path(tmp_path) -> Path:
    path = tmp_path / "sample.xlsx"
    write_sample_workbook(path)
    return path


@pytest.fixture()
def loaded(session):
    """已導入演示數據的會話（settings 看到測試用了 loaded，就從帶演示數據的模板複製）。"""
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


@pytest.fixture()
def pass_drift_gate(session):
    """記一次「這個漂移來源讓 1 個月的推演誤差顯著變小」的回測結果，讓推演可以選它（門檻本身另有測試）。"""
    from crudelab.models import BacktestRun

    def apply(*codes: str, passed: bool = True) -> None:
        gate = {"M1": {"significant": passed, "improvement": 0.05 if passed else -0.2, "t": 2.5 if passed else -3}}
        session.add(BacktestRun(created_by="t", status="done", mode="observation", step=1, results={
            "drift_models": {c: {"by_horizon": gate} for c in codes},
            "fair_values": [{"code": c, "drift": gate} for c in codes]}))
        session.commit()                    # 接口測試用另一個會話，要能看到
    return apply
