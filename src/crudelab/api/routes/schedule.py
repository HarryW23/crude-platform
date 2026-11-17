"""定時任務：查看安排和執行記錄、手動立即運行。只在後台。"""
from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import jobs, notifications, scheduler, switches
from ...models import AuditLog
from ..deps import AppState, get_actor, get_db, get_state

admin_router = APIRouter(tags=["schedule"])


class SwitchBody(BaseModel):
    enabled: bool


@admin_router.get("/schedule")
def schedule_status(state: AppState = Depends(get_state)):
    """調度器是否在運行，以及每個任務的時間安排、下次運行時間和最近一次結果。時間按 schedule.yaml 的時區顯示。"""
    return scheduler.status(state.cfg)


@admin_router.get("/schedule/runs")
def runs(limit: int = 100, job: str | None = None, session: Session = Depends(get_db),
         state: AppState = Depends(get_state)):
    tz = ZoneInfo(state.cfg.schedule.timezone)
    return [scheduler.run_view(r, tz) for r in jobs.recent_runs(session, min(limit, 500), job)]


@admin_router.post("/schedule/{code}/run")
def run_now(code: str, session: Session = Depends(get_db), state: AppState = Depends(get_state),
            actor: str = Depends(get_actor)):
    """立即執行一個任務，不管它是否啟用、是否到點。等它執行完才回應。"""
    try:
        result = jobs.run_now(state.cfg, state.settings, code)
    except jobs.JobError as exc:
        raise HTTPException(404 if "沒有名為" in str(exc) else 409, str(exc)) from exc
    session.add(AuditLog(actor=actor, action="job_run", entity="job", entity_id=code,
                         detail={"status": result["status"]}))
    return result


@admin_router.put("/switches/{key:path}")
def set_switch(key: str, body: SwitchBody, session: Session = Depends(get_db),
               state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    """打開或關閉一個開關。key：scheduler（定時任務總開關）、job:任務代碼、notify（通知總開關）、channel:渠道代碼。"""
    try:
        switches.set_switch(session, state.cfg, key, body.enabled, actor)
    except switches.SwitchError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"key": key, "enabled": body.enabled}


@admin_router.get("/notifications")
def notification_status(session: Session = Depends(get_db), state: AppState = Depends(get_state)):
    """通知的規則、每個渠道的狀態（不含地址和密碼）、最近發出的通知。"""
    cfg = state.cfg
    current, changed = switches.load(session, cfg), switches.overridden(session)
    tz = ZoneInfo(cfg.schedule.timezone)

    def local(value):
        return value.replace(tzinfo=UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M")

    return {"enabled": current[switches.NOTIFY], "on_failure": cfg.notifications.on_failure,
            "on_recovery": cfg.notifications.on_recovery, "repeat_hours": cfg.notifications.repeat_hours,
            "channels": [{"code": c.code, "name_zh": c.name_zh, "type": c.type,
                          "type_name": notifications.TYPE_NAMES[c.type], "switch": switches.channel_key(c.code),
                          "enabled": current[switches.channel_key(c.code)],
                          "overridden": switches.channel_key(c.code) in changed,
                          "configured": notifications.is_configured(c),
                          "credential": notifications.credential_name(c),
                          "target": "、".join(c.mail_to) if c.type == "email" else None}
                         for c in cfg.notifications.channels],
            "recent": [{"at": local(n.created_at), "job": n.job_code, "event": n.event, "title": n.title,
                        "body": n.body, "results": n.results} for n in notifications.recent(session)]}


@admin_router.post("/notifications/{code}/test")
def test_channel(code: str, state: AppState = Depends(get_state), actor: str = Depends(get_actor)):
    """向一個渠道發一條測試消息，不管開關開沒開。用來確認地址和密碼配對了。"""
    if code not in {c.code for c in state.cfg.notifications.channels}:
        raise HTTPException(404, f"沒有名為 {code} 的通知渠道")
    results = notifications.send(state.cfg, "測試通知", f"這是 {actor} 從後台發出的測試消息。收到說明這個渠道可以用。",
                                 event="test", only=code, ignore_switches=True)
    return {"code": code, "result": results.get(code, "沒有發送")}
