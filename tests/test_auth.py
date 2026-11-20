"""登錄與權限。登錄功能預設關閉，這裡的測試顯式打開它。"""
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from crudelab import auth, db
from crudelab.api.app import NoAdminAccount, create_app
from crudelab.api.security import required_role
from crudelab.cli import main
from crudelab.models import AuditLog, UserSession, utcnow

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 8)          # 測試裡用弱參數，否則每次哈希要 0.1 秒
    monkeypatch.setattr(auth, "_DUMMY_HASH", None)


@pytest.fixture()
def secured(settings, session):
    """開了登錄的兩個站點，以及三種角色各一個帳號。"""
    for name, role in (("alice", "admin"), ("bob", "analyst"), ("carol", "viewer")):
        auth.create_user(session, name, PASSWORD, role, actor="setup")
    session.commit()
    on = replace(settings, auth_enabled=True)
    return {"front": lambda: TestClient(create_app(on, "front")), "admin": lambda: TestClient(create_app(on, "admin"))}


def login(client, name, password=PASSWORD):
    return client.post("/api/auth/login", json={"username": name, "password": password})


# ------------------------------ 帳號服務 ------------------------------

def test_password_hashing():
    stored = auth.hash_password(PASSWORD)
    assert PASSWORD not in stored and stored != auth.hash_password(PASSWORD)          # 加鹽：同一密碼兩次結果不同
    assert auth.verify_password(PASSWORD, stored) and not auth.verify_password(PASSWORD + "x", stored)
    assert not auth.verify_password(PASSWORD, "garbage") and not auth.verify_password(PASSWORD, "md5$1$2$3$4$5")


def test_account_rules(session):
    alice = auth.create_user(session, "Alice", PASSWORD, "admin")
    for kwargs, message in [({"username": "alice"}, "已存在"), ({"username": "a b"}, "用戶名"),
                            ({"password": "short"}, "至少要"), ({"role": "root"}, "角色")]:
        with pytest.raises(auth.AuthError, match=message):
            auth.create_user(session, **{"username": "dave", "password": PASSWORD, "role": "viewer", **kwargs})
    with pytest.raises(auth.AuthError, match="最後一個"):
        auth.update_user(session, alice.id, role="viewer")
    with pytest.raises(auth.AuthError, match="最後一個"):
        auth.update_user(session, alice.id, active=False)
    auth.create_user(session, "erin", PASSWORD, "admin")
    assert auth.update_user(session, alice.id, role="viewer").role == "viewer"        # 還有別的管理員就可以
    session.commit()
    assert {a.action for a in session.scalars(select(AuditLog))} >= {"user_create", "user_update"}


def test_sessions_expire_and_are_revoked(session):
    user = auth.create_user(session, "alice", PASSWORD, "admin")
    token = auth.create_session(session, user, hours=1)
    session.commit()
    assert auth.resolve_session(session, token).username == "alice"
    assert token not in {s.token_hash for s in session.scalars(select(UserSession))}  # 庫裡沒有令牌原文
    assert auth.resolve_session(session, token, now=utcnow() + timedelta(hours=2)) is None
    assert auth.resolve_session(session, "not-a-token") is None and auth.resolve_session(session, None) is None
    auth.set_password(session, user.id, PASSWORD + "2")                               # 改密碼 → 舊登錄全部失效
    session.commit()
    assert auth.resolve_session(session, token) is None


def test_login_lockout_and_no_username_probing(session):
    auth.create_user(session, "alice", PASSWORD, "admin")
    session.commit()
    with pytest.raises(auth.LoginFailed):
        auth.authenticate(session, "nobody", PASSWORD)                                # 不存在的帳號：同一個錯誤
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.LoginFailed):
            auth.authenticate(session, "alice", "wrong password")
    with pytest.raises(auth.TooManyAttempts):
        auth.authenticate(session, "ALICE", PASSWORD)                                 # 鎖定期間對的密碼也不行
    session.commit()
    assert auth.locked_until(session, "alice") is not None
    assert auth.unlock(session, "alice") is True                                      # 管理員解鎖
    assert auth.authenticate(session, "ALICE", PASSWORD).username == "alice"          # 用戶名不分大小寫


def test_role_table():
    assert required_role("front", "GET", "/api/indicators") == "viewer"
    assert required_role("front", "POST", "/api/scenarios") == "analyst"
    assert required_role("front", "DELETE", "/api/adjustments/1") == "analyst"
    assert required_role("front", "POST", "/api/chat") == "analyst"                   # 問答要花錢
    assert required_role("front", "POST", "/api/refineries/CN_FUEL_SD/scenario") == "viewer"   # 不保存的試算
    assert required_role("admin", "GET", "/api/series") == "admin"
    assert required_role("admin", "POST", "/api/imports/preview") == "admin"
    for path in ("/api/auth/login", "/api/auth/me", "/api/site"):
        assert required_role("admin", "POST", path) is None


# ------------------------------ 接口 ------------------------------

