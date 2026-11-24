"""第四期：信號、事件、推演、報告、大模型接口。"""
import math
from datetime import date, timedelta

import numpy as np
import pytest

from crudelab import events, projection, reports, scenarios, signals
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import Obs, write_observations
from crudelab.llm import LLMNotConfigured, get_provider, status
from crudelab.llm.base import LLMError, LLMProvider
from crudelab.llm.guard import unknown_numbers
from crudelab.llm.tasks import extract_events, write_narrative
from crudelab.parameters import set_value

TODAY = date.today()


def put(session, code, dates, values):
    write_observations(session, [Obs(code, d, float(v)) for d, v in zip(dates, values, strict=True)], "manual")
    session.commit()


def trading_days(n: int) -> list[date]:
    out, d = [], TODAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def month_start(offset: int) -> date:
    index = TODAY.year * 12 + TODAY.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


# ------------------------------ 信號 ------------------------------

def test_signal_scores_follow_the_documented_formula(session, cfg):
    days = trading_days(100)
    m1 = [70.0] * 99 + [72.0]                                   # 月差最後一天從 1 跳到 3
    put(session, "BRENT_M1", days, m1)
    put(session, "BRENT_M3", days, [69.0] * 100)
    result = signals.compute(session, IndicatorEngine(session, cfg))
    spread = next(m for m in result["modules"] if m["module"] == "spread")
    brent = next(s for s in spread["signals"] if s["code"] == "SIG_BRENT_STRUCTURE")
    series = np.array([1.0] * 99 + [3.0])
    z = (3.0 - series.mean()) / series.std(ddof=1)
    assert brent["status"] == "ok" and brent["z"] == pytest.approx(z) and brent["score"] == 1.0     # z/2 封頂
    assert spread["signals_ok"] == 1 and spread["score"] == 1.0 and spread["label"] == "強偏多"
    assert next(s for s in spread["signals"] if s["code"] == "SIG_WTI_STRUCTURE")["status"] == "missing"
    # 其餘模組沒有數據：不出分，也不拖累總分
    assert [m["score"] for m in result["modules"] if m["module"] != "spread"] == [None, None, None, None]
    assert result["overall"]["score"] == 1.0 and result["overall"]["modules_ok"] == 1


def test_signal_needs_enough_history_and_respects_direction(session, cfg):
    days = trading_days(20)
    put(session, "BRENT_M1", days, [70 + i for i in range(20)])
    put(session, "BRENT_M3", days, [69.0] * 20)
    spread = signals.compute(session, IndicatorEngine(session, cfg))["modules"][0]
    assert spread["signals"][0]["status"] == "insufficient" and spread["score"] is None      # 少於 30 個點

    fridays = [TODAY - timedelta(days=7 * i) for i in range(6 * 52 + 2)][::-1]
    put(session, "EIA_CRUDE_STOCKS", fridays, [400_000 + (30_000 if i > 6 * 52 - 4 else 0) + (i % 7) * 100
                                               for i in range(len(fridays))])
    balance = signals.compute(session, IndicatorEngine(session, cfg))["modules"][1]
    stocks = next(s for s in balance["signals"] if s["code"] == "SIG_US_CRUDE_STOCKS")
    assert stocks["z"] > 0 and stocks["score"] < 0 and balance["label"] in ("偏空", "強偏空")   # 庫存偏高 → 利空


def test_geo_signal_comes_from_confirmed_events_only(session, cfg):
    events.create(session, cfg, title="航道受阻", category="chokepoint", status="ongoing", bias=2)
    events.create(session, cfg, title="傳聞", status="rumor", bias=2)
    events.create(session, cfg, title="模型抽的草稿", status="confirmed", bias=-2, draft=True, origin="llm")
    events.create(session, cfg, title="已結束", status="resolved", bias=-2)
    session.commit()
    result = signals.compute(session, IndicatorEngine(session, cfg))
    geo = next(m for m in result["modules"] if m["module"] == "geo")
    assert geo["score"] == 0.5 and [s["name_zh"] for s in geo["signals"]] == ["航道受阻"]
    set_value(session, "SIG_WEIGHT_GEO", 0)                       # 權重設為 0 → 不計入總分
    session.commit()
    assert signals.compute(session, IndicatorEngine(session, cfg))["overall"]["score"] is None


