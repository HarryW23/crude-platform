"""接口層的公共依賴：應用狀態、數據庫會話、操作者、指標引擎。"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .. import db
from ..config import Config, ConfigError, load_config
from ..indicators import IndicatorEngine
from ..master import sync_to_db
from ..scenarios import ScenarioError
from ..scenarios import load as load_scenario
from ..settings import Settings

Site = Literal["front", "admin"]
log = logging.getLogger("crudelab.api")
CONFIG_CHECK_INTERVAL = 2.0     # 最多每隔幾秒檢查一次配置文件有沒有變
SYNC_ATTEMPTS = 3               # 幾個進程同時同步同一份新配置會撞上唯一鍵：重試時對方已經寫好了


def _fingerprint(config_dir: Path) -> tuple:
    return tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in config_dir.rglob("*.yaml")))


class AppState:
    """一個站點進程的狀態。

    前台和後台可能是兩個獨立的進程，所以配置不能靠「後台通知前台」來同步：
    每個進程自己盯著 config/ 下的文件，發現改動就重新載入。新配置有錯時繼續用舊的，並把錯誤記在 config_error。
    """

    def __init__(self, settings: Settings, site: Site):
        self.settings, self.site = settings, site
        self.config_error: str | None = None
        self._fingerprint = _fingerprint(settings.config_dir)
        self._checked_at = time.monotonic()
        self._cfg = self._load()            # 啟動時配置有錯直接拋出，不帶病啟動

    def _load(self) -> Config:
        cfg = load_config(self.settings.config_dir)
        for attempt in range(SYNC_ATTEMPTS):
            try:
                with db.session_scope() as session:
                    self._last_sync = sync_to_db(session, cfg)
                break
            except (IntegrityError, OperationalError):
                # 前台、後台（或多個副本）都盯着同一份配置，改動後會同時同步：先寫的贏，後寫的撞上唯一鍵
                # 或鎖等待超時。稍等再試一次，那時對方的行已經在了，只剩更新
                if attempt == SYNC_ATTEMPTS - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))
        return cfg

    def reload(self) -> dict:
        """立即重新載入。成功回傳同步統計；配置有錯拋 ConfigError 並保持原配置。
        數據庫出錯（同步沒寫進去）也保持原配置，並且不記下這次的文件狀態，下次檢查會再試。"""
        fingerprint = _fingerprint(self.settings.config_dir)
        self._checked_at = time.monotonic()
        try:
            self._cfg = self._load()
        except ConfigError as exc:
            self._fingerprint = fingerprint             # 配置本身有錯：等文件再改才重試
            self.config_error = str(exc)
            raise
        self._fingerprint = fingerprint
        self.config_error = None
        return self._last_sync

    @property
    def cfg(self) -> Config:
        now = time.monotonic()
        if now - self._checked_at >= CONFIG_CHECK_INTERVAL:
            self._checked_at = now
            if _fingerprint(self.settings.config_dir) != self._fingerprint:
                try:
                    self.reload()
                except ConfigError:
                    pass                    # 已記在 config_error，繼續用舊配置
                except (IntegrityError, OperationalError):
                    log.exception("配置已改動，但同步到數據庫失敗；暫用舊配置，下次檢查再試")
        return self._cfg


def get_state(request: Request) -> AppState:
    return request.app.state.crudelab


def get_db() -> Iterator[Session]:
    with db.session_scope() as session:
        yield session


def get_actor(request: Request) -> str:
    """當前操作者：登錄功能開啟時是登錄的用戶名，關閉時固定為 local。寫入各種記錄的「誰做的」都來自這裡。"""
    user = getattr(request.state, "user", None)
    return user.username if user is not None else "local"


def get_as_of(as_of: datetime | None = Query(None, description="還原這個時點（UTC）當時的數據")) -> datetime | None:
    return as_of.replace(tzinfo=None) if as_of else None


def get_engine(session: Session = Depends(get_db), state: AppState = Depends(get_state),
               as_of: datetime | None = Depends(get_as_of),
               scenario: str | None = Query(None, description="套用這個已保存的情境")) -> IndicatorEngine:
    """按請求參數建引擎：as_of 還原歷史時點，scenario 套用已保存的情境。兩者都不給就是當前的基準視圖。"""
    cfg = state.cfg
    try:
        loaded = load_scenario(session, cfg, scenario, as_of) if scenario else None
    except ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc
    return IndicatorEngine(session, cfg, as_of=as_of, scenario=loaded)


def get_base_engine(session: Session = Depends(get_db), state: AppState = Depends(get_state),
                    as_of: datetime | None = Depends(get_as_of)) -> IndicatorEngine:
    """不套情境的基準引擎。用於需要「基準 vs 情境」對照的接口。"""
    return IndicatorEngine(session, state.cfg, as_of=as_of)
