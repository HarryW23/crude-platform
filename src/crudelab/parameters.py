"""參數服務：讀取當前值、新增版本、標記核對。後台接口和其他入口（命令行、之後的大模型工具）共用。"""
from __future__ import annotations

import math
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Parameter, ParameterVersion
from .schema import EPOCH


class ParameterError(ValueError):
    pass


def effective_versions(session: Session, key: str) -> list[ParameterVersion]:
    """目前有效的版本，按生效日期排序（同一生效日期只留最新一條）。"""
    latest: dict[date, ParameterVersion] = {}
    for v in session.scalars(select(ParameterVersion).where(ParameterVersion.key == key)
                             .order_by(ParameterVersion.id)):
        latest[v.valid_from] = v
    return [latest[d] for d in sorted(latest)]


def set_value(session: Session, key: str, value: float, valid_from: date | None = None,
              note: str | None = None, actor: str = "local") -> ParameterVersion:
    """新增一個版本。valid_from 不給 = 更正目前生效的那個版本（還沒有任何版本時 = 自始生效）。

    由人設定的值視為已核對。
    """
    param = session.get(Parameter, key)
    if param is None or not param.active:
        raise ParameterError(f"沒有名為 {key} 的參數")
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ParameterError("參數值必須是有效數字")
    if valid_from is None:
        today = date.today()
        current = [v for v in effective_versions(session, key) if v.valid_from <= today]
        valid_from = current[-1].valid_from if current else EPOCH
    version = ParameterVersion(key=key, valid_from=valid_from, value=float(value), note=note, created_by=actor)
    session.add(version)
    param.verified = True
    session.flush()
    return version


def set_verified(session: Session, key: str, verified: bool) -> None:
    param = session.get(Parameter, key)
    if param is None:
        raise ParameterError(f"沒有名為 {key} 的參數")
    param.verified = verified
