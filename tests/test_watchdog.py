"""健康檢查接口和看門狗（#33）：平台掛了能在幾分鐘內收到告警，恢復了再收到一條。"""
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from crudelab import auth, db, scheduler, watchdog
from crudelab.api.app import create_app
from crudelab.cli import main

HOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=canary-watchdog-55eeff"   # secret-scan: ok
URL = "http://platform.internal:8001"
T0 = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)


# ------------------------------ 健康檢查接口 ------------------------------

def test_health_needs_no_login_and_reports_coarse_status(settings, session, cfg):
    auth.create_user(session, "boss", "a-long-password-1", "admin")
    session.commit()
    on = replace(settings, auth_enabled=True)
    front, admin = TestClient(create_app(on, "front")), TestClient(create_app(on, "admin"))
    assert front.get("/api/overview").status_code == 401                         # 其他接口仍要登錄
    r = front.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["checks"]["scheduler"] == "never_ran"
    strict = admin.get("/api/health?require=scheduler")                          # 後台要求調度器在跑
    assert strict.status_code == 503 and strict.json()["required"] == ["database", "config", "scheduler"]
    scheduler.tick(cfg, settings)                                                # 寫一次心跳
    assert admin.get("/api/health?require=scheduler").status_code == 200
    assert set(r.json()) == {"status", "site", "checks", "required"}             # 不含版本、主機、路徑等細節


def test_health_reports_database_failure_without_details(settings, session, monkeypatch):
    client = TestClient(create_app(settings, "front"))

    def broken():
        raise RuntimeError("could not connect to postgresql://user:hunter2@db/crude")   # secret-scan: ok
    monkeypatch.setattr(db, "get_session", broken)
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["checks"]["database"] == "error" and "hunter2" not in r.text


# ------------------------------ 看門狗 ------------------------------

@pytest.fixture()
def platform(monkeypatch):
    """假的平台：up 決定健康檢查的結果；另外接住發往群機器人的消息。"""
    world = {"up": True, "sent": []}

    def get(url, timeout=None):
        if world["up"] is None:
            raise httpx.ConnectError("refused")
        status = 200 if world["up"] else 503
        body = {"status": "ok" if world["up"] else "fail", "site": "admin", "required": ["database", "scheduler"],
                "checks": {"database": "ok", "config": "ok", "scheduler": "ok" if world["up"] else "stale"}}
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    def post(url, json=None, timeout=None):
        world["sent"].append(json["text"]["content"])
        return httpx.Response(200 if world.get("hook_ok", True) else 500, json={"errcode": 0},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", HOOK)
    return world


def test_alert_after_consecutive_failures_remind_and_recover(cfg, tmp_path, platform):
    state = tmp_path / "wd.json"
    run = lambda minutes: watchdog.run_once(cfg, URL, require_scheduler=True, state_path=state,  # noqa: E731
                                            now=T0 + timedelta(minutes=minutes))
    assert run(0)["ok"] and platform["sent"] == []
    platform["up"] = None                                                       # 程序停了：連不上
    assert run(5)["event"] is None                                               # 第一次失敗：可能只是在重啟
    first = run(10)
    assert first["event"] == "failure" and "連不上" in first["body"] and len(platform["sent"]) == 1
    assert "從 2026-11-20 10:05 起" in platform["sent"][0]                       # 按 schedule.yaml 的時區（北京）
    assert run(15)["event"] is None                                              # 已告警：不轟炸
    assert run(5 + 60 * cfg.notifications.repeat_hours + 5)["event"] == "reminder"
    platform["up"] = False                                                       # 程序回來了，但調度器沒心跳
    assert "調度器超過兩分鐘沒有心跳" in watchdog.probe(URL, True).reason
    platform["up"] = True
    back = run(60 * 8)
    assert back["event"] == "recovery" and "停機約 7 小時 55 分鐘" in back["body"]
    assert run(60 * 8 + 5)["event"] is None and not state.read_text(encoding="utf-8").strip("{}")


def test_undelivered_alert_is_retried(cfg, tmp_path, platform):
    state = tmp_path / "wd.json"
    platform["up"], platform["hook_ok"] = None, False                            # 群機器人那邊也出錯
    for minutes in (0, 5):
        result = watchdog.run_once(cfg, URL, state_path=state, now=T0 + timedelta(minutes=minutes))
    assert result["event"] == "failure" and not result["delivered"]
    platform["hook_ok"] = True
    again = watchdog.run_once(cfg, URL, state_path=state, now=T0 + timedelta(minutes=10))
    assert again["event"] == "failure" and again["delivered"]                    # 沒送達就再發，直到送達


def test_real_connection_refused(cfg):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                                                 # 關掉後這個端口沒人聽
    result = watchdog.probe(f"http://127.0.0.1:{port}", False, timeout=3)
    assert not result.ok and "連不上" in result.reason


def test_cli_exit_codes(platform, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(watchdog, "default_state_path", lambda url: tmp_path / "wd.json")
    assert main(["watchdog", URL]) == 0
    platform["up"] = None
    assert main(["watchdog", URL]) == 1                                          # 有問題，還沒到告警次數
    platform["hook_ok"] = False
    assert main(["watchdog", URL]) == 2                                          # 該告警了但沒送達
    assert "有問題：連不上" in capsys.readouterr().out
    assert main(["watchdog", URL, "--channel", "NOPE"]) == 1
