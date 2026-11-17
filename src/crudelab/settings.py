"""運行配置：全部來自環境變量，方便本機 / 雲端切換。"""
from __future__ import annotations

import os
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