# ------------------------------ 事件與預案 ------------------------------

def test_event_validation_and_audit(session, cfg):
    e = events.create(session, cfg, title="  OPEC+ 會議  ", category="opec_policy", location_code="OPEC")
    assert e.title == "OPEC+ 會議"
    events.update(session, cfg, e.id, {"status": "resolved", "bias": -1})
    session.commit()
    assert e.status == "resolved" and e.bias == -1
    for bad, message in [({"bias": 3}, "方向判斷"), ({"status": "x"}, "狀態"), ({"category": "x"}, "類別"),
                         ({"location_code": "MARS"}, "地區"), ({"title": " "}, "標題"), ({"id": 9}, "不能修改")]:
        with pytest.raises(events.EventError, match=message):
            events.update(session, cfg, e.id, bad)
    with pytest.raises(events.EventError, match="沒有編號"):
        events.update(session, cfg, 999, {"bias": 0})


def test_playbook_turns_an_event_into_a_scenario(session, cfg):
    e = events.create(session, cfg, title="海峽受阻", category="chokepoint", status="ongoing", bias=2)
    draft = events.create(session, cfg, title="草稿", draft=True, origin="llm")
    with pytest.raises(events.EventError, match="先確認"):
        events.apply_playbook(session, cfg, draft.id, "CHOKEPOINT_DISRUPTION", TODAY)
    with pytest.raises(events.EventError, match="沒有名為"):
        events.apply_playbook(session, cfg, e.id, "NOPE", TODAY)
    scenario = events.apply_playbook(session, cfg, e.id, "CHOKEPOINT_DISRUPTION", date(2026, 12, 1),
                                     [{"target": "BAL_GLOBAL_S_OPEC", "delta": -2.0, "duration_days": 31}])
    session.commit()
    assert scenario.code == f"EVT_{e.id}" and e.scenario_code == scenario.code
    adj = scenarios.adjustments(session, scenario.code)[0]
    assert (adj.delta, adj.date_from, adj.date_to) == (-2.0, date(2026, 12, 1), date(2026, 12, 31))
    events.apply_playbook(session, cfg, e.id, "DEMAND_SHOCK", date(2026, 12, 1))         # 再套一個：沿用同一個情境
    assert len(scenarios.adjustments(session, scenario.code)) == 2
    with pytest.raises(events.EventError, match="既不是序列也不是指標"):
        events.apply_playbook(session, cfg, e.id, "DEMAND_SHOCK", TODAY, [{"target": "NOPE", "delta": 1}])


# ------------------------------ 推演 ------------------------------

@pytest.fixture()
def market(session, cfg):
    """250 個交易日、日波動恆為 ±1% 的 Brent，以及覆蓋未來四個月的全球平衡（每月過剩 0.5）。"""
    days = trading_days(250)
    prices, p = [], 80.0
    for i in range(250):
        p *= math.exp(0.01 if i % 2 else -0.01)
        prices.append(p)
    put(session, "BRENT_M1", days, prices)
    months = [month_start(k) for k in range(-1, 4)]
    for code, value in {"STEO_OPEC_SUPPLY": 33.0, "STEO_NONOPEC_SUPPLY": 70.0,
                        "STEO_OECD_DEMAND": 46.0, "STEO_NONOECD_DEMAND": 56.5}.items():
        put(session, code, months, [value] * 5)
    return prices[-1]