def test_off_by_default(client, admin):
    assert client.get("/api/site").json()["auth"] == {"enabled": False, "user": None}
    assert client.get("/api/indicators").status_code == 200 and admin.get("/api/audit").status_code == 200
    assert client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 400
    assert admin.post("/api/users", json={"username": "zed", "password": PASSWORD, "role": "admin"}).status_code == 200


def test_nothing_is_readable_without_login(secured):
    front, admin = secured["front"](), secured["admin"]()
    for client, paths in ((front, ["/api/overview", "/api/indicators", "/api/series", "/api/parameters", "/api/reports",
                                    "/api/scenarios", "/api/events", "/api/signals", "/api/balances", "/api/llm"]),
                          (admin, ["/api/overview", "/api/series", "/api/audit", "/api/sources", "/api/users"])):
        for path in paths:
            assert client.get(path).status_code == 401, path
    assert front.post("/api/scenarios", json={"code": "X", "name_zh": "x"}).status_code == 401
    site = front.get("/api/site").json()
    assert site["auth"] == {"enabled": True, "user": None} and site["config_error"] is None
    assert front.get("/").status_code == 200                                         # 頁面外殼本身不含數據


def test_roles_through_the_api(secured):
    viewer, analyst, boss = secured["front"](), secured["front"](), secured["front"]()
    assert login(viewer, "carol").json() == {"username": "carol", "role": "viewer", "role_name": "只讀"}
    login(analyst, "bob")
    login(boss, "alice")
    assert viewer.get("/api/indicators").status_code == 200
    assert viewer.get("/api/site").json()["auth"]["user"]["username"] == "carol"
    denied = viewer.post("/api/scenarios", json={"code": "V", "name_zh": "x"})
    assert denied.status_code == 403 and "研究員" in denied.json()["detail"]
    assert viewer.post("/api/refineries/SG_COMPLEX/scenario", json={}).status_code == 400    # 過了權限，只是沒數據
    assert analyst.post("/api/scenarios", json={"code": "B", "name_zh": "x"}).status_code == 200
    assert boss.post("/api/events", json={"title": "事件"}).status_code == 200

    for name, expected in (("carol", 403), ("bob", 403), ("alice", 200)):
        client = secured["admin"]()
        login(client, name)
        assert client.get("/api/audit").status_code == expected, name
        assert client.post("/api/parameters/CN_PORT_MISC/versions", json={"value": 50}).status_code == expected

    # 操作者記的是真實用戶名
    with db.session_scope() as s:
        actors = {a.actor for a in s.scalars(select(AuditLog))}
        from crudelab.models import ParameterVersion, ScenarioRow
        assert s.get(ScenarioRow, "B").created_by == "bob"
        assert s.scalars(select(ParameterVersion).where(ParameterVersion.key == "CN_PORT_MISC")
                         .order_by(ParameterVersion.id.desc())).first().created_by == "alice"
    assert "local" not in actors and {"alice", "bob", "carol"} <= actors


def test_login_failures_logout_and_cookie(secured):
    client = secured["front"]()
    bad = login(client, "alice", "wrong password")
    assert bad.status_code == 401 and bad.json()["detail"] == login(client, "nobody").json()["detail"]
    ok = login(client, "alice")
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie
    assert client.get("/api/overview").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/overview").status_code == 401
    for _ in range(auth.MAX_FAILURES):
        login(client, "bob", "wrong password")
    assert login(client, "bob").status_code == 429


def test_cross_site_writes_are_rejected(secured):
    client = secured["front"]()
    login(client, "bob")
    body = {"code": "C", "name_zh": "x"}
    assert client.post("/api/scenarios", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/scenarios", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/scenarios", headers={"Origin": "https://evil.example"}).status_code == 200   # 只查寫請求


def test_password_change_and_user_management(secured):
    boss = secured["admin"]()
    login(boss, "alice")
    made = boss.post("/api/users", json={"username": "dave", "password": PASSWORD, "role": "analyst"}).json()
    assert made["role_name"] == "研究員" and "password" not in str(boss.get("/api/users").json()).lower()
    assert boss.post("/api/users", json={"username": "dave2", "password": "short", "role": "viewer"}).status_code == 400

    dave = secured["front"]()
    login(dave, "dave")
    new = PASSWORD + "!"
    assert dave.post("/api/auth/password", json={"current_password": "nope", "new_password": new}).status_code == 400
    assert dave.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": new}).status_code == 200
    assert dave.get("/api/overview").status_code == 200                              # 當前瀏覽器換發了新登錄
    assert login(secured["front"](), "dave").status_code == 401
    assert login(secured["front"](), "dave", new).status_code == 200

    assert boss.patch(f"/api/users/{made['id']}", json={"active": False}).json()["active"] is False
    assert dave.get("/api/overview").status_code == 401                              # 停用後立刻失效
    me = next(u for u in boss.get("/api/users").json() if u["username"] == "alice")
    assert boss.patch(f"/api/users/{me['id']}", json={"role": "viewer"}).status_code == 400   # 最後一個管理員


