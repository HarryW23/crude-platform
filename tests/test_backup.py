"""備份與恢復：內容、校驗、篡改檢測、恢復往返、安全檢查、保留策略、定時任務、接口。"""
import io
import json
import os
import tarfile
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import URL

import crudelab
from crudelab import backup, db, jobs
from crudelab.models import AuditLog, Observation, SchedulerHeartbeat, utcnow
from crudelab.settings import PROJECT_ROOT


def obs_count(session) -> int:
    return session.scalar(select(func.count()).select_from(Observation))


def members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tar:
        return tar.getnames()


def test_version_matches_pyproject():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]
    assert crudelab.__version__ == project["version"]


def test_backup_contents_and_exclusions(loaded, settings, cfg):
    (settings.data_dir / "secrets.env").write_text("X=1")                # 看起來像密鑰的文件一律不備份
    (settings.data_dir / "server.pem").write_text("-----")
    (settings.data_dir / "inbox").mkdir(exist_ok=True)
    (settings.data_dir / "inbox" / "note.csv").write_text("a,b")
    made = backup.create(settings, cfg, "manual")
    path = backup.directory(settings, cfg) / made["name"]
    names = members(path)
    db_arc = backup.DB_SQLITE if backup.database_kind(settings) == "sqlite" else backup.DB_PG
    assert names[0] == "manifest.json" and db_arc in names
    assert "data/inbox/note.csv" in names and any(n.startswith("data/raw/") for n in names)
    assert any(n == "config/backup.yaml" for n in names)
    assert not any(n.endswith(("secrets.env", ".pem", "test.db", "-wal", "-shm")) for n in names)
    assert not any("backups/" in n for n in names)                        # 不把備份目錄自己打包進去
    manifest = backup.read_manifest(path)
    assert manifest["version"] == crudelab.__version__ and manifest["secrets_included"] is False
    assert manifest["counts"]["observation"] == obs_count(loaded) > 0
    assert manifest["revision"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert backup.directory(settings, cfg).stat().st_mode & 0o777 == 0o700
    assert made["observations"] == manifest["counts"]["observation"] and made["error"] is None
    assert not list(backup.directory(settings, cfg).glob("*.partial"))


def test_raw_files_can_be_left_out(loaded, settings, cfg):
    lean = cfg.backup.model_copy(update={"include_raw": False})
    original = cfg.backup
    cfg.backup = lean
    try:
        made = backup.create(settings, cfg, "manual")
    finally:
        cfg.backup = original
    assert not any(n.startswith("data/raw/") for n in members(backup.directory(settings, cfg) / made["name"]))


def _rewrite(src: Path, dest: Path, change) -> None:
    """複製一份備份，用 change(name, data) 改其中的內容（清單不變）。"""
    with tarfile.open(src, "r:gz") as tin, tarfile.open(dest, "w:gz") as tout:
        for m in tin:
            data = tin.extractfile(m).read()
            data = change(m.name, data)
            if data is None:
                continue
            m.size = len(data)
            tout.addfile(m, io.BytesIO(data))


def test_verify_detects_tampering_and_damage(loaded, settings, cfg):
    made = backup.create(settings, cfg, "manual")
    folder = backup.directory(settings, cfg)
    good = folder / made["name"]
    assert backup.verify(good)["ok"]

    edited = folder / "crudelab-20200101-000000-manual.tar.gz"
    _rewrite(good, edited, lambda n, d: d.replace(b"keep_daily", b"keep_dailx") if n == "config/backup.yaml" else d)
    with pytest.raises(backup.BackupError, match="校驗碼不符"):
        backup.verify(edited)

    dropped = folder / "crudelab-20200102-000000-manual.tar.gz"
    _rewrite(good, dropped, lambda n, d: None if n == "config/backup.yaml" else d)
    with pytest.raises(backup.BackupError, match="缺少"):
        backup.verify(dropped)

    truncated = folder / "crudelab-20200103-000000-manual.tar.gz"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    with pytest.raises(backup.BackupError):
        backup.verify(truncated)

    evil = folder / "crudelab-20200104-000000-manual.tar.gz"
    _rewrite(good, evil, lambda n, d: d)
    with tarfile.open(evil, "r:gz") as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
    manifest["files"].append({"path": "../escape.txt", "size": 1, "sha256": "0"})
    _rewrite(good, evil, lambda n, d: json.dumps(manifest).encode() if n == "manifest.json" else d)
    with pytest.raises(backup.BackupError):
        backup.verify(evil)

    with pytest.raises(backup.BackupError):
        backup.resolve(settings, cfg, "../../etc/passwd")


def test_restore_round_trip(loaded, settings, cfg):
    before = obs_count(loaded)
    made = backup.create(settings, cfg, "manual")
    raw = next((settings.data_dir / "raw").rglob("*.*"))
    raw.unlink()
    loaded.execute(Observation.__table__.delete())
    loaded.commit()
    loaded.close()
    check = db.get_session()
    assert obs_count(check) == 0
    check.close()                       # 不關的話 PostgreSQL 上它一直持有鎖，恢復時刪表會等它

    result = backup.restore(settings, cfg, backup.directory(settings, cfg) / made["name"])
    session = db.get_session()
    assert obs_count(session) == before
    assert raw.exists()                                                     # 原始文件也回來了
    assert result["safety_backup"] and result["safety_backup"].endswith("pre-restore.tar.gz")
    safety = backup.read_manifest(backup.directory(settings, cfg) / result["safety_backup"])
    assert safety["counts"]["observation"] == 0                             # 恢復前的狀態被保存了
    log = session.scalars(select(AuditLog).where(AuditLog.action == "restore")).one()
    assert log.entity_id == made["name"]
    session.close()


def test_restore_safety_checks(loaded, settings, cfg):
    made = backup.create(settings, cfg, "manual")
    path = backup.directory(settings, cfg) / made["name"]
    loaded.add(SchedulerHeartbeat(id=1, at=utcnow(), host="test"))         # 服務開著
    loaded.commit()
    with pytest.raises(backup.BackupError, match="服務還開著"):
        backup.restore(settings, cfg, path)

    future = path.with_name("crudelab-20200105-000000-manual.tar.gz")      # 更新版本程序做的備份
    def bump(name, data):
        if name != "manifest.json":
            return data
        m = json.loads(data)
        m["revision"] = "9999_from_the_future"
        return json.dumps(m).encode()
    _rewrite(path, future, bump)
    with pytest.raises(backup.BackupError, match="更新版本"):
        backup.restore(settings, cfg, future, force=True)

    sqlite = backup.database_kind(settings) == "sqlite"                    # 換成另一種數據庫：拒絕
    other = settings.__class__(config_dir=settings.config_dir, data_dir=settings.data_dir,
                               database_url="postgresql+psycopg://u@localhost/x" if sqlite
                               else f"sqlite:///{settings.data_dir / 'other.db'}")
    with pytest.raises(backup.BackupError, match="sqlite" if sqlite else "postgresql"):
        backup.restore(other, cfg, path)


def test_prune_keeps_daily_weekly_monthly(settings, cfg):
    folder = backup.directory(settings, cfg)
    folder.mkdir(parents=True)
    start = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    for i in range(300):                                                   # 300 天，每天兩份定時備份
        for hour in (0, 12):
            t = start + timedelta(days=i, hours=hour)
            (folder / f"crudelab-{t:%Y%m%d-%H%M%S}-scheduled.tar.gz").write_bytes(b"x")
    (folder / "crudelab-20260105-120000-manual.tar.gz").write_bytes(b"x")
    (folder / "crudelab-20260106-120000-pre-restore.tar.gz").write_bytes(b"x")
    (folder / "something-else.tar.gz").write_bytes(b"x")
    removed = backup.prune(settings, cfg)
    left = sorted(p.name for p in folder.iterdir())
    scheduled = [n for n in left if n.endswith("scheduled.tar.gz")]
    k = cfg.backup
    assert len(scheduled) <= k.keep_daily + k.keep_weekly + k.keep_monthly and len(removed) > 500
    newest = start + timedelta(days=299, hours=12)
    assert f"crudelab-{newest:%Y%m%d-%H%M%S}-scheduled.tar.gz" in scheduled
    days = {n[9:17] for n in scheduled}
    recent = {f"{start + timedelta(days=299 - i):%Y%m%d}" for i in range(k.keep_daily)}
    assert recent <= days                                                  # 最近 14 天每天都有
    assert len({n[9:15] for n in scheduled}) >= min(k.keep_monthly, 10)     # 每個月都留了一份
    assert {"crudelab-20260105-120000-manual.tar.gz", "crudelab-20260106-120000-pre-restore.tar.gz",
            "something-else.tar.gz"} <= set(left)                          # 不是定時備份的一律不動
    assert backup.prune(settings, cfg) == []                               # 再清一次沒有東西可刪


def test_backup_job(loaded, settings, cfg):
    result = jobs.run_now(cfg, settings, "BACKUP_DAILY")
    assert result["status"] == "ok" and "已備份並校驗" in result["message"]
    assert [b["label"] for b in backup.list_backups(settings, cfg)] == ["scheduled"]


def test_backup_job_failure_is_readable(session, settings, cfg, monkeypatch):
    monkeypatch.setenv("CRUDELAB_BACKUP_DIR", str(settings.data_dir / "blocked"))
    (settings.data_dir / "blocked").write_text("not a folder")
    result = jobs.run_now(cfg, settings, "BACKUP_DAILY")
    assert result["status"] == "failed" and "備份目錄無法寫入" in result["message"]


def test_postgres_secrets_stay_out_of_arguments(settings, monkeypatch):
    seen = {}

    def fake_run(args, env, **_):
        seen["args"], seen["env"] = args, env

        class Done:
            returncode, stderr = 1, "FATAL: password authentication failed for user secretuser password=hunter2"
        return Done()
    monkeypatch.setattr(backup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    url = URL.create("postgresql+psycopg", username="secretuser", password="hunter2",     # 假密碼，只用於測試
                     host="db.internal", port=5433, database="crude", query={"sslmode": "require"})
    pg = settings.__class__(config_dir=settings.config_dir, data_dir=settings.data_dir,
                            database_url=url.render_as_string(hide_password=False))
    with pytest.raises(backup.BackupError) as err:
        backup._run_pg(["pg_dump", "--format=custom", "--file=/x"], pg)
    assert not any("hunter2" in a for a in seen["args"])
    assert seen["env"]["PGPASSWORD"] == "hunter2" and seen["env"]["PGHOST"] == "db.internal"
    assert seen["env"]["PGPORT"] == "5433" and seen["env"]["PGSSLMODE"] == "require"
    assert "hunter2" not in str(err.value)


def test_backup_api(sites, loaded):
    admin, front = sites["admin"], sites["front"]
    assert front.get("/api/backups").status_code == 404
    status = admin.get("/api/backups").json()
    assert status["backups"] == [] and status["stale"]
    assert status["database"] == backup.database_kind(sites["front"].app.state.crudelab.settings)
    made = admin.post("/api/backups").json()
    status = admin.get("/api/backups").json()
    assert [b["name"] for b in status["backups"]] == [made["name"]] and not status["stale"]
    assert admin.post(f"/api/backups/{made['name']}/verify").json()["ok"]
    bad = admin.post("/api/backups/..%2F..%2Fetc%2Fpasswd/verify")
    assert bad.status_code == 404 or bad.json()["ok"] is False


@pytest.mark.skipif(not os.environ.get("CRUDELAB_TEST_PG"), reason="只在 PostgreSQL 上：恢復時有連接佔着表")
def test_pg_restore_refuses_while_another_connection_is_open(loaded, settings, cfg):
    made = backup.create(settings, cfg, "manual")
    loaded.close()
    from sqlalchemy import create_engine, text
    engine = create_engine(settings.database_url, connect_args={"application_name": "dbeaver"})
    holder = engine.connect()
    holder.execute(text("SELECT count(*) FROM observation"))              # 開着事務不提交：持有表鎖
    try:
        with pytest.raises(backup.BackupError, match="1 個連接.*dbeaver"):
            backup.restore(settings, cfg, backup.directory(settings, cfg) / made["name"], force=True)
    finally:
        holder.close()
        engine.dispose()