def test_projection_matches_hand_calculation(session, cfg, market):
    price = market
    scenarios.create(session, "TIGHT", "供應收緊")
    scenarios.add_adjustment(session, cfg, "TIGHT", "BAL_GLOBAL_S_OPEC", month_start(0), None, -1.0, "減產")
    p = projection.create(session, "測試推演")
    base_case = projection.cases(session, p.id)[0]
    projection.update_case(session, base_case.id, probability=0.6)
    projection.add_case(session, cfg, p.id, "收緊", "TIGHT", 0.4, price_adjustment=2.0, note="另加風險溢價")
    session.commit()

    blocked = projection.compute(session, cfg, p.id)                 # 還沒給敏感度：情境分支算不出，也不硬算
    assert blocked["cases"][1]["horizons"]["M1"]["central"] is None
    assert "缺參數 PROJ_PRICE_PER_STOCK" in blocked["cases"][1]["horizons"]["M1"]["reason"]
    assert blocked["expected"]["M1"] is None
    assert blocked["cases"][0]["horizons"]["M1"]["central"] == pytest.approx(price)

    set_value(session, "PROJ_PRICE_PER_STOCK", 8.0)
    session.commit()
    result = projection.compute(session, cfg, p.id)
    returns = np.array([0.01 if i % 2 else -0.01 for i in range(1, 250)])
    sigma = returns.std(ddof=1)
    assert result["volatility"]["daily"] == pytest.approx(sigma) and result["anchor"]["price"] == pytest.approx(price)
    base, tight = result["cases"]
    for code, trading, days in (("W1", 5, 7), ("M1", 21, 30), ("M3", 63, 91)):
        band = price * sigma * math.sqrt(trading)
        b, t = base["horizons"][code], tight["horizons"][code]
        assert (b["central"], b["low"], b["high"]) == pytest.approx((price, price - band, price + band))
        # 少供應 1 mb/d，持續整個期限：累庫 = −1 × 天數（百萬桶），供需效應 = −8 × 累庫
        assert t["balance_change"] == pytest.approx(-1.0) and t["stock_change"] == pytest.approx(-days)
        assert t["balance_effect"] == pytest.approx(8.0 * days)
        assert t["central"] == pytest.approx(price + 8.0 * days + 2.0)
        assert result["expected"][code] == pytest.approx(0.6 * price + 0.4 * (price + 8.0 * days + 2.0))
    assert tight["horizons"]["W1"]["periods"] == 1 and tight["horizons"]["M3"]["periods"] >= 3
    assert result["problems"] == []


def test_projection_reports_problems_instead_of_guessing(session, cfg, market):
    p = projection.create(session, "測試")
    projection.add_case(session, cfg, p.id, "多一個", None, 0.5)
    session.commit()
    result = projection.compute(session, cfg, p.id)
    assert result["total_probability"] == 1.5 and result["expected"] == {"W1": None, "M1": None, "M3": None}
    assert any("不等於 100%" in problem for problem in result["problems"])
    for kwargs, message in [({"probability": 1.5}, "0 到 1"), ({"scenario_code": "NOPE"}, "沒有名為")]:
        with pytest.raises(projection.ProjectionError, match=message):
            projection.add_case(session, cfg, p.id, "x", **{"scenario_code": None, "probability": 0.1, **kwargs})
    with pytest.raises(projection.ProjectionError, match="沒有編號"):
        projection.compute(session, cfg, 999)


def test_projection_without_price_history(session, cfg):
    p = projection.create(session, "空")
    session.commit()
    result = projection.compute(session, cfg, p.id)
    assert result["anchor"]["price"] is None and result["cases"][0]["horizons"]["M1"]["central"] is None
    assert any("沒有數據" in problem for problem in result["problems"])


# ------------------------------ 大模型接口 ------------------------------

class FakeProvider(LLMProvider):
    name, model = "fake", "fake-1"

    def __init__(self, reply: str):
        self.reply, self.calls = reply, []

    def complete(self, system, user, *, json=False, max_tokens=4000):
        self.calls.append({"system": system, "user": user, "json": json})
        return self.reply


