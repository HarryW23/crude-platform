"""情境服務：保存、讀取人為調整。

情境把「數據說什麼」和「我認為會怎樣」分開：數據永遠原樣保存，判斷作為調整疊加在上面，
每條調整都要寫依據。同一份數據可以有多個情境並存（例如「我的判斷」「霍爾木茲中斷兩周」）。
第四期的地緣事件會落成這裡的調整。
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config
from .indicators import RangedShift, Scenario
from .models import Adjustment, ScenarioRow, utcnow

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ScenarioError(ValueError):
    pass


def create(session: Session, code: str, name_zh: str, description: str | None = None,
           actor: str = "local") -> ScenarioRow:
    if not _CODE.match(code):
        raise ScenarioError("情境代碼只能用大寫字母、數字、下劃線，並以字母開頭")
    if session.get(ScenarioRow, code) is not None:
        raise ScenarioError(f"情境 {code} 已存在")
    row = ScenarioRow(code=code, name_zh=name_zh.strip() or code, description=description, created_by=actor)
    session.add(row)
    session.flush()
    return row


def _require(session: Session, code: str) -> ScenarioRow:
    row = session.get(ScenarioRow, code)
    if row is None or row.archived_at is not None:
        raise ScenarioError(f"沒有名為 {code} 的情境")
    return row


def archive(session: Session, code: str) -> None:
    _require(session, code).archived_at = utcnow()


def add_adjustment(session: Session, cfg: Config, scenario_code: str, target: str, date_from: date,
                   date_to: date | None, delta: float, note: str | None, actor: str = "local") -> Adjustment:
    _require(session, scenario_code)
    if target not in cfg.series and target not in cfg.indicators:
        raise ScenarioError(f"{target} 既不是序列也不是指標")
    if not isinstance(delta, (int, float)) or not math.isfinite(delta) or delta == 0:
        raise ScenarioError("調整量必須是非零的有效數字")
    if date_to is not None and date_to < date_from:
        raise ScenarioError("結束日期不能早於開始日期")
    row = Adjustment(scenario_code=scenario_code, target=target, date_from=date_from, date_to=date_to,
                     delta=float(delta), note=note, created_by=actor)
    session.add(row)
    session.flush()
    return row


def remove_adjustment(session: Session, adjustment_id: int) -> None:
    row = session.get(Adjustment, adjustment_id)
    if row is None or row.removed_at is not None:
        raise ScenarioError(f"沒有編號為 {adjustment_id} 的調整")
    row.removed_at = utcnow()


def adjustments(session: Session, scenario_code: str, as_of: datetime | None = None) -> list[Adjustment]:
    """情境裡目前有效的調整。給了 as_of 就還原那個時點的狀態。"""
    stmt = select(Adjustment).where(Adjustment.scenario_code == scenario_code).order_by(Adjustment.id)
    rows = list(session.scalars(stmt))
    if as_of is None:
        return [r for r in rows if r.removed_at is None]
    return [r for r in rows if r.created_at <= as_of and (r.removed_at is None or r.removed_at > as_of)]


def load(session: Session, cfg: Config, scenario_code: str, as_of: datetime | None = None) -> Scenario:
    """把保存的情境變成引擎能用的 Scenario。目標已從配置裡移除的調整會被跳過。"""
    _require(session, scenario_code)
    ranged = tuple(RangedShift(a.target, a.date_from, a.date_to, a.delta)
                   for a in adjustments(session, scenario_code, as_of)
                   if a.target in cfg.series or a.target in cfg.indicators)
    return Scenario(ranged=ranged)
