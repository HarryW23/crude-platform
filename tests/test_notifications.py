"""開關與失敗通知。"""
import json
import smtplib
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from crudelab import db, jobs, notifications, scheduler, switches
from crudelab.connectors.base import ConnectorError
from crudelab.models import Notification, Report
from crudelab.schema import ChannelDef

HOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=canary-hook-11aa22bb33"      # secret-scan: ok
T0 = datetime(2026, 11, 17, 10, 31)          # 周二 上海 18:31


@pytest.fixture()
def ready(session, settings, cfg, monkeypatch):
    monkeypatch.setattr(jobs, "SETTLE_SECONDS", 0)
    return settings


@pytest.fixture()
def outbox(monkeypatch):
    """接住所有發往群機器人的請求。"""
    sent = []

    def post(url, json=None, timeout=None):
        sent.append({"url": url, "json": json})
        return httpx.Response(200, json={"errcode": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return sent


def flip(key, enabled, cfg):
    with db.session_scope() as s:
        switches.set_switch(s, cfg, key, enabled, "tester")


# ------------------------------ 開關 ------------------------------

def test_each_job_has_its_own_switch_and_there_is_a_master(ready, cfg, session):
    with db.session_scope() as s:
        state = switches.load(s, cfg)
    assert state["scheduler"] and state["job:REPORT_DAILY"] and not state["job:REPORT_MONTHLY"]   # 預設來自配置

    flip("job:REPORT_DAILY", False, cfg)
    ran = [r["job"] for r in scheduler.tick(cfg, ready, T0)]
    assert "REPORT_DAILY" not in ran and "IMPORT_INBOX" in ran               # 只關了這一個
    assert jobs.run_now(cfg, ready, "REPORT_DAILY")["status"] == "ok"        # 關掉的任務仍可手動運行

    flip("scheduler", False, cfg)
    assert scheduler.tick(cfg, ready, T0 + timedelta(minutes=10)) == []      # 總開關：全部不跑
    status = scheduler.status(cfg, T0 + timedelta(minutes=10))
    assert status["running"] and status["enabled"] is False and all(j["next_run"] is None for j in status["jobs"])

    flip("scheduler", True, cfg)
    flip("job:REPORT_MONTHLY", True, cfg)                                    # 配置裡停用的，也能在後台打開
    monthly = next(j for j in scheduler.status(cfg, T0)["jobs"] if j["code"] == "REPORT_MONTHLY")
    assert monthly["enabled"] and monthly["overridden"] and monthly["default_enabled"] is False
    assert monthly["next_run"] == "2026-12-01T01:00:00Z"                  # 北京 09:00 = UTC 01:00
    with pytest.raises(switches.SwitchError):
        flip("job:NOPE", True, cfg)


# ------------------------------ 通知的時機 ------------------------------

def _fail_pull(monkeypatch):
    class Broken:
        def fetch(self, *a):
            raise ConnectorError("EIA 回報錯誤 500")
    monkeypatch.setattr(jobs, "get_connector", lambda code: Broken())


def _ok_pull(monkeypatch):
    class Fine:
        def fetch(self, *a):
            return []
    monkeypatch.setattr(jobs, "get_connector", lambda code: Fine())


def _run_pull(cfg, settings, when):
    """讓 PULL_EIA 在 when 這個時點（當作計劃時點）執行一次，並走通知流程。"""
    job = jobs.find_job(cfg, "PULL_EIA")
    run_id = jobs.claim(job.code, when, "schedule")
    jobs.execute(cfg, settings, job, run_id)
    return notifications.after_run(cfg, job, run_id)


def test_notify_on_failure_remind_sparingly_and_on_recovery(ready, cfg, session, monkeypatch, outbox):
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", HOOK)
    monkeypatch.setenv("EIA_API_KEY", "some-eia-key-123456")
    day = datetime(2026, 11, 10, 23, 30)

    _ok_pull(monkeypatch)
    assert _run_pull(cfg, ready, day) is None                                # 一直正常：不發
    _fail_pull(monkeypatch)
    assert _run_pull(cfg, ready, day + timedelta(days=1)) == "failure"       # 剛失敗：發
    assert _run_pull(cfg, ready, day + timedelta(days=2)) is None            # 還在失敗但剛通知過：不發
    with db.session_scope() as s:                                            # 假裝上一條通知是 7 小時前發的
        s.scalars(select(Notification)).one().created_at -= timedelta(hours=7)
    assert _run_pull(cfg, ready, day + timedelta(days=3)) == "reminder"      # 超過 6 小時：提醒一次
    _ok_pull(monkeypatch)
    assert _run_pull(cfg, ready, day + timedelta(days=4)) == "recovery"      # 恢復：發
    assert _run_pull(cfg, ready, day + timedelta(days=5)) is None

    assert [m["json"]["text"]["content"].splitlines()[0] for m in outbox] == [
        "【crudelab】定時任務失敗：取 EIA 數據", "【crudelab】定時任務仍在失敗：取 EIA 數據",
        "【crudelab】定時任務已恢復：取 EIA 數據"]
    assert "EIA 回報錯誤 500" in outbox[0]["json"]["text"]["content"] and all(m["url"] == HOOK for m in outbox)
    assert [n.event for n in notifications.recent(session)] == ["recovery", "reminder", "failure"]
    assert all(n.results == {"WECOM": "ok"} for n in notifications.recent(session))   # 另兩個渠道沒配置，不算失敗


def test_skipped_and_manual_runs_do_not_notify(ready, cfg, session, monkeypatch, outbox):
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", HOOK)
    assert _run_pull(cfg, ready, datetime(2026, 11, 10, 23, 30)) is None     # 沒配 EIA 密鑰 → 跳過 → 不發
    monkeypatch.setenv("EIA_API_KEY", "some-eia-key-123456")
    _fail_pull(monkeypatch)
    assert jobs.run_now(cfg, ready, "PULL_EIA")["status"] == "failed"        # 手動觸發的失敗：當場可見，不發
    assert outbox == [] and notifications.recent(session) == []


def test_tick_sends_notifications_and_switches_silence_them(ready, cfg, session, monkeypatch, outbox):
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", HOOK)
    monkeypatch.setenv("EIA_API_KEY", "some-eia-key-123456")
    _fail_pull(monkeypatch)
    results = {r["job"]: r for r in scheduler.tick(cfg, ready, T0)}
    assert results["PULL_EIA"]["status"] == "failed" and results["PULL_EIA"]["notified"] == "failure"
    assert results["REPORT_DAILY"]["notified"] is None and len(outbox) == 1
    assert scheduler.status(cfg, T0)["failing"] == ["PULL_EIA"]              # 站內提醒用的清單

    flip("channel:WECOM", False, cfg)                                        # 關掉渠道：記錄還在，但沒有發出
    assert notifications.send(cfg, "x", "y", event="failure", job_code="PULL_EIA") == {}
    flip("channel:WECOM", True, cfg)
    flip("notify", False, cfg)                                               # 通知總開關
    assert notifications.send(cfg, "x", "y", event="failure") == {} and len(outbox) == 1
    assert notifications.send(cfg, "x", "y", event="test", only="WECOM", ignore_switches=True) == {"WECOM": "ok"}


# ------------------------------ 發送 ------------------------------

def test_payload_shapes_per_channel_type(cfg, session, monkeypatch, outbox):
    for name in ("NOTIFY_WECOM_WEBHOOK", "NOTIFY_DINGTALK_WEBHOOK", "NOTIFY_FEISHU_WEBHOOK"):
        monkeypatch.setenv(name, f"https://example.com/{name}")
    assert notifications.send(cfg, "標題", "正文", event="test") == {"WECOM": "ok", "DINGTALK": "ok", "FEISHU": "ok"}
    bodies = {m["url"].rsplit("/", 1)[1]: m["json"] for m in outbox}
    assert bodies["NOTIFY_WECOM_WEBHOOK"] == {"msgtype": "text", "text": {"content": "【crudelab】標題\n正文"}}
    assert bodies["NOTIFY_DINGTALK_WEBHOOK"]["msgtype"] == "text"
    assert bodies["NOTIFY_FEISHU_WEBHOOK"] == {"msg_type": "text", "content": {"text": "【crudelab】標題\n正文"}}
    generic = notifications._payload("webhook", "標題", "正文", {"event": "failure", "job": "J"})
    assert generic == {"source": "crudelab", "title": "標題", "text": "正文", "event": "failure", "job": "J"}
    assert notifications._payload("slack", "t", "b", {}) == {"text": "【crudelab】t\nb"}


def test_send_failures_never_reveal_the_address(cfg, session, monkeypatch):
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", HOOK)
    monkeypatch.setenv("EIA_API_KEY", "eia-secret-key-998877")

    def unreachable(url, **kwargs):
        raise httpx.ConnectError(f"cannot connect to {url}")

    def rejected(url, **kwargs):
        return httpx.Response(200, json={"errcode": 93000, "errmsg": f"invalid webhook url {url}"},
                              request=httpx.Request("POST", url))

    def server_error(url, **kwargs):
        return httpx.Response(502, text=url, request=httpx.Request("POST", url))

    for handler, expected in ((unreachable, "ConnectError"), (rejected, "93000"), (server_error, "502")):
        monkeypatch.setattr(httpx, "post", handler)
        result = notifications.send(cfg, "t", "key eia-secret-key-998877 洩漏測試", event="failure")["WECOM"]
        assert expected in result and "canary-hook" not in result and "weixin" not in result
    stored = json.dumps([[n.title, n.body, n.results] for n in notifications.recent(session)], ensure_ascii=False)
    assert "canary-hook" not in stored and "eia-secret-key-998877" not in stored      # 正文也過了脫敏

    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", "http://plain.example.com/hook")
    assert "https" in notifications.send(cfg, "t", "b", event="test", only="WECOM", ignore_switches=True)["WECOM"]


def test_email_channel(monkeypatch):
    channel = ChannelDef(code="EMAIL", name_zh="郵件", type="email", smtp_host="smtp.example.com", smtp_port=465,
                         mail_from="alerts@example.com", mail_to=["a@example.com", "b@example.com"])
    assert notifications._send_email(channel, "t", "b") == "還沒有設定 SMTP_PASSWORD"
    monkeypatch.setenv("SMTP_PASSWORD", "mail-pass-445566")
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, **kwargs):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    assert notifications._send_email(channel, "標題", "正文") == "ok"
    assert sent["server"] == ("smtp.example.com", 465) and sent["login"] == ("alerts@example.com", "mail-pass-445566")
    assert sent["message"]["To"] == "a@example.com, b@example.com" and sent["message"]["Subject"] == "【crudelab】標題"

    class Refusing(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials mail-pass-445566")

    monkeypatch.setattr(smtplib, "SMTP_SSL", Refusing)
    result = notifications._send_email(channel, "t", "b")
    assert "拒絕登錄" in result and "mail-pass" not in result
    with pytest.raises(ValueError, match="smtp_host"):
        ChannelDef(code="E", name_zh="x", type="email")


# ------------------------------ 接口 ------------------------------

def test_switch_and_notification_api(sites, monkeypatch, outbox):
    front, admin = sites["front"], sites["admin"]
    assert front.put("/api/switches/scheduler", json={"enabled": False}).status_code == 404   # 只在後台
    assert front.get("/api/notifications").status_code == 404

    assert admin.put("/api/switches/job:REPORT_DAILY", json={"enabled": False}).json() == {
        "key": "job:REPORT_DAILY", "enabled": False}
    assert admin.put("/api/switches/job:NOPE", json={"enabled": False}).status_code == 404
    daily = next(j for j in admin.get("/api/schedule").json()["jobs"] if j["code"] == "REPORT_DAILY")
    assert daily["enabled"] is False and daily["overridden"] is True and daily["next_run"] is None
    assert "switch" in {a["action"] for a in admin.get("/api/audit").json()}

    status = admin.get("/api/notifications").json()
    wecom = next(c for c in status["channels"] if c["code"] == "WECOM")
    assert wecom == {**wecom, "configured": False, "enabled": True, "credential": "NOTIFY_WECOM_WEBHOOK"}
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", HOOK)
    assert admin.post("/api/notifications/WECOM/test").json() == {"code": "WECOM", "result": "ok"}
    assert admin.post("/api/notifications/NOPE/test").status_code == 404
    after = admin.get("/api/notifications")
    assert after.json()["recent"][0]["event"] == "test" and "canary-hook" not in after.text   # 地址不出現在任何回應裡
    assert admin.put("/api/switches/notify", json={"enabled": False}).status_code == 200
    assert admin.get("/api/notifications").json()["enabled"] is False


def test_reports_are_not_created_when_job_switched_off(ready, cfg, session):
    flip("job:REPORT_DAILY", False, cfg)
    scheduler.tick(cfg, ready, T0)
    assert session.scalars(select(Report)).all() == []