def test_number_guard():
    reference = "Brent 當前 81.23 美元/桶，月差 +1.50，庫存 430,000 千桶，分位 66%，日期 2026-10-23。"
    assert unknown_numbers("Brent 在 81.23，月差 1.5，庫存 430000，分位 66%。", reference) == []
    assert unknown_numbers("Brent 約 81.2 美元，四捨五入是 81。", reference) == []          # 四捨五入到更少位數可以
    assert unknown_numbers("預計升至 85 美元，漲幅 4.6%。", reference) == ["85", "4.6"]
    assert unknown_numbers("三大機構、第 2 季、100% 確定。", reference) == []


def test_extraction_validates_and_never_trusts_the_model(cfg):
    reply = """```json
{"events": [
  {"title": "海峽通行受阻", "category": "chokepoint", "location_code": "MEG", "status": "ongoing", "bias": 2,
   "started_on": "2026-11-10", "summary": "摘要"},
  {"title": "亂填的", "category": "aliens", "location_code": "MARS", "status": "weird", "bias": 1},
  {"title": "", "bias": 0},
  {"title": "方向超界", "bias": 9}
]}
```"""
    provider = FakeProvider(reply)
    found, dropped = extract_events(provider, "一段新聞", cfg)
    assert dropped == 2 and [e.title for e in found] == ["海峽通行受阻", "亂填的"]
    assert (found[1].category, found[1].location_code, found[1].status) == ("other", None, "rumor")
    assert provider.calls[0]["json"] is True and "MEG（中東海灣）" in provider.calls[0]["system"]
    with pytest.raises(LLMError, match="無法解析"):
        extract_events(FakeProvider("抱歉，我不確定"), "x", cfg)


def test_narrative_is_checked_against_the_report():
    markdown = "Brent 當前 81.23 美元/桶。"
    good = write_narrative(FakeProvider("Brent 在 81.23 附近。"), markdown, "日報")
    assert good["guard_passed"] and good["model"] == "fake-1"
    bad = write_narrative(FakeProvider("Brent 將升至 90。"), markdown, "日報")
    assert not bad["guard_passed"] and bad["unknown_numbers"] == ["90"]