def test_secure_cookie_and_refusing_to_start_without_admin(settings, session):
    with pytest.raises(NoAdminAccount, match="crudelab user add"):
        create_app(replace(settings, auth_enabled=True), "front")
    auth.create_user(session, "alice", PASSWORD, "admin")
    session.commit()
    client = TestClient(create_app(replace(settings, auth_enabled=True, cookie_secure=True), "front"),
                        base_url="https://testserver")
    assert "secure" in login(client, "alice").headers["set-cookie"].lower()


def test_cli_user_commands(settings, monkeypatch, capsys):
    import io
    monkeypatch.setenv("CRUDELAB_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DATABASE_URL", settings.database_url)

    def run(*args, password=None):
        monkeypatch.setattr("sys.stdin", io.StringIO((password or "") + "\n"))
        code = main(["user", *args, *(["--password-stdin"] if password else [])])
        return code, capsys.readouterr()

    assert run("add", "alice", "--role", "admin", password=PASSWORD)[0] == 0
    assert run("add", "alice", password=PASSWORD)[0] == 1
    assert run("add", "bob", password="short")[0] == 1
    code, out = run("list")
    assert code == 0 and "alice" in out.out and "管理員" in out.out and "bob" not in out.out
    assert run("disable", "alice")[0] == 1                                           # 最後一個管理員
    assert run("add", "bob", "--role", "admin", password=PASSWORD)[0] == 0
    assert run("role", "alice", "--role", "viewer")[0] == 0 and run("disable", "alice")[0] == 0
    assert run("passwd", "bob", password=PASSWORD + "9")[0] == 0 and run("passwd", "nobody", password=PASSWORD)[0] == 1
    with db.session_scope() as s:
        assert auth.authenticate(s, "bob", PASSWORD + "9").role == "admin"


# ------------------------------ 鎖定存數據庫（#36） ------------------------------

def test_lockout_holds_across_processes_and_restarts(settings, session):
    """兩個「進程」各有自己的數據庫連接（模擬多進程 / 重啟）：在一個裡連錯，另一個裡也鎖著。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    auth.create_user(session, "alice", PASSWORD, "admin")
    session.commit()
    other = sessionmaker(create_engine(settings.database_url))()
    t0 = utcnow()
    for i in range(auth.MAX_FAILURES):
        with pytest.raises(auth.LoginFailed):
            auth.authenticate(session, "alice", "wrong password", now=t0 + timedelta(seconds=i))
        session.commit()                                                    # 接口層在拋錯後也會提交
    with pytest.raises(auth.TooManyAttempts):
        auth.authenticate(other, "Alice", PASSWORD, now=t0 + timedelta(minutes=1))
    until = auth.locked_until(other, "alice", now=t0 + timedelta(minutes=1))
    assert until == t0 + timedelta(seconds=auth.FAILURE_WINDOW_SECONDS)     # 最早那次失敗滿 15 分鐘後解開
    later = t0 + timedelta(seconds=auth.FAILURE_WINDOW_SECONDS + 1)
    assert auth.authenticate(other, "alice", PASSWORD, now=later).username == "alice"
    other.commit()
    assert auth.locked_until(session, "alice", now=later) is None
    other.close()


def test_old_failures_are_cleaned_up(session):
    from crudelab.models import LoginFailure
    t0 = utcnow() - timedelta(hours=2)
    for i in range(40):                                                     # 撒網：很多個用戶名各錯一次
        with pytest.raises(auth.LoginFailed):
            auth.authenticate(session, f"random-{i}", "x" * 12, now=t0)
    with pytest.raises(auth.LoginFailed):
        auth.authenticate(session, "someone", "x" * 12)                     # 一小時後再有一次失敗：舊記錄清掉
    session.commit()
    assert [r.username_key for r in session.scalars(select(LoginFailure))] == ["someone"]


def test_admin_sees_and_clears_lock(secured):
    client = secured["front"]()
    for _ in range(auth.MAX_FAILURES):
        login(client, "bob", "wrong password")
    locked = login(client, "bob")
    assert locked.status_code == 429 and "解鎖" in locked.json()["detail"]
    admin = secured["admin"]()
    login(admin, "alice")
    bob = next(u for u in admin.get("/api/users").json() if u["username"] == "bob")
    assert bob["locked_until"]
    assert admin.patch(f"/api/users/{bob['id']}", json={"unlock": True}).json()["locked_until"] is None
    assert login(client, "bob").status_code == 200


def test_cli_unlock(settings, session, capsys, monkeypatch):
    monkeypatch.setenv("CRUDELAB_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    auth.create_user(session, "alice", PASSWORD, "admin")
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.LoginFailed):
            auth.authenticate(session, "alice", "wrong password")
    session.commit()
    assert main(["user", "list"]) == 0 and "暫時鎖定" in capsys.readouterr().out
    assert main(["user", "unlock", "alice"]) == 0 and "已解鎖 alice" in capsys.readouterr().out
    assert auth.locked_until(session, "alice") is None
