"""大模型調用的限制：頻率和用量上限、自動暫停、不自動重試、用量記錄不含內容、配置選擇。"""
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from crudelab.llm import LLMError, get_provider, limits, status
from crudelab.llm.limits import LLMLimitError
from crudelab.models import LLMCall, LLMPause, utcnow
from crudelab.schema import LLMSettings

KEY = "deepseek-fake-key-7d21c9"              # 測試用的假密鑰  # secret-scan: ok
FAST = dict(provider="deepseek", min_interval_seconds=0)


class FakeServer:
    """假的 DeepSeek：按順序回應給定的狀態碼，記下被請求了幾次。"""

    def __init__(self, monkeypatch, *codes):
        self.codes, self.calls = list(codes), 0
        monkeypatch.setattr(httpx, "post", self.post)

    def post(self, url, json=None, **kwargs):
        self.calls += 1
        code = self.codes.pop(0) if self.codes else 200
        request = httpx.Request("POST", url)
        if code != 200:
            return httpx.Response(code, text=f"upstream said no, key {KEY}", request=request)
        return httpx.Response(200, request=request, json={
            "choices": [{"message": {"content": "正常"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 200}})


@pytest.fixture()
def deepseek(monkeypatch, session):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    for name in ("CRUDELAB_LLM_PROVIDER", "CRUDELAB_LLM_MODEL", "CRUDELAB_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return session


def provider(**overrides):
    return get_provider("chat", LLMSettings(**{**FAST, **overrides}))


def calls(session):
    session.expire_all()
    return list(session.scalars(select(LLMCall).order_by(LLMCall.id)))


def test_success_is_recorded_without_content(deepseek, monkeypatch):
    server = FakeServer(monkeypatch, 200)
    assert provider().complete("系統指令：機密內容", "用戶問題：機密內容") == "正常"
    [call] = calls(deepseek)
    assert (call.status, call.http_status, call.purpose, call.prompt_tokens, call.completion_tokens) == \
        ("ok", 200, "chat", 400, 200)
    assert call.finished_at is not None and server.calls == 1
    assert not any("機密" in str(getattr(call, c.name)) for c in LLMCall.__table__.columns)


def test_rate_and_volume_caps_block_before_sending(deepseek, monkeypatch):
    server = FakeServer(monkeypatch)
    p = provider(max_requests_per_minute=2)
    p.complete("s", "u")
    p.complete("s", "u")
    with pytest.raises(LLMLimitError, match="最近一分鐘"):
        p.complete("s", "u")
    assert server.calls == 2                                             # 第三次沒有發出去

    with pytest.raises(LLMLimitError, match="token"):
        provider(max_tokens_per_day=1000, max_requests_per_minute=100).complete("s", "u")   # 已用 1200
    assert server.calls == 2


def test_concurrency_and_interval(deepseek, monkeypatch):
    server = FakeServer(monkeypatch)
    deepseek.add(LLMCall(provider="deepseek", model="m", purpose="chat", status="running"))
    deepseek.commit()
    with pytest.raises(LLMLimitError, match="進行中"):
        provider(max_concurrent=1).complete("s", "u")
    assert server.calls == 0
    slept = []
    monkeypatch.setattr(limits.time, "sleep", slept.append)
    provider(max_concurrent=5, min_interval_seconds=5).complete("s", "u")
    assert slept and 0 < slept[0] <= 5                                   # 和上一次太近：先等一下再發


@pytest.mark.parametrize(("code", "hours", "reason"),
                         [(401, 24, "密鑰無效"), (403, 24, "沒有權限"), (402, 6, "餘額不足")])
def test_auth_and_balance_errors_pause(deepseek, monkeypatch, code, hours, reason):
    server = FakeServer(monkeypatch, code)
    with pytest.raises(LLMError) as err:
        provider().complete("s", "u")
    assert KEY not in str(err.value) and "upstream" not in str(err.value)
    assert server.calls == 1                                             # 不自動重試
    pause = deepseek.get(LLMPause, "deepseek")
    assert reason in pause.reason and abs((pause.until - utcnow()) - timedelta(hours=hours)) < timedelta(minutes=1)
    with pytest.raises(LLMLimitError, match="自動暫停"):
        provider().complete("s", "u")
    assert server.calls == 1                                             # 暫停期間不發請求
    assert limits.reset() == 1
    assert provider().complete("s", "u") == "正常"


def test_rate_limit_backoff_doubles_and_clears(deepseek, monkeypatch):
    FakeServer(monkeypatch, 429, 429)
    with pytest.raises(LLMError):
        provider().complete("s", "u")
    first = deepseek.get(LLMPause, "deepseek")
    assert first.strikes == 1 and timedelta(minutes=9) < first.until - utcnow() <= timedelta(minutes=10)
    first.until = utcnow() - timedelta(seconds=1)                         # 讓暫停到期
    deepseek.commit()
    with pytest.raises(LLMError):
        provider().complete("s", "u")
    deepseek.expire_all()
    second = deepseek.get(LLMPause, "deepseek")
    assert second.strikes == 2 and second.until - utcnow() > timedelta(minutes=19)
    second.until = utcnow() - timedelta(seconds=1)
    deepseek.commit()
    provider().complete("s", "u")                                        # 成功：清掉過期的暫停和連續次數
    deepseek.expire_all()
    assert deepseek.get(LLMPause, "deepseek") is None


def test_repeated_rejections_and_outages_pause(deepseek, monkeypatch):
    FakeServer(monkeypatch, 400, 400, 400)
    for _ in range(2):
        with pytest.raises(LLMError, match="400"):
            provider().complete("s", "u")
    assert deepseek.get(LLMPause, "deepseek") is None                    # 偶爾被拒絕不暫停
    with pytest.raises(LLMError, match="400"):
        provider().complete("s", "u")
    deepseek.expire_all()
    assert "被拒絕了 3 次" in deepseek.get(LLMPause, "deepseek").reason
    limits.reset()
    deepseek.execute(LLMCall.__table__.delete())
    deepseek.commit()
    FakeServer(monkeypatch, 500, 503, 502)
    for _ in range(3):
        with pytest.raises(LLMError):
            provider().complete("s", "u")
    deepseek.expire_all()
    assert "連續 3 次" in deepseek.get(LLMPause, "deepseek").reason


def test_provider_comes_from_config_and_env_overrides(deepseek, monkeypatch):
    assert status(LLMSettings())["configured"] is False
    assert status(LLMSettings(provider="deepseek"))["model"] == "deepseek-chat"
    assert status(LLMSettings(provider="deepseek", model="deepseek-reasoner"))["model"] == "deepseek-reasoner"
    monkeypatch.setenv("CRUDELAB_LLM_MODEL", "from-env")
    assert status(LLMSettings(provider="deepseek", model="x"))["model"] == "from-env"
    with pytest.raises(ValidationError):
        LLMSettings(provider="compatible", base_url="http://plain-text.example")     # 密鑰不走明文
    assert KEY not in str(status(LLMSettings(provider="deepseek")))


def test_admin_llm_page(sites, deepseek, monkeypatch):
    FakeServer(monkeypatch, 200, 401)
    monkeypatch.setenv("CRUDELAB_LLM_PROVIDER", "deepseek")
    admin, front = sites["admin"], sites["front"]
    assert front.get("/api/llm/usage").status_code == 404
    r = admin.post("/api/llm/test")
    assert r.status_code == 200 and r.json()["reply"] == "正常" and r.json()["tokens"] == [400, 200]
    assert admin.post("/api/llm/test").status_code == 502
    body = admin.get("/api/llm/usage").json()
    assert body["pause"] and "密鑰無效" in body["pause"]["reason"]
    assert [c["status"] for c in body["recent"]] == ["failed", "ok"] and body["usage"]["day"] == 2
    assert KEY not in admin.get("/api/llm/usage").text
    assert admin.post("/api/llm/reset").json()["cleared"] == 1
