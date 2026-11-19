"""金十數據：MCP 客戶端（握手、會話、SSE、錯誤）、額度控制、財經日曆的同步與超預期。用模擬的 MCP 服務器測試。"""
import hashlib
import json
from datetime import date, datetime

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
            return self.reply(body, {"contents": [{"uri": "quote://codes", "text": json.dumps(CODES)}]})
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


CODES = {"data": [{"code": "UKOIL", "name": "布伦特原油"}, {"code": "USDX", "name": "美元指数"}]}


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
    assert client.read_resource("quote://codes") == CODES


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
                         "get_kline": minute_klines,
                         "list_calendar": lambda a: {"data": calendar_rows()},
                         "list_flash": lambda a: {"data": {"items": [{"id": 1, "content": "沙特宣布延长减产"}],
                                                           "has_more": False}},
                         "search_flash": lambda a: {"data": {"items": []}}})
    monkeypatch.setenv("JIN10_TOKEN", TOKEN)
    monkeypatch.setattr(jin10, "MCPClient", lambda *args, **kw: client_for(server))
    assert cli.main(["jin10", "probe"]) == 0
    out = capsys.readouterr().out
    assert "已連接" in out and "EIA原油库存" in out and "第一條的字段：['content', 'id']" in out
    assert "缺 " in out and "USDCNH" in out                       # 配置裡的代碼不在列表裡會點名
    assert "16:59 的 K 線" in out and "翻頁用" in out
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

BASE = {"UKOIL": 82.0, "USOIL": 78.0, "XAUUSD": 2650.0, "USDCNH": 7.25, "EURUSD": 1.05, "USDJPY": 154.0}
HOLIDAY = date(2026, 11, 26)                                             # 感恩節：當天沒有 K 線


def minute_klines(args):
    """模擬金十 get_kline：1 分鐘 K 線，回傳截止到 time（秒）的最後 count 根；假期那天整天沒有 K 線。"""
    from datetime import UTC, datetime, timedelta
    end = datetime.fromtimestamp(args.get("time") or 1795058460, UTC).replace(second=0)
    bars, t = [], end
    while len(bars) < args.get("count", 3):
        if jin10.trade_date(t.replace(tzinfo=None)) not in (None, HOLIDAY):
            bars.append({"time": int(t.timestamp()), "close": f"{BASE[args['code']] + t.day / 100:.4f}"})
            t -= timedelta(minutes=1)
        else:
            t -= timedelta(hours=1)
    return {"data": {"code": args["code"], "klines": bars}}


def test_close_on_uses_new_york_1659_and_skips_holidays(cfg):
    from datetime import datetime
    calls = []
    jin = jin10.Jin10(cfg, client_for(FakeServer({"get_kline": lambda a: calls.append(a) or minute_klines(a)})))
    value, when = jin10.close_on(jin, "UKOIL", date(2026, 11, 18))
    assert when == datetime(2026, 11, 18, 21, 59) and value == 82.18             # 冬令時：紐約 16:59 = UTC 21:59
    assert calls[0]["time"] == jin10.close_stamp(date(2026, 11, 18)) and isinstance(calls[0]["time"], int)
    assert jin10.close_on(jin, "UKOIL", HOLIDAY) is None                          # 假期：拿到的是前一天的 K 線，不記
    assert jin10.close_stamp(date(2026, 7, 1)) == int(datetime(2026, 7, 1, 20, 59).timestamp()
                                                      - datetime(1970, 1, 1).timestamp())   # 夏令時：UTC 20:59
    days = jin10.completed_trade_days(datetime(2026, 11, 23, 22, 30), 3)          # 紐約週一 17:30
    assert days == [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)]
    assert jin10.completed_trade_days(datetime(2026, 11, 23, 21, 0), 1) == [date(2026, 11, 20)]   # 週一還沒收盤


def test_quote_connector_writes_daily_closes(session, cfg):
    from datetime import datetime

    from crudelab.connectors.jin10_quotes import Jin10QuoteConnector
    from crudelab.models import Series
    server = FakeServer({"get_kline": minute_klines})
    connector = Jin10QuoteConnector(cfg, client_for(server), now=datetime(2026, 11, 27, 22, 30))   # 紐約週五收盤後
    obs = connector.fetch(list(session.scalars(select(Series))), None, None)
    got = {(o.series_code, o.obs_date): o.value for o in obs}
    assert got[("MACRO_XAUUSD", date(2026, 11, 27))] == 2650.27
    assert ("MACRO_XAUUSD", HOLIDAY) not in got and ("MACRO_XAUUSD", date(2026, 11, 25)) in got   # 跳過感恩節
    assert not any(code == "BRENT_M1" for code, _ in got)                 # 金十報價不混進期貨結算價
    assert len(obs) == 6 * 2


