"""運行配置：全部來自環境變量，方便本機 / 雲端切換。

本機設定文件（預設 ~/.crudelab/local.env，可用 CRUDELAB_LOCAL_FILE 改）：
    放「這台機器自己的」非密鑰設定，例如用哪家大模型、備份放哪。它在項目文件夾之外，
    所以 git pull 更新代碼、甚至刪掉重新下載項目，都不會影響它；也不會和倉庫裡的配置文件衝突。
    每行 NAME=value，只接受 LOCAL_SETTINGS 裡列出的名字；真正的環境變量優先於它。
    密鑰不放這裡，放 secrets.env（crudelab secret set）。用 crudelab local set / unset / list 管理。
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import credentials

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    data_dir: Path
    database_url: str = field(repr=False)        # 可能含密碼：不進 repr，打印時用 safe_database_url
    front_url: str = "http://127.0.0.1:8000"      # 兩個站點互相連結時用的對外地址
    admin_url: str = "http://127.0.0.1:8001"
    auth_enabled: bool = False        # 登錄功能的總開關。關閉時所有人都以 local 身份操作，只適合本機
    session_hours: int = 12           # 登錄後多久要重新登錄
    cookie_secure: bool = False       # 用 https 對外提供服務時必須設為 True

    @property
    def safe_database_url(self) -> str:
        """可以打印的連接串：密碼已遮。"""
        return credentials.redact(self.database_url)

    @property
    def raw_dir(self) -> Path:
        """原始文件存放處：導入的文件原樣保留，任何數字都能回到原文件核對。"""
        return self.data_dir / "raw"


LOCAL_SETTINGS = {
    "CRUDELAB_LLM_PROVIDER": "大模型供應商：none / deepseek / openai / anthropic / compatible",
    "CRUDELAB_LLM_MODEL": "大模型型號（不設就用預設）",
    "CRUDELAB_LLM_BASE_URL": "compatible 的接口地址（https）",
    "CRUDELAB_BACKUP_DIR": "備份放哪（另一塊硬盤、NAS、雲同步文件夾）",
    "CRUDELAB_DATA_DIR": "數據目錄（預設是項目裡的 data/）",
    "CRUDELAB_AUTH": "登錄功能：on / off",
    "CRUDELAB_COOKIE_SECURE": "用 https 對外服務時設 on",
    "CRUDELAB_SESSION_HOURS": "登錄後多久要重新登錄",
    "CRUDELAB_FRONT_URL": "前台的對外地址",
    "CRUDELAB_ADMIN_URL": "後台的對外地址",
}
_LOCAL_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class LocalSettingsError(RuntimeError):
    """本機設定文件寫不進去，或寫了不允許的名字。"""


def local_settings_path() -> Path:
    custom = os.environ.get("CRUDELAB_LOCAL_FILE", "").strip()
    return Path(custom).expanduser() if custom else Path.home() / ".crudelab" / "local.env"


def read_local_settings() -> dict[str, str]:
    path = local_settings_path()
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LOCAL_LINE.match(line.strip())
        if match and match.group(1) in LOCAL_SETTINGS and match.group(2).strip():
            out[match.group(1)] = match.group(2).strip()
    return out


def apply_local_settings() -> dict[str, str]:
    """把本機設定放進環境變量（已經設了的環境變量不覆蓋）。回傳實際生效的部分。程序入口調用一次。"""
    applied = {}
    for name, value in read_local_settings().items():
        if not os.environ.get(name):
            os.environ[name] = value
            applied[name] = value
    return applied


def set_local_setting(name: str, value: str | None) -> None:
    """設定或刪除（value 為 None）一項本機設定。原子寫入。"""
    if name not in LOCAL_SETTINGS:
        raise LocalSettingsError(f"不認識的設定名 {name}。可用的：{'、'.join(LOCAL_SETTINGS)}")
    if value is not None and (not value.strip() or "\n" in value or "\r" in value):
        raise LocalSettingsError("值不能為空，也不能包含換行")
    values = read_local_settings()
    if value is None:
        values.pop(name, None)
    else:
        values[name] = value.strip()
    path = local_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".local-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# crudelab 本機設定（不含密鑰）。用 crudelab local set / unset 管理，也可以直接編輯。\n")
            fh.writelines(f"{k}={v}\n" for k, v in sorted(values.items()))
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


def get_settings() -> Settings:
    config_dir = Path(os.environ.get("CRUDELAB_CONFIG_DIR", PROJECT_ROOT / "config"))
    data_dir = Path(os.environ.get("CRUDELAB_DATA_DIR", PROJECT_ROOT / "data"))
    database_url = credentials.get("DATABASE_URL") or f"sqlite:///{data_dir / 'crudelab.db'}"
    return Settings(config_dir=config_dir, data_dir=data_dir, database_url=database_url,
                    front_url=os.environ.get("CRUDELAB_FRONT_URL", "http://127.0.0.1:8000").rstrip("/"),
                    admin_url=os.environ.get("CRUDELAB_ADMIN_URL", "http://127.0.0.1:8001").rstrip("/"),
                    auth_enabled=_flag("CRUDELAB_AUTH"),
                    session_hours=int(os.environ.get("CRUDELAB_SESSION_HOURS", "12")),
                    cookie_secure=_flag("CRUDELAB_COOKIE_SECURE"))
