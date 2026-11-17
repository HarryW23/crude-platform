"""後台頁面上的開關：定時任務的總開關和每個任務的開關、通知的總開關和每個渠道的開關。

配置文件裡的 enabled 是預設值；在後台點過開關之後，以後台為準（和參數的做法一致）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config
from .models import AuditLog, Switch, utcnow

SCHEDULER, NOTIFY = "scheduler", "notify"


class SwitchError(ValueError):
    pass


def job_key(code: str) -> str:
    return f"job:{code}"


def channel_key(code: str) -> str:
    return f"channel:{code}"


def defaults(cfg: Config) -> dict[str, bool]:
    """配置文件給出的預設狀態。這也是全部合法的開關。"""
    out = {SCHEDULER: True, NOTIFY: True}
    out.update({job_key(j.code): j.enabled for j in cfg.schedule.jobs})
    out.update({channel_key(c.code): c.enabled for c in cfg.notifications.channels})
    return out


def load(session: Session, cfg: Config) -> dict[str, bool]:
    """每個開關目前的實際狀態。"""
    state = defaults(cfg)
    for row in session.scalars(select(Switch)):
        if row.key in state:
            state[row.key] = row.enabled
    return state


def overridden(session: Session) -> set[str]:
    return set(session.scalars(select(Switch.key)))


def set_switch(session: Session, cfg: Config, key: str, enabled: bool, actor: str = "local") -> None:
    if key not in defaults(cfg):
        raise SwitchError(f"沒有名為 {key} 的開關")
    row = session.get(Switch, key)
    if row is None:
        session.add(Switch(key=key, enabled=enabled, updated_by=actor))
    else:
        row.enabled, row.updated_by, row.updated_at = enabled, actor, utcnow()
    session.add(AuditLog(actor=actor, action="switch", entity="switch", entity_id=key, detail={"enabled": enabled}))
