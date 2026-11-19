"""密鑰保管：每一條已知的洩漏路徑都有一個測試擋著。"""
import io
import logging
import os
import stat
import subprocess
import sys

import httpx
import pytest

from crudelab import credentials
from crudelab.cli import main
from crudelab.connectors.base import ConnectorError
from crudelab.connectors.eia import EIAConnector
from crudelab.llm import LLMError
from crudelab.llm.providers import OpenAICompatibleProvider
from crudelab.models import Series
from crudelab.settings import PROJECT_ROOT, get_settings

KEY = "eia-live-key-9f3a7c21d4"                # 測試用的假密鑰  # secret-scan: ok


def test_file_is_private_and_values_never_printed(capsys, monkeypatch):
    path = credentials.secrets_path()
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["secret", "set", "EIA_API_KEY", "--stdin"]) == 0
    assert credentials.get("EIA_API_KEY") == KEY and credentials.source("EIA_API_KEY") == "密鑰文件"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert main(["secret", "list"]) == 0
    out = capsys.readouterr()
    assert KEY not in out.out + out.err and "✓ EIA_API_KEY" in out.out and "· KPLER_API_TOKEN" in out.out

    monkeypatch.setenv("EIA_API_KEY", "from-env-value-123456")           # 環境變量優先
    assert credentials.get("EIA_API_KEY") == "from-env-value-123456"
    assert credentials.source("EIA_API_KEY") == "環境變量"
    monkeypatch.delenv("EIA_API_KEY")
    assert main(["secret", "unset", "EIA_API_KEY"]) == 0 and credentials.get("EIA_API_KEY") is None
    assert main(["secret", "set", "NOT_A_KNOWN_NAME", "--stdin"]) == 1
    with pytest.raises(KeyError):
        credentials.get("SOMETHING_UNREGISTERED")


@pytest.mark.skipif(os.name != "posix", reason="文件權限位只在 Mac / Linux 上有意義")
def test_world_readable_file_is_refused():
    credentials.set_secret("EIA_API_KEY", KEY)
    credentials.secrets_path().chmod(0o644)
    with pytest.raises(credentials.CredentialError, match="chmod 600") as exc:
        credentials.get("EIA_API_KEY")
    assert KEY not in str(exc.value)