def test_times_dates_and_flash_ids():
    from datetime import datetime
    assert jin10.parse_time("2026-11-19T11:21:25+08:00") == datetime(2026, 11, 19, 3, 21, 25)
    assert jin10.parse_time("2026-11-19T03:21:25Z") == datetime(2026, 11, 19, 3, 21, 25)
    assert jin10.parse_time("2026-11-19 11:21:25") == datetime(2026, 11, 19, 3, 21, 25)     # 不帶時區 = 北京
    assert jin10.parse_time(1795058460) == datetime(2026, 11, 19, 3, 21)                     # K 線：秒
    assert jin10.parse_time(1795058460000) == datetime(2026, 11, 19, 3, 21)                  # 毫秒
    assert jin10.parse_time("1795058460") == datetime(2026, 11, 19, 3, 21)
    assert jin10.parse_time(True) is None and jin10.parse_time("昨天") is None
    assert jin10.parse_time("20261119") == datetime(2026, 11, 18, 16, 0)                    # 8 位 = 日期，不是時間戳
    assert jin10.parse_time(0) is None and jin10.parse_time("12345") is None                # 太小：不當時間戳
    assert jin10.trade_date(datetime(2026, 11, 19, 3, 21)) == date(2026, 11, 18)            # 紐約週三晚上
    assert jin10.trade_date(datetime(2026, 11, 21, 18, 0)) is None                           # 紐約週六
    when = datetime(2026, 11, 19, 3, 0)
    a = jin10.flash_id({"url": "https://flash.jin10.com/detail/1"}, when, "甲")
    assert a == jin10.flash_id({"url": "https://flash.jin10.com/detail/1"}, when, "內容改過")   # 同一 url = 同一條
    assert a != jin10.flash_id({"url": "https://flash.jin10.com/detail/2"}, when, "甲")
    assert jin10.flash_id({"id": 7}, when, "甲") == "7" and len(jin10.flash_id({}, when, "甲")) == 40


def test_backfill_closes_checks_budget_first(session, cfg):
    from datetime import datetime
    calls = []
    jin = jin10.Jin10(cfg, client_for(FakeServer({"get_kline": lambda a: calls.append(a) or minute_klines(a)})))
    now = datetime(2026, 11, 27, 22, 30)
    result = jin10.backfill_closes(session, cfg, jin, days=10, now=now)
    rows = {r["series"]: r for r in result["rows"]}
    assert result["calls"] == 60 == len(calls) and result["last"] == date(2026, 11, 27)
    assert rows["MACRO_XAUUSD"]["closes"] == 9 and rows["MACRO_XAUUSD"]["no_trading"] == 1
    assert rows["MACRO_XAUUSD"]["written"] == 9 and rows["MACRO_XAUUSD"]["earliest"] == date(2026, 11, 16)
    session.commit()
    again = jin10.backfill_closes(session, cfg, jin, days=10, now=now)    # 再跑一次：不重複寫
    assert all(r["written"] == 0 for r in again["rows"])
    session.commit()
    import dataclasses
    small = dataclasses.replace(cfg, jin10=cfg.jin10.model_copy(update={"max_calls_per_tool_per_day": 150}))
    # 已用 120 次，只剩 30 次
    before = len(calls)
    with pytest.raises(jin10.Jin10LimitReached, match="最多可以補 5 天"):
        jin10.backfill_closes(session, small, jin, days=10, now=now)
    assert len(calls) == before                                           # 額度不夠：一次都不調用


def test_quote_job_skips_without_token(session, cfg, settings):
    result = jobs.run_now(cfg, settings, "PULL_JIN10_QUOTES")
    assert result["status"] == "skipped" and "JIN10_TOKEN" in result["message"]


# ------------------------------ 簡繁 ------------------------------

def test_to_simplified_and_overrides():
    from crudelab import zh
    assert zh.to_simplified("歐佩克減產") == "欧佩克减产"
    assert zh.to_simplified("欧佩克减产") == "欧佩克减产"                 # 已經是簡體：不變
    assert zh.to_simplified("EIA原油庫存(萬桶)") == "EIA原油库存(万桶)"
    assert zh.to_simplified("日圓走弱") == "日圆走弱"                     # 逐字轉換轉不出用詞
    assert zh.to_simplified("日圓走弱", {"日圓": "日元"}) == "日元走弱"    # 替換表兩邊寫繁體也行
    issues = zh.traditional_issues({"關鍵詞": ["原油", "歐佩克", "WTI"]})
    assert issues == ["關鍵詞：歐佩克 → 應寫成 欧佩克"]


