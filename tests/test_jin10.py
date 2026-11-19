"""金十數據：MCP 客戶端（握手、會話、SSE、錯誤）、額度控制、財經日曆的同步與超預期。用模擬的 MCP 服務器測試。"""
import json
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select

from crudelab import jin10, jobs
from crudelab.connectors.mcp import MCPClient, MCPError, MCPRateLimited, MCPToolError
from crudelab.models import CalendarEvent, ExternalCall

TOKEN = "jin10-fake-token-8c1d"               # 測試用的假 token  # secret-scan: ok


class FakeServer:
    """按 MCP 流程回應的假服務器。tools 是 {工具名: 回傳 structuredContent 的函數或異常}。"""

    def __init__(self, tools=None, sse=False):
        self.tools, self.sse, self.requests = tools or {}, sse, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((body, dict(request.headers)))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return self.reply(body, {"protocolVersion": "2025-11-25", "serverInfo": {"name": "jin10"}},
                              headers={"Mcp-Session-Id": "sess-1"})
        assert request.headers.get("mcp-session-id") == "sess-1"
        assert request.headers.get("mcp-protocol-version") == "2025-11-25"
        if body["method"] == "tools/list":
            return self.reply(body, {"tools": [{"name": n} for n in self.tools]})
        if body["method"] == "resources/read":
            return self.reply(body, {"contents": [{"uri": "quote://codes", "text": json.dumps({"data": ["UKOIL"]})}]})
        tool = body["params"]["name"]
        handler = self.tools[tool]
        if handler == "ratelimit":
            text = "今日该工具调用次数已达上限，请明日再试"
            return self.reply(body, {"isError": True, "content": [{"type": "text", "text": text}]})
        if handler == "protocol":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32602, "message": f"bad params {TOKEN}"}})
        return self.reply(body, {"structuredContent": handler(body["params"]["arguments"]),
                                 "content": [{"type": "text", "text": "給人看的文字"}]})

    def reply(self, body, result, headers=None):
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            text = "event: message\ndata: " + json.dumps(message) + "\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(200, json=message, headers=headers or {})


def client_for(server):
    transport = httpx.Client(transport=httpx.MockTransport(server))
    return MCPClient("金十", "https://mcp.example/mcp", TOKEN, client=transport)