def test_redact_known_values_and_common_shapes(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-0123456789abcdef")
    url = "postgresql+psycopg://crudelab:" + "Sup3rSecretPw" + "@db.internal:5432/crudelab"
    monkeypatch.setenv("DATABASE_URL", url)
    text = credentials.redact(
        "GET https://api.eia.gov/v2/seriesid/X?api_key=abcdef123456&length=5 failed; "
        "Authorization: Bearer ds-0123456789abcdef; pw Sup3rSecretPw; sk-" + "a" * 24 +
        "; mysql://root:hunter2hunter2@host/db")                                                  # secret-scan: ok
    for leaked in ("abcdef123456", "ds-0123456789abcdef", "Sup3rSecretPw", "a" * 24, "hunter2hunter2"):
        assert leaked not in text
    assert "length=5" in text and "api.eia.gov" in text                     # 只遮密鑰，其餘保留
    assert "Sup3rSecretPw" not in get_settings().safe_database_url and "Sup3rSecretPw" not in repr(get_settings())
    assert credentials.redact("Brent 81.23，庫存 430000") == "Brent 81.23，庫存 430000"


def test_logs_are_redacted(monkeypatch, caplog):
    monkeypatch.setenv("EIA_API_KEY", KEY)
    credentials.install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    try:
        credentials.install_log_redaction()                                # 對後加的 handler 也生效
        logger.error("request to https://api.eia.gov/x?api_key=%s failed", KEY)
        try:
            raise RuntimeError(f"boom https://api.eia.gov/x?api_key={KEY}")
        except RuntimeError:
            logger.exception("unhandled")
    finally:
        logger.removeHandler(handler)
    assert KEY not in stream.getvalue() and "***" in stream.getvalue() and "boom" in stream.getvalue()
    assert logging.getLogger("httpx").level == logging.WARNING             # 不讓 httpx 打印完整請求網址


def _eia(handler) -> EIAConnector:
    return EIAConnector(api_key=KEY, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _series() -> list[Series]:
    return [Series(code="EIA_CRUDE_STOCKS", external_ids={"eia": "PET.WCESTUS1.W"})]


def test_eia_errors_never_carry_the_key():
    def refused(request):
        assert request.url.params["api_key"] == KEY
        return httpx.Response(403, text=f"invalid key {KEY}")              # 對方把 key 回顯出來

    def broken(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")            # 異常原文裡有完整網址

    for handler, expected in ((refused, "無效"), (broken, "連不上"),
                              (lambda r: httpx.Response(500, text=KEY), "500"),
                              (lambda r: httpx.Response(200, text=f"<html>{KEY}"), "不是 JSON")):
        with pytest.raises(ConnectorError, match=expected) as exc:
            _eia(handler).fetch(_series(), None, None)
        assert KEY not in str(exc.value) and "api_key" not in str(exc.value)
        assert exc.value.__cause__ is None                                 # 也不把原始異常掛在後面
    ok = _eia(lambda r: httpx.Response(200, json={"response": {"data": [{"period": "2026-10-23", "value": 430000}]}}))
    assert ok.fetch(_series(), None, None)[0].value == 430000


def test_pull_endpoint_does_not_leak(admin, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", KEY)

    def explode(self, series, start, end):
        raise RuntimeError(f"GET https://api.eia.gov/v2/seriesid/X?api_key={KEY} timed out")

    monkeypatch.setattr(EIAConnector, "fetch", explode)
    r = admin.post("/api/sources/eia/pull", json={})
    assert r.status_code == 502 and KEY not in r.text and "RuntimeError" in r.json()["detail"]
    listing = admin.get("/api/sources").text
    assert KEY not in listing and KEY not in admin.get("/api/audit").text


def test_llm_errors_never_carry_key_or_upstream_text(monkeypatch):
    def post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(401, text=f"Incorrect API key provided: {KEY}", request=request)

    monkeypatch.setattr(httpx, "post", post)
    provider = OpenAICompatibleProvider("deepseek", "https://api.deepseek.com", KEY, "test-model")
    with pytest.raises(LLMError, match="401：密鑰無效") as exc:
        provider.complete("s", "u")
    assert KEY not in str(exc.value) and exc.value.__cause__ is None

    def unreachable(url, **kwargs):
        raise httpx.ConnectError(f"failed with header Bearer {KEY}")

    monkeypatch.setattr(httpx, "post", unreachable)
    with pytest.raises(LLMError, match="連不上") as exc:
        provider.complete("s", "u")
    assert KEY not in str(exc.value)


def test_http_errors_are_redacted_on_the_way_out(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setenv("CRUDELAB_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("CRUDELAB_LLM_MODEL", "test-model")
    from crudelab.llm import chat

    def leaky(*args, **kwargs):
        raise LLMError(f"上游說：key {KEY} 無效")                          # 假設某處不小心把 key 放進了錯誤訊息

    monkeypatch.setattr(chat, "answer", leaky)
    r = client.post("/api/chat", json={"question": "x"})
    assert r.status_code == 502 and KEY not in r.text and "***" in r.json()["detail"]
    assert KEY not in client.get("/api/llm").text


def test_repository_has_no_secrets_and_scanner_catches_them(tmp_path):
    script = PROJECT_ROOT / "scripts" / "check_secrets.py"
    clean = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout

    sys.path.insert(0, str(script.parent))
    try:
        import check_secrets
    finally:
        sys.path.pop(0)
    # 樣本拆開寫，免得這個測試文件自己被掃描器當成洩漏
    samples = ['OPENAI_API_KEY = "' + "abcdEFGH1234" * 2 + '"', "key: sk-" + "x" * 30,
               "url: postgresql://app:" + "hunter2secret" + "@db/x", "-----BEGIN RSA " + "PRIVATE KEY-----"]
    for i, content in enumerate(samples):
        path = tmp_path / f"f{i}.txt"
        path.write_text(content + "\n", encoding="utf-8")
        found = check_secrets.scan([path])
        assert len(found) == 1 and content.split()[-1] not in found[0]     # 報位置，不回顯內容
    harmless = tmp_path / "ok.txt"
    harmless.write_text('EIA_API_KEY 用 crudelab secret set 設定\npostgresql+psycopg://crudelab:你的密碼@localhost/x\n'
                        'api_key = os.environ["X"]\n', encoding="utf-8")
    assert check_secrets.scan([harmless]) == []


def test_data_requirements_doc_is_current_and_complete(cfg):
    from crudelab.data_needs import PROVIDERS, provider_of, render
    text = render(cfg)
    committed = (PROJECT_ROOT / "docs" / "data-requirements.md").read_text(encoding="utf-8")
    assert committed == text, "配置變了：請運行 crudelab data-needs --output docs/data-requirements.md"
    assert all(f"`{code}`" in text for code in cfg.series)                 # 每條序列都列出來了
    assert all(f"`{name}`" in text for name in credentials.KNOWN)          # 每個憑證都列出來了
    assert {provider_of(s) for s in cfg.series.values()} <= set(PROVIDERS)
    kinds = {code: provider_of(cfg.series[code]) for code in
             ("EIA_CRUDE_STOCKS", "BRENT_M1", "SC_M1", "DATED_BRENT", "ARAB_LIGHT_OSP_ASIA", "TD3C_WS", "USDCNY",
              "SD_GASOLINE_92", "IEA_WORLD_DEMAND", "PP_CN", "PX_CFR_CHINA")}
    assert kinds == {"EIA_CRUDE_STOCKS": "eia", "BRENT_M1": "exchange_intl", "SC_M1": "exchange_cn",
                     "DATED_BRENT": "pra", "ARAB_LIGHT_OSP_ASIA": "osp", "TD3C_WS": "freight", "USDCNY": "fx",
                     "SD_GASOLINE_92": "cn_vendor", "IEA_WORLD_DEMAND": "agency", "PP_CN": "exchange_cn",
                     "PX_CFR_CHINA": "pra"}
    more = {code: provider_of(cfg.series[code]) for code in
            ("CFTC_WTI_MM_LONG", "CAL_EIA_CRUDE_SURPRISE", "MACRO_XAUUSD", "GLOBAL_REFINERY_OUTAGES",
             "CN_SD_TEAPOT_UTIL")}
    assert more == {"CFTC_WTI_MM_LONG": "cftc", "CAL_EIA_CRUDE_SURPRISE": "jin10", "MACRO_XAUUSD": "jin10",
                    "GLOBAL_REFINERY_OUTAGES": "consult", "CN_SD_TEAPOT_UTIL": "cn_vendor"}   # 自動取的不能歸到 Platts
    assert "FLAT_TD3C" in text and "PROJ_PRICE_PER_STOCK" in text