def test_check_flags_traditional_keywords(cfg, monkeypatch, capsys):
    import dataclasses

    from crudelab import cli, config
    rules = [r.model_copy(update={"require": "萬桶"}) for r in cfg.jin10.calendar_rules[:1]]
    bad = dataclasses.replace(cfg, jin10=cfg.jin10.model_copy(update={
        "flash_keywords": ["原油", "歐佩克", "日圓"], "calendar_rules": rules}))
    monkeypatch.setattr(config, "load_config", lambda *_: bad)
    assert cli.main(["check"]) == 1
    err = capsys.readouterr().err
    assert "歐佩克 → 應寫成 欧佩克" in err and "日圓 → 應寫成 日元" in err and "萬桶 → 應寫成 万桶" in err
    assert "原油 →" not in err
    monkeypatch.setattr(config, "load_config", lambda *_: cfg)            # 現在的配置全是簡體
    assert cli.main(["check"]) == 0


def test_probe_reports_keyword_hits_and_script_search(session, cfg, monkeypatch, capsys):
    from crudelab import cli
    searched = []

    def search(args):
        searched.append(args["keyword"])
        hit = {"content": "欧佩克+会议推迟", "time": "2026-11-19T10:00:00+08:00"}
        items = [hit] if args["keyword"] == "欧佩克" else []
        return {"data": {"items": items}}
    server = FakeServer({"get_quote": lambda a: {"data": {"code": "UKOIL", "close": "82.5"}},
                         "get_kline": minute_klines, "list_calendar": lambda a: {"data": calendar_rows()},
                         "list_flash": lambda a: {"data": {"items": [
                             {"content": "沙特宣布延长原油减产", "time": "2026-11-19T10:00:00+08:00", "url": "u1"},
                             {"content": "火山引擎上线新模型", "time": "2026-11-19T10:01:00+08:00", "url": "u2"}],
                             "has_more": False, "next_cursor": None}},
                         "search_flash": search})
    monkeypatch.setenv("JIN10_TOKEN", TOKEN)
    monkeypatch.setattr(jin10, "MCPClient", lambda *args, **kw: client_for(server))
    assert cli.main(["jin10", "probe"]) == 0
    out = capsys.readouterr().out
    assert "2 條裡命中 1 條" in out and "沙特" in out and "火山引擎" not in out.split("命中 1 條")[1]
    assert searched == ["欧佩克", "歐佩克"]
    assert "search_flash（簡體：欧佩克）\n  1 條" in out and "search_flash（繁體：歐佩克）\n  0 條" in out


def test_migration_rekeys_flashes_by_url_and_merges_duplicates(tmp_path):
    """0.29.0 用時間 + 內容雜湊當快訊編號，之後改用網址：升級時要換編號、合併已經重複的，下次同步不會再重複收錄。"""
    import sqlalchemy as sa
    from alembic import command

    from crudelab import db
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    alembic = db.alembic_config()
    with engine.begin() as conn:
        alembic.attributes["connection"] = conn
        command.upgrade(alembic, "0014")
    rows = [  # (ext_id, url, first_seen_at, extracted_at, drafts)
        ("oldhash1", "https://flash.jin10.com/detail/1", "2026-11-19 01:00:00", "2026-11-19 02:00:00", 2),
        ("u:" + hashlib.sha256(b"https://flash.jin10.com/detail/1").hexdigest()[:40],
         "https://flash.jin10.com/detail/1", "2026-11-19 03:00:00", None, 0),      # 新版又收錄了一次：重複
        ("oldhash2", "https://flash.jin10.com/detail/2", "2026-11-19 01:00:00", None, 0),
        ("oldhash3", None, "2026-11-19 01:00:00", None, 0),                       # 沒有網址：不動
    ]
    with engine.begin() as conn:
        for ext, url, seen, extracted, drafts in rows:
            conn.execute(sa.text(
                "INSERT INTO news_flash (source, ext_id, time, content, url, keywords, important, first_seen_at, "
                "extracted_at, drafts) VALUES ('jin10', :e, '2026-11-19 00:00:00', '原油', :u, '[]', 0, :s, :x, :d)"),
                {"e": ext, "u": url, "s": seen, "x": extracted, "d": drafts})
    db.migrate(engine)
    with engine.connect() as conn:
        got = conn.execute(sa.text("SELECT ext_id, url, extracted_at, drafts FROM news_flash ORDER BY id")).all()
    when = datetime(2026, 11, 19)
    assert len(got) == 3
    assert got[0][0] == jin10.flash_id({"url": "https://flash.jin10.com/detail/1"}, when, "")
    assert got[0][2] is not None and got[0][3] == 2                               # 留下抽取過草稿的那條
    assert got[1][0] == jin10.flash_id({"url": "https://flash.jin10.com/detail/2"}, when, "")
    assert got[2][0] == "oldhash3"