def test_provider_selection_from_environment(monkeypatch):
    for key in ("CRUDELAB_LLM_PROVIDER", "CRUDELAB_LLM_MODEL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert status()["configured"] is False
    with pytest.raises(LLMNotConfigured, match="還沒有選擇大模型"):
        get_provider()
    monkeypatch.setenv("CRUDELAB_LLM_PROVIDER", "deepseek")
    with pytest.raises(LLMNotConfigured, match="還沒有選擇 DeepSeek 的模型"):
        get_provider()                                           # 不替使用者猜型號：舊的預設名已經被停用過
    monkeypatch.setenv("CRUDELAB_LLM_MODEL", "some-model")
    with pytest.raises(LLMNotConfigured, match="DEEPSEEK_API_KEY"):
        get_provider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert (get_provider().name, get_provider().model) == ("deepseek", "some-model")
    monkeypatch.setenv("CRUDELAB_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert get_provider().model == "some-model"
    monkeypatch.setenv("CRUDELAB_LLM_PROVIDER", "nope")
    assert "不認識" in status()["message"]


# ------------------------------ 報告與接口 ------------------------------

def test_report_is_a_snapshot_with_every_section(loaded, cfg):
    events.create(loaded, cfg, title="航道受阻", category="chokepoint", status="ongoing", bias=2)
    projection.create(loaded, "演示推演")
    loaded.commit()
    report = reports.generate(loaded, cfg, "weekly")
    loaded.commit()
    text = report.markdown
    for heading in ("# 原油市場周報", "## 方向信號", "## 地域價差", "## 供需平衡", "## 煉廠盈利", "## 地緣事件",
                    "## 價格推演"):
        assert heading in text
    assert "航道受阻" in text and "缺參數：FLAT_TD3C" in text and "待核對" in text
    brent_wti = next(i for i in report.facts["indicators"]["spread"]["基準價差"] if i["code"] == "SPR_BRENT_WTI")
    assert f"{brent_wti['last_value']:,.2f}" in text
    assert unknown_numbers(text, text) == []
    assert report.facts["data_date"] == "2026-10-23" and "數據截至 2026-10-23" in text   # 不被月度預測期帶偏

    set_value(loaded, "BBL_PER_T_GASOIL", 9.0)                    # 之後改參數，不影響已生成的報告
    loaded.commit()
    assert loaded.get(type(report), report.id).markdown == text
    with pytest.raises(reports.ReportError, match="報告類型"):
        reports.generate(loaded, cfg, "yearly")


def test_phase4_api(client, admin, sample_path, monkeypatch):
    monkeypatch.delenv("CRUDELAB_LLM_PROVIDER", raising=False)
    with sample_path.open("rb") as fh:
        batch = admin.post("/api/imports/preview", files={"file": ("s.xlsx", fh)}, data={"source_code": "demo"}).json()
    admin.post(f"/api/imports/{batch['batch_id']}/commit", json={})

    sig = client.get("/api/signals").json()
    assert [m["module"] for m in sig["modules"]] == ["spread", "balance", "refinery", "positioning", "geo"]
    assert sig["overall"]["label"] in ("強偏多", "偏多", "中性", "偏空", "強偏空")

    event = client.post("/api/events", json={"title": "海峽受阻", "category": "chokepoint", "status": "ongoing",
                                             "bias": 2, "location_code": "MEG"}).json()
    assert event["location_name"] == "中東海灣" and event["draft"] is False
    assert client.post("/api/events", json={"title": "x", "bias": 5}).status_code == 400
    assert client.patch(f"/api/events/{event['id']}", json={"bias": 1}).json()["bias"] == 1
    playbooks = client.get("/api/playbooks").json()
    assert playbooks[0]["impacts"][0]["unit"] == "mb/d"
    applied = client.post(f"/api/events/{event['id']}/apply-playbook",
                          json={"playbook": "CHOKEPOINT_DISRUPTION", "start": "2026-12-01"}).json()
    assert applied == {"scenario_code": f"EVT_{event['id']}", "adjustments": 1}
    assert client.get("/api/events").json()[0]["adjustments"] == 1
    table = client.get(f"/api/balances/GLOBAL?scenario=EVT_{event['id']}").json()        # 事件的影響能在平衡表裡看
    opec = next(r for r in table["rows"] if r["indicator"] == "BAL_GLOBAL_S_OPEC")
    assert any(abs(v - b + 3.0) < 1e-9 for v, b in zip(opec["values"], opec["base_values"], strict=True))

    assert client.get("/api/llm").json()["configured"] is False
    assert client.post("/api/events/extract", json={"text": "一段足夠長的新聞文字內容"}).status_code == 409

    pid = client.post("/api/projections", json={"name_zh": "十一月推演"}).json()["id"]
    case = client.post(f"/api/projections/{pid}/cases", json={
        "label": "海峽受阻", "scenario_code": f"EVT_{event['id']}", "probability": 0.3, "price_adjustment": 3}).json()
    base_id = client.get(f"/api/projections/{pid}").json()["cases"][0]["id"]
    assert client.patch(f"/api/projection-cases/{base_id}", json={"probability": 0.7}).status_code == 200
    result = client.get(f"/api/projections/{pid}").json()
    assert result["total_probability"] == pytest.approx(1.0) and len(result["cases"]) == 2
    assert client.patch(f"/api/projection-cases/{case['id']}", json={"probability": 2}).status_code == 400
    assert client.get("/api/projections/999").status_code == 404
    assert client.get("/api/projections").json()[0]["cases"] == 2

    report = client.post("/api/reports", json={"kind": "daily"}).json()
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert "海峽受阻" in detail["markdown"] and "十一月推演" in detail["markdown"] and detail["narrative"] is None
    download = client.get(f"/api/reports/{report['id']}/markdown")
    assert download.headers["content-type"].startswith("text/markdown") and download.text == detail["markdown"]
    assert client.post(f"/api/reports/{report['id']}/narrative").status_code == 409      # 沒配大模型
    assert client.post("/api/reports", json={"kind": "yearly"}).status_code == 400
    assert client.delete(f"/api/projection-cases/{case['id']}").status_code == 200
    assert client.get("/api/reports/999").status_code == 404


# ------------------------------ 對話式查詢 ------------------------------

class ScriptedProvider(LLMProvider):
    """按劇本逐步回覆的假模型，並記下每一步收到的內容。"""
    name, model = "fake", "fake-1"

    def __init__(self, *replies: str):
        self.replies, self.prompts = list(replies), []

    def complete(self, system, user, *, json=False, max_tokens=4000):
        self.prompts.append({"system": system, "user": user})
        return self.replies.pop(0)


def step(tool, **args):
    import json
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)


def final(text):
    import json
    return json.dumps({"answer": text}, ensure_ascii=False)


def test_chat_looks_up_data_and_answer_is_checked(loaded, cfg):
    from crudelab.llm import chat
    value = IndicatorEngine(loaded, cfg).summary("SPR_BRENT_WTI")["last_value"]
    provider = ScriptedProvider(
        step("search", query="Brent"),
        step("get_values", code="SPR_BRENT_WTI", last=3),
        final(f"Brent − WTI 最新為 {value:.2f} 美元/桶（2026-10-23）。"))
    result = chat.answer(provider, loaded, cfg, "現在 Brent 和 WTI 的價差是多少？")
    assert result["guard_passed"] and [s["tool"] for s in result["steps"]] == ["search", "get_values"]
    assert any(m["code"] == "SPR_BRENT_WTI" for m in result["steps"][0]["result"]["matches"])
    assert result["steps"][1]["result"]["last_value"] == pytest.approx(value, abs=1e-4)
    assert "get_values" in provider.prompts[2]["user"] and "不要自己做算術" in provider.prompts[0]["system"]

    made_up = ScriptedProvider(final("Brent 大約在 88.5 美元。"))          # 沒查數就報數字
    bad = chat.answer(made_up, loaded, cfg, "Brent 多少錢？")
    assert not bad["guard_passed"] and bad["unknown_numbers"] == ["88.5"] and bad["steps"] == []


def test_chat_what_if_uses_the_engine_not_the_model(loaded, cfg):
    from crudelab.llm import chat
    set_value(loaded, "PROJ_PRICE_PER_STOCK", 5.0)
    loaded.commit()
    start = month_start(0).isoformat()
    provider = ScriptedProvider(
        step("what_if", adjustments=[{"target": "BAL_GLOBAL_S_OPEC", "delta": -2, "date_from": start}]),
        final("已試算。"))
    result = chat.answer(provider, loaded, cfg, "如果 OPEC 從本月起少供應 2 百萬桶/日會怎樣？")
    what_if = result["steps"][0]["result"]
    rows = what_if["compared"][0]["rows"]
    assert what_if["compared"][0]["code"] == "BAL_GLOBAL_BALANCE" and all(r["change"] == -2 for r in rows)
    month = what_if["price"]["horizons"]["M1"]
    assert month["balance_change"] == -2 and month["stock_change"] == -60                # −2 mb/d × 30 天
    assert month["balance_effect"] == 300                                                  # −5 × −60
    assert month["central"] == pytest.approx(what_if["price"]["price"] + 300, abs=1e-3)
    assert len(loaded.new) == 0 and len(loaded.dirty) == 0                                # 沒有寫入任何東西

    # 之前的問答會帶進下一輪
    again = ScriptedProvider(final("好的。"))
    chat.answer(again, loaded, cfg, "那三個月呢？", [{"role": "user", "content": "先前的問題"},
                                                     {"role": "assistant", "content": "先前的回答"}])
    prompt = again.prompts[0]["user"]
    assert "先前的回答" in prompt and prompt.rstrip().endswith("請輸出下一步的 JSON。")


def test_chat_handles_bad_tool_calls_and_gives_up_cleanly(loaded, cfg):
    from crudelab.llm import chat
    provider = ScriptedProvider(step("delete_everything"), step("get_values", code="NOPE"),
                                step("get_values", wrong_arg=1), step("list_indicators", module="x"),
                                "這不是 JSON，直接作答。")
    result = chat.answer(provider, loaded, cfg, "隨便問問")
    errors = [s["result"]["error"] for s in result["steps"]]
    assert "沒有名為 delete_everything 的工具" in errors[0] and "先用 search" in errors[1]
    assert "參數不對" in errors[2] and "module 必須是" in errors[3]
    assert result["answer"] == "這不是 JSON，直接作答。"
    assert set(chat.TOOLS) == {"search", "get_values", "list_indicators", "get_balance", "get_refinery", "get_signals",
                               "list_events", "list_scenarios", "get_projection", "get_fair_value", "get_parameter",
                               "what_if"}

    looping = ScriptedProvider(*[step("get_signals")] * (chat.MAX_STEPS + 1))
    with pytest.raises(LLMError, match="仍沒有得出回答"):
        chat.answer(looping, loaded, cfg, "?")


def test_every_chat_tool_runs_on_real_data(loaded, cfg):
    from crudelab.llm import chat
    ctx = chat._Ctx(loaded, cfg, IndicatorEngine(loaded, cfg))
    events.create(loaded, cfg, title="事件", status="ongoing", bias=1)
    scenarios.create(loaded, "S", "情境")
    projection.create(loaded, "推演")
    loaded.commit()
    calls = {"search": {"query": "柴油"}, "get_values": {"code": "EIA_CRUDE_STOCKS"},
             "list_indicators": {"module": "spread"}, "get_balance": {"code": "GLOBAL", "scenario": "S"},
             "get_refinery": {"code": "CN_FUEL_SD"}, "get_signals": {},
             "list_events": {}, "list_scenarios": {}, "get_projection": {}, "get_parameter": {"key": "CN_VAT_RATE"},
             "what_if": {"parameters": {"RM_CN_FUEL_SD_C_OPEX": 400}, "watch": ["RM_CN_FUEL_SD_MARGIN"]}}
    import json
    for name, args in calls.items():
        result = chat._r(chat.TOOLS[name][0](ctx, **args))
        assert json.dumps(result, ensure_ascii=False, default=str)          # 結果能序列化給模型
    margin = chat._r(chat.tool_what_if(ctx, **calls["what_if"]))["compared"][0]["rows"][0]
    assert margin["change"] == pytest.approx(-100)                           # 加工費 +100 → 毛利 −100
    assert chat.tool_get_refinery(ctx, "CN_FUEL_SD")["components"][-1]["label"] == "毛利"
    with pytest.raises(chat.ToolError, match="至少要給一項假設"):
        chat.tool_what_if(ctx)


def test_chat_api(client, monkeypatch):
    monkeypatch.delenv("CRUDELAB_LLM_PROVIDER", raising=False)
    assert len(client.get("/api/chat/tools").json()) == 12
    assert client.post("/api/chat", json={"question": "Brent 多少？"}).status_code == 409      # 沒配大模型
    assert client.post("/api/chat", json={"question": ""}).status_code == 422


# ------------------------------ 地緣事件的機率與推演分支 ------------------------------

def test_event_probability_validation(session, cfg):
    with pytest.raises(events.EventError, match="機率"):
        events.create(session, cfg, title="x", probability=1.5)
    with pytest.raises(events.EventError, match="溢價"):
        events.create(session, cfg, title="x", price_premium=500)


def test_sync_events_into_projection(loaded, cfg):
    session = loaded
    a = events.create(session, cfg, title="航道風險", status="confirmed", probability=0.2, price_premium=5.0)
    events.create(session, cfg, title="只有機率", status="rumor", probability=0.1)                  # 沒量化：跳過
    events.create(session, cfg, title="草稿", status="confirmed", probability=0.3, price_premium=2, draft=True)
    events.create(session, cfg, title="已結束", status="resolved", probability=0.3, price_premium=2)
    p = projection.create(session, "地緣推演")
    session.commit()

    r = projection.sync_events(session, cfg, p.id)
    session.commit()
    assert r["events"] == 1 and r["baseline_probability"] == pytest.approx(0.8) and r["skipped"] == ["只有機率"]
    result = projection.compute(session, cfg, p.id)
    labels = {c["label"]: c for c in result["cases"]}
    assert labels["基準"]["probability"] == pytest.approx(0.8)
    event_case = labels["地緣：航道風險"]
    assert event_case["probability"] == pytest.approx(0.2) and event_case["event_id"] == a.id
    price = result["anchor"]["price"]
    assert result["expected"]["M1"] == pytest.approx(price + 0.2 * 5.0)                            # 0.8·P + 0.2·(P + 5)

    events.update(session, cfg, a.id, {"probability": 0.4})
    session.commit()
    assert any("請重新同步" in x for x in projection.compute(session, cfg, p.id)["problems"])
    projection.sync_events(session, cfg, p.id)                                                    # 反覆同步不重複
    session.commit()
    again = projection.compute(session, cfg, p.id)
    assert len(again["cases"]) == 2 and again["total_probability"] == pytest.approx(1.0)
    assert not any("請重新同步" in x for x in again["problems"])

    events.update(session, cfg, a.id, {"probability": 0.9})
    projection.add_case(session, cfg, p.id, "手動情境", scenarios.create(session, "MANUAL_X", "手動").code, 0.3)
    session.commit()
    with pytest.raises(projection.ProjectionError, match="超過 100%"):
        projection.sync_events(session, cfg, p.id)


def test_event_expected_impact(loaded, cfg, client):
    session = loaded
    e = events.create(session, cfg, title="風險溢價", status="ongoing", probability=0.25, price_premium=4.0)
    events.create(session, cfg, title="沒量化", status="ongoing", probability=0.5)
    session.commit()
    impacts = projection.event_impacts(session, cfg)
    cell = impacts["events"][e.id]["M1"]
    assert cell["if_happens"] == pytest.approx(4.0) and cell["expected"] == pytest.approx(1.0)
    assert len(impacts["events"]) == 1
    body = client.get("/api/events/impacts").json()
    assert body["events"][str(e.id)]["W1"]["expected"] == pytest.approx(1.0)


def test_balance_period_starting_on_the_horizon_end_is_not_counted():
    """2026-11-24 發現：1 周期限的終點正好是 12-01，12 月那一期和期限重疊 0 天，卻被算進期數和平均。"""
    import pandas as pd

    from crudelab.projection import _balance_change
    idx = pd.to_datetime(["2026-11-01", "2026-12-01"])
    base = pd.Series([0.0, 0.0], index=idx)
    scenario = pd.Series([-1.0, -5.0], index=idx)                         # 12 月的變化大很多
    today, end = pd.Timestamp("2026-11-24"), pd.Timestamp("2026-12-01")
    change, stock, n = _balance_change(base, scenario, today, end, 31)
    assert n == 1 and change == pytest.approx(-1.0) and stock == pytest.approx(-7.0)
    change, stock, n = _balance_change(base, scenario, today, end + pd.Timedelta(days=1), 31)
    assert n == 2 and stock == pytest.approx(-7.0 - 5.0)                    # 多一天就跨進 12 月