@pytest.mark.parametrize("sse", [False, True])
def test_handshake_session_and_structured_content(sse):
    server = FakeServer({"get_quote": lambda args: {"data": {"code": args["code"], "close": 82.5}}}, sse=sse)
    client = client_for(server)
    assert client.call("get_quote", {"code": "UKOIL"}) == {"data": {"code": "UKOIL", "close": 82.5}}
    methods = [b["method"] for b, _ in server.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert client.read_resource("quote://codes") == {"data": ["UKOIL"]}


def test_errors_are_readable_and_never_leak_the_token():
    client = client_for(FakeServer({"get_quote": "ratelimit", "list_news": "protocol"}))
    with pytest.raises(MCPRateLimited, match="上限"):
        client.call("get_quote", {"code": "X"})
    with pytest.raises(MCPError) as err:
        client.call("list_news", {})
    assert TOKEN not in str(err.value) and "-32602" in str(err.value)
    denied = MCPClient("金十", "https://mcp.example/mcp", TOKEN,
                       client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    with pytest.raises(MCPError, match="token 無效") as err:
        denied.call("get_quote", {})
    assert TOKEN not in str(err.value)
    assert issubclass(MCPRateLimited, MCPToolError)


def calendar_rows(consensus="-150", actual=None, title="美国至11月14日当周EIA原油库存(万桶)"):
    return [{"pub_time": "2026-11-19 23:30:00", "star": 4, "title": title, "previous": "-87.4",
             "consensus": consensus, "actual": actual, "revised": None, "affect_txt": "利空 原油"},
            {"pub_time": "2026-11-20 21:30:00", "star": 5, "title": "美国10月季调后非农就业人口(万人)",
             "previous": "25.4", "consensus": "18", "actual": None}]


def test_calendar_sync_freezes_consensus_and_writes_surprise(session, cfg):
    state = {"rows": calendar_rows()}
    server = FakeServer({"list_calendar": lambda args: {"data": state["rows"]}})
    jin = jin10.Jin10(cfg, client_for(server))
    first = jin10.sync_calendar(session, cfg, jin)
    session.commit()
    assert (first["new"], first["released"], first["observations"]) == (2, 0, 0)
    eia = session.scalars(select(CalendarEvent).where(CalendarEvent.title.contains("EIA"))).one()
    assert eia.pub_time.hour == 15                                       # 北京 23:30 = UTC 15:30

    state["rows"] = calendar_rows(consensus="-120")                      # 公佈前預期改了：跟著改
    jin10.sync_calendar(session, cfg, jin)
    session.commit()                                                     # 和定時任務一樣：每次同步一個事務
    state["rows"] = calendar_rows(consensus="-120", actual="-50")        # 公佈：凍結當時的預期
    released = jin10.sync_calendar(session, cfg, jin)
    session.commit()
    assert released["released"] == 1 and released["observations"] == 1
    state["rows"] = calendar_rows(consensus="-300", actual="-50")        # 事後改預期：超預期不變
    jin10.sync_calendar(session, cfg, jin)
    session.commit()
    session.expire_all()
    eia = session.scalars(select(CalendarEvent).where(CalendarEvent.title.contains("EIA"))).one()
    assert eia.consensus_at_release == "-120" and jin10.surprise(eia) == pytest.approx(70)
    from crudelab.indicators import IndicatorEngine
    values = IndicatorEngine(session, cfg).series_values("CAL_EIA_CRUDE_SURPRISE")
    assert values.iloc[-1] == pytest.approx(700) and values.index[-1].date() == date(2026, 11, 19)   # 万桶 → 千桶
    assert session.scalar(select(func.count()).select_from(ExternalCall)) == 4


def test_unit_guard_and_unparsable_values(session, cfg):
    rows = calendar_rows(actual="-50", title="美国至11月14日当周EIA原油库存(千桶)")   # 單位不是万桶：不換算
    rows.append({"pub_time": "2026-11-19 23:30:00", "title": "EIA汽油库存(万桶)", "consensus": "--", "actual": "12"})
    server = FakeServer({"list_calendar": lambda args: {"data": rows}})
    stats = jin10.sync_calendar(session, cfg, jin10.Jin10(cfg, client_for(server)))
    assert stats["released"] == 2 and stats["observations"] == 0
    assert jin10.number("3.2%") == 3.2 and jin10.number("1.2-1.5") is None and jin10.number("--") is None


def test_local_limit_stops_before_sending(session, cfg, monkeypatch):
    server = FakeServer({"list_calendar": lambda args: {"data": []}})
    monkeypatch.setattr(cfg.jin10, "max_calls_per_tool_per_day", 2)
    jin = jin10.Jin10(cfg, client_for(server))
    jin.call("list_calendar")
    jin.call("list_calendar")
    sent = len(server.requests)
    with pytest.raises(jin10.Jin10LimitReached):
        jin.call("list_calendar")
    assert len(server.requests) == sent                                  # 第三次沒有發出去
    assert jin10.used_today("list_calendar") == 2


def test_job_without_token_is_skipped(session, cfg, settings, client):
    result = jobs.run_now(cfg, settings, "SYNC_JIN10_CALENDAR")
    assert result["status"] == "skipped" and "JIN10_TOKEN" in result["message"]
    body = client.get("/api/calendar").json()
    assert body["configured"] is False and body["events"] == []


def test_probe_command(session, cfg, monkeypatch, capsys):
    from crudelab import cli
    server = FakeServer({"get_quote": lambda a: {"data": {"code": "UKOIL", "close": 82.5}},
                         "get_kline": lambda a: {"data": {"code": "UKOIL", "klines": []}},
                         "list_calendar": lambda a: {"data": calendar_rows()},
                         "list_flash": lambda a: {"data": {"items": [{"id": 1, "content": "沙特宣布延长减产"}],
                                                           "has_more": False}}})
    monkeypatch.setenv("JIN10_TOKEN", TOKEN)
    monkeypatch.setattr(jin10, "MCPClient", lambda *args, **kw: client_for(server))
    assert cli.main(["jin10", "probe"]) == 0
    out = capsys.readouterr().out
    assert "已連接" in out and "EIA原油库存" in out and "第一條的字段：['content', 'id']" in out
    assert TOKEN not in out
    assert cli.main(["jin10", "usage"]) == 0
    assert "list_calendar    1" in capsys.readouterr().out


# ------------------------------ 快訊 ------------------------------

def flash_page(ids, cursor=None, has_more=False):
    items = [{"id": i, "time": f"2026-11-19 10:{i:02d}:00", "content": f"<b>消息{i}</b>：沙特称将延长原油减产",
              "url": f"https://flash.example/{i}"} for i in ids]
    items.append({"id": 999, "time": "2026-11-19 09:00:00", "content": "日本央行维持利率不变"})   # 不相關
    return {"data": {"items": items, "next_cursor": cursor, "has_more": has_more}}


def test_flash_sync_filters_paginates_and_stops_at_known(session, cfg):
    pages = {None: flash_page([5, 4], cursor="p2", has_more=True), "p2": flash_page([3, 2], cursor="p3", has_more=True),
             "p3": flash_page([1])}
    calls = []

    def list_flash(args):
        calls.append(args.get("cursor"))
        return pages[args.get("cursor")]
    jin = jin10.Jin10(cfg, client_for(FakeServer({"list_flash": list_flash})))
    first = jin10.sync_flash(session, cfg, jin)
    session.commit()
    assert first["kept"] == 5 and calls == [None, "p2", "p3"]
    from crudelab.models import NewsFlash
    flash = session.scalars(select(NewsFlash).where(NewsFlash.ext_id == "5")).one()
    assert flash.content == "消息5：沙特称将延长原油减产" and set(flash.keywords) >= {"原油", "沙特"}   # 去掉 HTML
    assert flash.time.hour == 2                                          # 北京 10:05 = UTC 02:05

    pages[None] = flash_page([6, 5], cursor="p2", has_more=True)        # 只多了一條：看到已收錄的就停
    calls.clear()
    second = jin10.sync_flash(session, cfg, jin)
    session.commit()
    assert second["kept"] == 1 and calls == [None]


def test_flash_extract_to_drafts(session, cfg, sites, monkeypatch):
    from crudelab.api.routes import geo
    from crudelab.llm.tasks import ExtractedEvent
    from crudelab.models import Event, NewsFlash
    from crudelab.models import utcnow as now
    session.add(NewsFlash(source="jin10", ext_id="1", time=now(), content="沙特称将延长原油减产至明年一季度",
                          keywords=["沙特"], important=True, url="https://flash.example/1"))
    session.commit()
    seen = []

    class Provider:
        name = "fake"

    def fake_extract(provider, text, cfg):
        seen.append(text)
        return [ExtractedEvent(title="沙特延長減產", category="opec_policy", bias=1)], 0
    monkeypatch.setattr(geo, "get_provider", lambda *a, **k: Provider())
    monkeypatch.setattr(geo, "extract_events", fake_extract)
    front = sites["front"]
    item = front.get("/api/flashes").json()["items"][0]
    assert item["extracted"] is False and item["important"] is True
    r = front.post(f"/api/flashes/{item['id']}/extract")
    assert r.status_code == 200 and r.json()["drafts"] == 1
    assert "沙特称将延长原油减产" in seen[0]
    session.expire_all()
    draft = session.scalars(select(Event)).one()
    assert draft.draft and draft.origin == "llm" and "flash.example/1" in draft.source
    assert front.get("/api/flashes").json()["items"][0]["drafts"] == 1
    assert front.post("/api/flashes/999/extract").status_code == 404


def test_flash_job_skips_without_token(session, cfg, settings):
    result = jobs.run_now(cfg, settings, "SYNC_JIN10_FLASH")
    assert result["status"] == "skipped" and "JIN10_TOKEN" in result["message"]


# ------------------------------ 報價 ------------------------------

def test_quote_connector_writes_daily_values(session, cfg):
    from crudelab.connectors.jin10_quotes import Jin10QuoteConnector
    from crudelab.models import Series
    quotes = {"UKOIL": 82.4, "USOIL": 78.1, "XAUUSD": 2650.0, "USDCNH": 7.25, "EURUSD": 1.05, "USDJPY": 154.2}
    server = FakeServer({"get_quote": lambda a: {"data": {"code": a["code"], "close": str(quotes[a["code"]]),
                                                          "time": "2026-11-19 06:35:00"}}})
    connector = Jin10QuoteConnector(cfg, client_for(server))
    obs = connector.fetch(list(session.scalars(select(Series))), None, None)
    got = {o.series_code: (o.obs_date, o.value) for o in obs}
    assert got["MACRO_XAUUSD"] == (date(2026, 11, 19), 2650.0) and got["JIN10_UKOIL"][1] == 82.4
    assert "BRENT_M1" not in got                                          # 金十報價不混進期貨結算價
    assert len(obs) == 6


def test_backfill_accepts_only_daily_bars(session, cfg):
    def kline(args):
        if args["code"] == "XAUUSD":                                      # 日 K
            bars = [{"time": f"2026-11-{d:02d} 00:00:00", "close": 2600 + d} for d in range(10, 19)]
        else:                                                             # 小時 K：不寫
            bars = [{"time": f"2026-11-18 {h:02d}:00:00", "close": 80 + h / 10} for h in range(0, 9)]
        return {"data": {"code": args["code"], "klines": bars}}
    jin = jin10.Jin10(cfg, client_for(FakeServer({"get_kline": kline})))
    rows = {r["series"]: r for r in jin10.backfill_klines(session, cfg, jin, count=10)}
    assert rows["MACRO_XAUUSD"]["written"] == 9 and rows["MACRO_XAUUSD"]["median_gap_hours"] == 24
    assert rows["JIN10_UKOIL"]["written"] == 0 and "不是日 K" in rows["JIN10_UKOIL"]["note"]


def test_quote_job_skips_without_token(session, cfg, settings):
    result = jobs.run_now(cfg, settings, "PULL_JIN10_QUOTES")
    assert result["status"] == "skipped" and "JIN10_TOKEN" in result["message"]
