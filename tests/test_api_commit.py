"""接口先提交、再回應（查 #33 時 CI 偶發失敗查出的問題）。

FastAPI 預設在回應產生之後才結束帶 yield 的依賴：先回「成功」、後提交。提交失敗時用戶以為存好了；
緊接着的下一個請求也可能還看不到剛寫的東西。api/deps.py 的 DB 用 scope="function" 讓提交發生在回應之前。
"""
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from crudelab.api.app import create_app

SRC = Path(__file__).resolve().parents[1] / "src" / "crudelab"


def test_no_route_uses_the_late_committing_dependency():
    offenders = [f"{p.relative_to(SRC)}:{i}" for p in SRC.rglob("*.py")
                 for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if "Depends(get_db)" in line]
    assert offenders == [], "改用 api/deps.py 的 DB：" + "、".join(offenders)


def test_failed_commit_is_an_error_not_a_success(settings, session, monkeypatch):
    client = TestClient(create_app(settings, "front"), raise_server_exceptions=False)
    real = Session.commit

    def failing(self):                                             # 這個請求裡的提交一律失敗（例如數據庫被鎖）
        raise OperationalError("COMMIT", {}, Exception("database is locked"))
    monkeypatch.setattr(Session, "commit", failing)
    r = client.post("/api/scenarios", json={"code": "C1", "name_zh": "提交會失敗"})
    assert r.status_code == 500                                   # 以前這裡是 200，數據卻沒存
    monkeypatch.setattr(Session, "commit", real)
    assert all(s["code"] != "C1" for s in client.get("/api/scenarios").json())


def test_write_is_visible_to_the_very_next_request(settings, session):
    client = TestClient(create_app(settings, "front"))
    assert client.post("/api/scenarios", json={"code": "C2", "name_zh": "馬上可見"}).status_code == 200
    assert any(s["code"] == "C2" for s in client.get("/api/scenarios").json())
