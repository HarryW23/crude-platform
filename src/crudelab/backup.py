"""備份與恢復。

一份備份是一個 .tar.gz 文件，名字是 crudelab-<UTC 時間>-<類型>.tar.gz，類型有 scheduled（定時）、manual（手動）、
pre-restore（恢復前自動做的）。裡面：

  manifest.json          第一個文件：格式版本、程序版本、數據庫類型、表結構版本（alembic revision）、
                         各表行數、每個文件的大小和 SHA-256
  database/crudelab.sqlite   SQLite：用 SQLite 的在線備份接口做的一致快照（服務開著也可以備份，含 WAL 裡的內容）
  database/postgres.dump     PostgreSQL：pg_dump -Fc 的輸出
  config/…               配置文件（YAML）
  data/…                 數據目錄裡除數據庫和備份本身以外的文件：導入時保存的原始文件（raw/）、收件夾等

不包含密鑰：密鑰在 ~/.crudelab/secrets.env，不在數據目錄；看起來像密鑰的文件名（secrets*、*.env、*.pem、*.key）
即使出現在配置或數據目錄裡也一律跳過。換機器恢復後用 crudelab secret set 重新錄入。
備份文件權限設為只有自己可讀（0600），目錄 0700：數據庫裡有帳號的密碼雜湊等內容。

做法：
  1. 先把要備份的東西全部複製到備份目錄下的臨時文件夾（數據庫做快照），之後只讀這份副本，
     所以打包期間文件被改動也不會造成清單和內容不一致。
  2. 算校驗碼、寫清單、打包成 .partial，寫完再改名：任何時候備份目錄裡要麼是完整的備份，要麼沒有。
  3. 立刻完整校驗一遍（逐個文件核對 SHA-256，SQLite 再做 integrity_check），不通過就刪掉並報錯。

恢復（只能用命令行，見 cli.py 的 restore）：
  1. 校驗備份；數據庫類型必須和現在的一致；表結構版本必須是這個程序認識的（更新版本程序做的備份要先升級程序）。
  2. 調度器心跳顯示服務還開著時拒絕（--force 可以跳過，但服務開著時替換數據庫文件會出問題）。
  3. 先把現在的狀態做一份 pre-restore 備份，恢復錯了還能回去。
  4. 替換數據庫（SQLite 原子替換文件；PostgreSQL 用 pg_restore --clean），把 data/ 下的文件放回去
     （只覆蓋同名文件，不刪多出來的）；配置文件預設不動（它們在 git 裡），加 --with-config 才覆蓋。
  5. 升級表結構到當前版本，記一條日誌。

PostgreSQL 的連接信息（含密碼）通過環境變量交給 pg_dump / pg_restore，不出現在命令行參數裡（命令行參數別的用戶看得到）；
它們的錯誤輸出先脫敏再給人看。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import zlib
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from . import __version__, credentials, db
from .config import Config
from .settings import Settings

FORMAT = 1
NAME = re.compile(r"^crudelab-(?P<stamp>\d{8}-\d{6})-(?P<label>scheduled|manual|pre-restore)\.tar\.gz$")
LABELS = {"scheduled": "定時", "manual": "手動", "pre-restore": "恢復前自動"}
DB_SQLITE, DB_PG = "database/crudelab.sqlite", "database/postgres.dump"
ROOTS = ("config/", "data/", "database/")
SECRET_LIKE = re.compile(r"(^secrets|\.env$|^\.env|\.pem$|\.key$|\.p12$|\.pfx$|^id_rsa|^credentials.*\.json$)", re.I)
CHUNK = 1 << 20


class BackupError(Exception):
    """備份或恢復出錯，訊息可以直接給人看。"""


# ------------------------------ 位置與類型 ------------------------------

def directory(settings: Settings, cfg: Config) -> Path:
    raw = os.environ.get("CRUDELAB_BACKUP_DIR", "").strip() or cfg.backup.directory
    path = Path(raw).expanduser()
    return path if path.is_absolute() else settings.data_dir / path


def database_kind(settings: Settings) -> str:
    backend = make_url(settings.database_url).get_backend_name()
    if backend in ("sqlite", "postgresql"):
        return backend
    raise BackupError(f"不支持備份 {backend} 數據庫，只支持 SQLite 和 PostgreSQL")


def _sqlite_path(settings: Settings) -> Path:
    name = make_url(settings.database_url).database
    if not name or name == ":memory:":
        raise BackupError("內存數據庫無法備份")
    return Path(name)


def same_disk(settings: Settings, cfg: Config) -> bool | None:
    """備份目錄和數據目錄是不是在同一塊磁盤上。判斷不了時回 None。"""
    try:
        target = directory(settings, cfg)
        probe = target if target.exists() else next(p for p in target.parents if p.exists())
        return probe.stat().st_dev == settings.data_dir.stat().st_dev
    except (OSError, StopIteration):
        return None


def _private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:          # Windows 等不支持 POSIX 權限的系統
        pass


# ------------------------------ 數據庫快照 ------------------------------

def _check_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError:
        result = "無法打開"
    finally:
        conn.close()
    if result != "ok":
        raise BackupError(f"數據庫快照檢查不通過：{result}")


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    if not src.exists():
        raise BackupError("數據庫文件不存在：還沒有初始化過（crudelab init）")
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)            # SQLite 在線備份：一致快照，包含 WAL 裡已提交的內容
    finally:
        target.close()
        source.close()
    _check_sqlite(dest)


def _sqlite_facts(path: Path) -> tuple[str | None, dict[str, int]]:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        revision = (conn.execute("SELECT version_num FROM alembic_version").fetchone() or [None])[0] \
            if "alembic_version" in tables else None
    finally:
        conn.close()
    counts.pop("alembic_version", None)
    return revision, counts


def _live_facts() -> tuple[str | None, dict[str, int]]:
    from .models import Base
    with db.session_scope() as session:
        counts = {t.name: session.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}
        revision = session.connection().exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    return revision, dict(sorted(counts.items()))


def _pg_env(settings: Settings) -> dict[str, str]:
    """把連接串拆成 libpq 的環境變量。密碼只出現在環境變量裡，不出現在命令行參數裡。"""
    url = make_url(settings.database_url)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    for key, value in (("PGHOST", url.host), ("PGPORT", url.port), ("PGUSER", url.username),
                       ("PGPASSWORD", url.password), ("PGDATABASE", url.database)):
        if value is not None:
            env[key] = str(value)
    if "sslmode" in url.query:
        env["PGSSLMODE"] = str(url.query["sslmode"])
    return env


def _other_pg_connections(settings: Settings) -> list[str]:
    """除了這次檢查本身，還有誰連着這個數據庫。恢復要刪表重建，有別的連接開着事務時 pg_restore 會一直等它
    （備份文件裡自帶 SET lock_timeout = 0，在外面設超時也沒用），所以恢復前先查。"""
    from sqlalchemy import create_engine, text
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT coalesce(nullif(application_name, ''), '未命名客戶端'), "
                                     "coalesce(host(client_addr), '本機') FROM pg_stat_activity "
                                     "WHERE datname = current_database() AND pid <> pg_backend_pid()")).fetchall()
    finally:
        engine.dispose()
    return [f"{name}（{addr}）" for name, addr in rows]


def _run_pg(args: list[str], settings: Settings) -> None:
    exe = shutil.which(args[0])
    if exe is None:
        raise BackupError(f"找不到 {args[0]}：PostgreSQL 的備份要用它，請在這台機器上安裝 PostgreSQL 客戶端"
                          "（版本不低於數據庫服務器）")
    try:
        done = subprocess.run([exe, *args[1:]], env=_pg_env(settings), capture_output=True, text=True,
                              timeout=6 * 3600, check=False)
    except subprocess.TimeoutExpired:
        raise BackupError(f"{args[0]} 超時") from None
    if done.returncode != 0:
        tail = "\n".join(done.stderr.strip().splitlines()[-3:])
        raise BackupError(f"{args[0]} 失敗：{credentials.redact(tail)}")


# ------------------------------ 要備份的文件 ------------------------------

def _is_secret_like(path: Path) -> bool:
    return bool(SECRET_LIKE.search(path.name))


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def collect_files(settings: Settings, cfg: Config) -> list[tuple[Path, str]]:
    """（文件, 備份裡的路徑）。數據庫本身另外處理，不在這裡。"""
    out: list[tuple[Path, str]] = []
    secrets = credentials.secrets_path().resolve()
    if settings.config_dir.exists():
        for p in sorted(settings.config_dir.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not _is_secret_like(p) and p.resolve() != secrets:
                out.append((p, "config/" + p.relative_to(settings.config_dir).as_posix()))
    target = directory(settings, cfg)
    skip: set[Path] = set()
    if database_kind(settings) == "sqlite":
        base = _sqlite_path(settings).resolve()
        skip = {base, *(base.with_name(base.name + s) for s in ("-wal", "-shm", "-journal"))}
    if settings.data_dir.exists():
        for p in sorted(settings.data_dir.rglob("*")):
            if not p.is_file() or p.is_symlink() or _inside(p, target) or p.resolve() in skip:
                continue
            rel = p.relative_to(settings.data_dir)
            if (not cfg.backup.include_raw and rel.parts[0] == "raw") or _is_secret_like(p) or p.resolve() == secrets:
                continue
            out.append((p, "data/" + rel.as_posix()))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------ 備份 ------------------------------

def create(settings: Settings, cfg: Config, label: str = "manual", now: datetime | None = None) -> dict:
    try:
        return _create(settings, cfg, label, now)
    except OSError as exc:
        raise BackupError(f"備份目錄無法寫入或磁盤已滿：{exc.strerror or type(exc).__name__}"
                          f"（{directory(settings, cfg)}）") from None


def _create(settings: Settings, cfg: Config, label: str, now: datetime | None) -> dict:
    if label not in LABELS:
        raise BackupError(f"不認識的備份類型 {label}")
    now = now or datetime.now(UTC)
    target = directory(settings, cfg)
    target.mkdir(parents=True, exist_ok=True)
    _private(target, 0o700)
    name = f"crudelab-{now.astimezone(UTC):%Y%m%d-%H%M%S}-{label}.tar.gz"
    final = target / name
    if final.exists():
        raise BackupError("同一秒已經有一份備份，請稍後再試")
    kind = database_kind(settings)
    with tempfile.TemporaryDirectory(prefix=".crudelab-backup-", dir=target) as tmp:
        stage = Path(tmp) / "stage"
        db_arc = DB_SQLITE if kind == "sqlite" else DB_PG
        db_file = stage / db_arc
        db_file.parent.mkdir(parents=True)
        if kind == "sqlite":
            _snapshot_sqlite(_sqlite_path(settings), db_file)
            revision, counts = _sqlite_facts(db_file)
        else:
            _run_pg(["pg_dump", "--format=custom", "--no-owner", f"--file={db_file}"], settings)
            revision, counts = _live_facts()
        staged = [(db_file, db_arc)]
        for src, arc in collect_files(settings, cfg):        # 先複製一份，之後只讀副本
            dest = stage / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
            except FileNotFoundError:                          # 收件夾裡的文件剛好被導入移走
                continue
            staged.append((dest, arc))
        manifest = {"format": FORMAT, "app": "crudelab", "version": __version__, "label": label,
                    "created_at": now.astimezone(UTC).isoformat(timespec="seconds"), "database": kind,
                    "revision": revision, "counts": counts, "secrets_included": False,
                    "files": [{"path": arc, "size": p.stat().st_size, "sha256": _sha256(p)} for p, arc in staged]}
        partial = target / (name + ".partial")
        try:
            with tarfile.open(partial, "w:gz") as tar:
                data = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mtime, info.mode = len(data), int(now.timestamp()), 0o600
                tar.addfile(info, io.BytesIO(data))
                for path, arc in staged:
                    tar.add(path, arcname=arc, recursive=False)
            _private(partial, 0o600)
            os.replace(partial, final)
        finally:
            partial.unlink(missing_ok=True)
    try:
        verify(final)
    except BackupError:
        final.unlink(missing_ok=True)
        raise
    return describe(final)


# ------------------------------ 讀取與校驗 ------------------------------

def read_manifest(path: Path) -> dict:
    try:
        with tarfile.open(path, "r:gz") as tar:
            first = tar.next()
            if first is None or first.name != "manifest.json" or not first.isfile():
                raise BackupError(f"{path.name} 不是 crudelab 的備份（第一個文件不是 manifest.json）")
            manifest = json.load(tar.extractfile(first))
    except (tarfile.TarError, OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError):
        raise BackupError(f"{path.name} 無法讀取：文件損壞或不是備份文件") from None
    if manifest.get("app") != "crudelab" or not isinstance(manifest.get("files"), list):
        raise BackupError(f"{path.name} 的清單不完整")
    if manifest.get("format", 0) > FORMAT:
        raise BackupError(f"{path.name} 是更新版本的程序做的備份（格式 {manifest['format']}），請先升級程序")
    return manifest


def _safe(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not name.startswith(ROOTS):
        raise BackupError(f"備份裡有不安全的路徑：{name}")
    return p


def _members(path: Path):
    """逐個（成員, 文件對象）遍歷，跳過 manifest。每個成員都檢查路徑和類型。"""
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            if member.name == "manifest.json":
                continue
            _safe(member.name)
            if not member.isfile():
                raise BackupError(f"備份裡有非普通文件：{member.name}")
            yield member, tar.extractfile(member)


def verify(path: Path) -> dict:
    """逐個文件核對大小和 SHA-256；SQLite 快照再做一次 integrity_check。不通過就拋 BackupError。"""
    manifest = read_manifest(path)
    expected = {f["path"]: f for f in manifest["files"]}
    seen: set[str] = set()
    try:
        with tempfile.TemporaryDirectory(prefix=".crudelab-verify-", dir=path.parent) as tmp:
            for member, stream in _members(path):
                entry = expected.get(member.name)
                if entry is None:
                    raise BackupError(f"備份裡有清單之外的文件：{member.name}")
                h, size = hashlib.sha256(), 0
                keep = Path(tmp) / "db.sqlite" if member.name == DB_SQLITE else None
                sink = keep.open("wb") if keep else None
                try:
                    while chunk := stream.read(CHUNK):
                        h.update(chunk)
                        size += len(chunk)
                        if sink:
                            sink.write(chunk)
                finally:
                    if sink:
                        sink.close()
                if size != entry["size"] or h.hexdigest() != entry["sha256"]:
                    raise BackupError(f"{member.name} 的校驗碼不符：備份文件已損壞")
                if keep:
                    _check_sqlite(keep)
                seen.add(member.name)
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise BackupError(f"{path.name} 讀到一半出錯：文件不完整或已損壞") from None
    missing = set(expected) - seen
    if missing:
        raise BackupError(f"備份裡缺少 {len(missing)} 個清單上的文件，例如 {sorted(missing)[0]}")
    if not any(name.startswith("database/") for name in seen):
        raise BackupError("備份裡沒有數據庫")
    return {"ok": True, "name": path.name, "files": len(seen)}


def describe(path: Path) -> dict:
    m = NAME.match(path.name)
    stamp = datetime.strptime(m["stamp"], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    out = {"name": path.name, "label": m["label"], "label_zh": LABELS[m["label"]],
           "created_at": stamp.isoformat(), "size": path.stat().st_size, "error": None}
    try:
        manifest = read_manifest(path)
        counts = manifest.get("counts", {})
        out.update(version=manifest.get("version"), database=manifest.get("database"),
                   revision=manifest.get("revision"), files=len(manifest["files"]),
                   observations=counts.get("observation"), raw_files=sum(
                       f["path"].startswith("data/raw/") for f in manifest["files"]))
    except BackupError as exc:
        out["error"] = str(exc)
    return out


def list_backups(settings: Settings, cfg: Config) -> list[dict]:
    target = directory(settings, cfg)
    if not target.exists():
        return []
    found = [describe(p) for p in target.iterdir() if p.is_file() and NAME.match(p.name)]
    return sorted(found, key=lambda b: b["name"][9:24], reverse=True)


def resolve(settings: Settings, cfg: Config, name: str) -> Path:
    """後台按名字找備份：名字必須符合命名規則，不能帶路徑。"""
    if not NAME.match(name):
        raise BackupError("備份名稱不正確")
    path = directory(settings, cfg) / name
    if not path.is_file():
        raise BackupError(f"沒有找到備份 {name}")
    return path


# ------------------------------ 清理 ------------------------------

def prune(settings: Settings, cfg: Config) -> list[str]:
    """按保留策略刪掉多餘的定時備份。手動和恢復前的備份不動；最新一份定時備份永遠保留。"""
    rule = cfg.backup
    scheduled = [b for b in list_backups(settings, cfg) if b["label"] == "scheduled"]   # 新 → 舊
    keep: set[str] = set()
    days: set[date] = set()
    weeks: set[tuple[int, int]] = set()
    months: set[tuple[int, int]] = set()
    for b in scheduled:
        d = datetime.fromisoformat(b["created_at"]).date()
        week, month = tuple(d.isocalendar())[:2], (d.year, d.month)
        if d not in days and len(days) < rule.keep_daily:
            days.add(d)
            keep.add(b["name"])
        if week not in weeks and len(weeks) < rule.keep_weekly:
            weeks.add(week)
            keep.add(b["name"])
        if month not in months and len(months) < rule.keep_monthly:
            months.add(month)
            keep.add(b["name"])
    if scheduled:
        keep.add(scheduled[0]["name"])
    target = directory(settings, cfg)
    removed = []
    for b in scheduled:
        if b["name"] not in keep:
            (target / b["name"]).unlink(missing_ok=True)
            removed.append(b["name"])
    return removed


# ------------------------------ 恢復 ------------------------------

def _known_revisions() -> set[str]:
    from alembic.script import ScriptDirectory
    return {r.revision for r in ScriptDirectory.from_config(db.alembic_config()).walk_revisions()}


def _service_running() -> bool:
    from .models import SchedulerHeartbeat, utcnow
    from .scheduler import HEARTBEAT_FRESH
    try:
        with db.session_scope() as session:
            beat = session.get(SchedulerHeartbeat, 1)
            return beat is not None and utcnow() - beat.at <= HEARTBEAT_FRESH
    except Exception:            # 現在的數據庫壞了或不存在：正是需要恢復的時候，不擋
        return False


def restore(settings: Settings, cfg: Config, path: Path, *, with_config: bool = False, force: bool = False,
            actor: str = "local") -> dict:
    manifest = read_manifest(path)
    verify(path)
    kind = database_kind(settings)
    if manifest["database"] != kind:
        raise BackupError(f"這份備份是 {manifest['database']} 數據庫的，現在用的是 {kind}，不能直接恢復")
    if manifest.get("revision") and manifest["revision"] not in _known_revisions():
        raise BackupError("這份備份來自更新版本的程序（表結構版本不認識），請先把程序升級到同一版本再恢復")
    current_exists = kind != "sqlite" or _sqlite_path(settings).exists()
    if current_exists and not force and _service_running():
        raise BackupError("服務還開著（調度器一分鐘內還在運行）。請先關掉 crudelab serve / crudelab scheduler 再恢復，"
                          "確定已經關掉可以加 --force")
    safety = None
    if current_exists:
        try:
            safety = create(settings, cfg, "pre-restore")["name"]
        except BackupError as exc:
            if not force:
                raise BackupError(f"恢復前先備份現有數據，但失敗了：{exc}。如果現有數據已經損壞、確定不需要保留，"
                                  "可以加 --force 跳過") from None

    with tempfile.TemporaryDirectory(prefix=".crudelab-restore-", dir=settings.data_dir) as tmp:
        root = Path(tmp)
        for member, stream in _members(path):
            dest = root / member.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                shutil.copyfileobj(stream, f, CHUNK)
        if db._engine is not None:
            db._engine.dispose()
        if kind == "sqlite":
            target = _sqlite_path(settings)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(target.name + ".restoring")
            shutil.copyfile(root / DB_SQLITE, staging)
            for suffix in ("-wal", "-shm", "-journal"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            os.replace(staging, target)
        else:
            others = _other_pg_connections(settings)
            if others:
                raise BackupError(f"還有 {len(others)} 個連接開着這個數據庫：{'、'.join(others[:5])}。"
                                  "恢復要刪表重建，會一直等它們。請先關掉 crudelab serve / scheduler "
                                  "和其他連着它的客戶端（例如數據庫管理工具）再恢復；數據沒有改動")
            dbname = make_url(settings.database_url).database
            _run_pg(["pg_restore", "--clean", "--if-exists", "--no-owner", "--single-transaction",
                     f"--dbname={dbname}", str(root / DB_PG)], settings)
        restored_files = 0
        for entry in manifest["files"]:
            arc = entry["path"]
            if arc.startswith("data/"):
                dest = settings.data_dir / PurePosixPath(arc).relative_to("data")
            elif arc.startswith("config/") and with_config:
                dest = settings.config_dir / PurePosixPath(arc).relative_to("config")
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / arc, dest)
            restored_files += 1

    db.init_engine(settings)                 # 升級表結構到當前版本
    from .models import AuditLog
    detail = {"backup": path.name, "created_at": manifest["created_at"], "revision": manifest.get("revision"),
              "safety_backup": safety, "files": restored_files, "with_config": with_config}
    with db.session_scope() as session:
        session.add(AuditLog(actor=actor, action="restore", entity="backup", entity_id=path.name, detail=detail))
    return detail
