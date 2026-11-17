"""地緣事件服務：事件的增改查，以及把事件預案套用成情境。

事件本身只是記錄（發生了什麼、方向判斷）。它對供需的量化影響存在一個情境裡（scenario_code），
所以一個事件的影響可以在供需平衡表裡看、在推演裡當一個分支用，也可以隨時改。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import scenarios
from .config import Config
from .models import AuditLog, Event, ScenarioRow, utcnow

STATUSES = ("rumor", "confirmed", "ongoing", "resolved", "dismissed")
CATEGORIES = ("conflict", "chokepoint", "sanction", "opec_policy", "outage", "policy", "demand", "other")
EDITABLE = ("title", "category", "location_code", "status", "bias", "started_on", "ended_on", "summary", "source")


class EventError(ValueError):
    pass


def _validate(cfg: Config, fields: dict) -> None:
    if "title" in fields and not str(fields["title"] or "").strip():
        raise EventError("事件標題不能為空")
    if "category" in fields and fields["category"] not in CATEGORIES:
        raise EventError(f"事件類別必須是：{'、'.join(CATEGORIES)}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise EventError(f"事件狀態必須是：{'、'.join(STATUSES)}")
    if "bias" in fields and fields["bias"] not in (-2, -1, 0, 1, 2):
        raise EventError("方向判斷必須是 −2 到 +2 的整數")
    if fields.get("location_code") and fields["location_code"] not in cfg.locations:
        raise EventError(f"地區 {fields['location_code']} 不在地區字典裡")
    if fields.get("started_on") and fields.get("ended_on") and fields["ended_on"] < fields["started_on"]:
        raise EventError("結束日期不能早於開始日期")


def create(session: Session, cfg: Config, *, title: str, category: str = "other", status: str = "confirmed",
           bias: int = 0, location_code: str | None = None, started_on: date | None = None,
           ended_on: date | None = None, summary: str | None = None, source: str | None = None,
           origin: str = "manual", draft: bool = False, actor: str = "local") -> Event:
    fields = dict(title=title, category=category, status=status, bias=bias, location_code=location_code or None,
                  started_on=started_on, ended_on=ended_on, summary=summary, source=source)
    _validate(cfg, fields)
    row = Event(**{**fields, "title": title.strip()[:200]}, origin=origin, draft=draft, created_by=actor)
    session.add(row)
    session.flush()
    return row


def require(session: Session, event_id: int) -> Event:
    row = session.get(Event, event_id)
    if row is None:
        raise EventError(f"沒有編號為 {event_id} 的事件")
    return row


def update(session: Session, cfg: Config, event_id: int, changes: dict, actor: str = "local") -> Event:
    """修改事件。changes 只能含 EDITABLE 裡的字段，另可帶 draft=False 表示確認草稿。改動記入操作日誌。"""
    row = require(session, event_id)
    unknown = set(changes) - set(EDITABLE) - {"draft"}
    if unknown:
        raise EventError(f"不能修改的字段：{', '.join(sorted(unknown))}")
    merged = {k: changes.get(k, getattr(row, k)) for k in EDITABLE}
    _validate(cfg, merged)
    before = {k: str(getattr(row, k)) for k in changes if str(getattr(row, k)) != str(changes[k])}
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = utcnow()
    if before:
        session.add(AuditLog(actor=actor, action="event_update", entity="event", entity_id=str(row.id),
                             detail={"before": before, "after": {k: str(changes[k]) for k in before}}))
    return row


def list_events(session: Session, include_closed: bool = True) -> list[Event]:
    stmt = select(Event).order_by(Event.draft.desc(), Event.id.desc())
    rows = list(session.scalars(stmt))
    return rows if include_closed else [r for r in rows if r.status not in ("resolved", "dismissed")]


def apply_playbook(session: Session, cfg: Config, event_id: int, playbook_code: str, start: date,
                   impacts: list[dict] | None = None, actor: str = "local") -> ScenarioRow:
    """把預案套用到事件上：生成（或沿用）這個事件的情境，並加入調整。

    impacts 不給就用預案裡的示意值；給了就逐項使用（每項：target, delta, duration_days, note）。
    """
    event = require(session, event_id)
    if event.draft:
        raise EventError("草稿事件要先確認，才能套用預案")
    if playbook_code not in cfg.playbooks:
        raise EventError(f"沒有名為 {playbook_code} 的事件預案")
    playbook = cfg.playbooks[playbook_code]
    items = impacts if impacts is not None else [i.model_dump() for i in playbook.impacts]
    if not items:
        raise EventError("至少要有一項影響")
    try:
        if event.scenario_code is None:
            scenario = scenarios.create(session, f"EVT_{event.id}", f"事件：{event.title}"[:128],
                                        f"由事件 #{event.id} 套用預案「{playbook.name_zh}」生成", actor)
            event.scenario_code = scenario.code
        for item in items:
            days = item.get("duration_days")
            scenarios.add_adjustment(
                session, cfg, event.scenario_code, item["target"], start,
                start + timedelta(days=days - 1) if days else None, item["delta"],
                item.get("note") or f"預案「{playbook.name_zh}」", actor)
    except scenarios.ScenarioError as exc:
        raise EventError(str(exc)) from exc
    event.updated_at = utcnow()
    return session.get(ScenarioRow, event.scenario_code)
