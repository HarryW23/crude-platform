"""運行配置：全部來自環境變量，方便本機 / 雲端切換。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    data_dir: Path
    database_url: str

    @property
    def raw_dir(self) -> Path:
        """原始文件存放處：導入的文件原樣保留，任何數字都能回到原文件核對。"""
        return self.data_dir / "raw"


def get_settings() -> Settings:
    config_dir = Path(os.environ.get("CRUDELAB_CONFIG_DIR", PROJECT_ROOT / "config"))
    data_dir = Path(os.environ.get("CRUDELAB_DATA_DIR", PROJECT_ROOT / "data"))
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'crudelab.db'}")
    return Settings(config_dir=config_dir, data_dir=data_dir, database_url=database_url)
