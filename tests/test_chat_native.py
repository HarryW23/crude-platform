"""問答用原生工具調用（#42）。

三層：
  1. 工具的參數說明（JSON Schema）和函數一一對應
  2. 原生循環：一輪調幾個工具、參數錯誤交還模型、步數上限、數字核對、選協議
  3. 線路格式：chat completions 用假的 httpx；Claude 用真的 anthropic SDK 打到本機的假服務器，
     確認發出去的請求（工具定義、tool_result、原樣帶回的助手內容、後備 beta）是 SDK 和接口要的樣子
"""
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from crudelab.indicators import IndicatorEngine
from crudelab.llm import chat
from crudelab.llm.base import LLMError, LLMProvider, ToolCall, ToolTurn
from crudelab.llm.limits import LimitedProvider
from crudelab.llm.providers import OpenAICompatibleProvider
from crudelab.schema import LLMSettings

# ------------------------------ 1. 參數說明 ------------------------------


def test_schemas_match_the_tool_functions():
    for name, (func, _) in chat.TOOLS.items():
        params = list(inspect.signature(func).parameters.values())[1:]            # 去掉 ctx
        schema = chat.SCHEMAS[name]
        assert set(schema["properties"]) == {p.name for p in params}, name
        assert set(schema["required"]) == {p.name for p in params if p.default is inspect.Parameter.empty}, name
        assert schema["additionalProperties"] is False
    specs = chat.tool_specs()
    assert [s.name for s in specs] == list(chat.TOOLS) and all(" — " not in s.description for s in specs)


# ------------------------------ 2. 原生循環 ------------------------------

class NativeScript(LLMProvider):
    """按劇本逐輪回覆的原生工具調用假模型，記下每輪收到的消息。"""
    name, model, supports_tools = "fake", "fake-native", True

    def __init__(self, *turns: ToolTurn):
        self.turns, self.seen = list(turns), []

    def complete(self, *a, **k):
        raise AssertionError("原生協議不應該走 complete")

    def chat_tools(self, system, messages, tools, *, max_tokens=4000):
        self.seen.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        return self.turns.pop(0)


def calls(*items) -> ToolTurn:
    return ToolTurn(text="", calls=[ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(items)],
                    raw={"marker": len(items)})


def test_native_loop_parallel_calls_and_guard(loaded, cfg):
    value = IndicatorEngine(loaded, cfg).summary("SPR_BRENT_WTI")["last_value"]
    provider = NativeScript(
        calls(("search", {"query": "Brent"}), ("get_values", {"code": "SPR_BRENT_WTI", "last": 3})),
        ToolTurn(text=f"Brent − WTI 最新為 {value:.2f} 美元/桶。"))
    history = [{"role": "user", "content": "先前的問題"}, {"role": "assistant", "content": "先前的回答"}]
    result = chat.answer(provider, loaded, cfg, "價差多少？", history, protocol="native")
    assert result["protocol"] == "native" and result["guard_passed"]
    assert [s["tool"] for s in result["steps"]] == ["search", "get_values"]
    first, second = provider.seen
    assert [m["role"] for m in first["messages"]] == ["user", "assistant", "user"]
    assert first["messages"][-1]["text"] == "價差多少？" and "JSON" not in first["system"]
    assert {t.name for t in first["tools"]} == set(chat.TOOLS)
    raw, results = second["messages"][-2:]                       # 助手這一輪原樣帶回，兩個結果一次交回
    assert raw == {"role": "assistant", "raw": {"marker": 2}} and results["role"] == "tool_results"
    assert [r[0] for r in results["results"]] == ["c0", "c1"] and not any(r[2] for r in results["results"])
    assert json.loads(results["results"][1][1])["code"] == "SPR_BRENT_WTI"

    made_up = NativeScript(ToolTurn(text="Brent 大約在 88.5 美元。"))
    bad = chat.answer(made_up, loaded, cfg, "Brent 多少錢？", protocol="native")
    assert not bad["guard_passed"] and bad["unknown_numbers"] == ["88.5"]


def test_native_bad_calls_go_back_to_the_model_and_steps_are_capped(loaded, cfg):
    broken = ToolTurn(text="", calls=[ToolCall("a", "get_values", None, "參數不是合法的 JSON"),
                                      ToolCall("b", "delete_everything", {}),
                                      ToolCall("c", "get_values", {"wrong": 1})], raw="r1")
    provider = NativeScript(broken, ToolTurn(text="好的。"))
    result = chat.answer(provider, loaded, cfg, "隨便問問", protocol="native")
    returned = provider.seen[1]["messages"][-1]["results"]
    assert all(bad for _, _, bad in returned) and "合法的 JSON" in returned[0][1]
    assert "沒有名為 delete_everything" in returned[1][1] and "參數不對" in returned[2][1]
    assert [s["tool"] for s in result["steps"]] == ["delete_everything", "get_values"]    # 解析失敗的不算一步

    greedy = NativeScript(calls(*[("get_signals", {})] * (chat.MAX_STEPS + 2)), calls(("get_signals", {})))
    with pytest.raises(LLMError, match="仍沒有得出回答"):
        chat.answer(greedy, loaded, cfg, "一直查", protocol="native")
    over = greedy.seen[1]["messages"][-1]["results"]
    assert sum(1 for _, text, _ in over if "上限" in text) == 2                         # 多出的兩個沒有執行


def test_protocol_choice():
    class Compatible(NativeScript):
        name = "compatible"
    native = NativeScript()
    for provider, setting, expected in ((native, "auto", "json"), (native, "native", "native"),
                                        (Compatible(), "auto", "json"), (Compatible(), "native", "native")):
        assert chat.protocol_for(provider, setting) == expected
    native.name = "deepseek"
    assert chat.protocol_for(native, "auto") == "native" and chat.protocol_for(native, "json") == "json"

    class TextOnly(LLMProvider):
        name, model = "deepseek", "x"

        def complete(self, *a, **k):
            return ""
    assert chat.protocol_for(TextOnly(), "native") == "json"                     # 不支持就只能用通用協議


def test_each_native_round_goes_through_the_limits(loaded, cfg):
    from crudelab.models import LLMCall
    inner = NativeScript(calls(("get_signals", {})), ToolTurn(text="好的。"))
    inner.name = "deepseek"
    limited = LimitedProvider(inner, LLMSettings(min_interval_seconds=0), "chat")
    assert limited.supports_tools and chat.answer(limited, loaded, cfg, "信號？")["protocol"] == "native"
    assert [c.purpose for c in loaded.query(LLMCall).all()] == ["chat", "chat"]          # 兩輪各記一次


# ------------------------------ 3a. chat completions 線路格式 ------------------------------

def test_chat_completions_wire_format(monkeypatch):
    sent, replies = [], [
        {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "reasoning_content": "先查一下",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "get_values", "arguments": "{\"code\": \"BRENT_M1\"}"}},
                           {"id": "call_2", "type": "function",
                            "function": {"name": "search", "arguments": "{oops"}}]}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "答案"}}]}]

    def post(url, json=None, timeout=None, headers=None):
        sent.append(json)
        return httpx.Response(200, json=replies.pop(0), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    p = OpenAICompatibleProvider("deepseek", "https://api.example.test", "k", "m", thinking=True)
    turn = p.chat_tools("系統", [{"role": "user", "text": "問題"}], chat.tool_specs())
    assert [c.name for c in turn.calls] == ["get_values", "search"] and turn.calls[0].args == {"code": "BRENT_M1"}
    assert turn.calls[1].args is None and "JSON" in turn.calls[1].error and p.last_usage == (100, 20)
    body = sent[0]
    assert body["messages"][0] == {"role": "system", "content": "系統"} and body["tool_choice"] == "auto"
    assert body["tools"][0]["type"] == "function" and body["tools"][0]["function"]["parameters"]["type"] == "object"
    assert body["thinking"] == {"type": "enabled"} and body["max_tokens"] > 4000

    results = {"role": "tool_results", "results": [("call_1", "{}", False), ("call_2", "錯", True)]}
    final = p.chat_tools("系統", [{"role": "user", "text": "問題"}, {"role": "assistant", "raw": turn.raw}, results],
                         chat.tool_specs())
    assert final.text == "答案" and not final.calls
    msgs = sent[1]["messages"]
    assert msgs[2]["reasoning_content"] == "先查一下" and len(msgs[2]["tool_calls"]) == 2   # 思考內容同一題內帶回
    assert msgs[3:] == [{"role": "tool", "tool_call_id": "call_1", "content": "{}"},
                        {"role": "tool", "tool_call_id": "call_2", "content": "錯"}]


def test_chat_completions_rejection_suggests_json_protocol(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(400, json={"error": "secret detail"},
                                                                      request=httpx.Request("POST", url)))
    p = OpenAICompatibleProvider("compatible", "https://llm.example.test", "k", "m")
    with pytest.raises(LLMError, match="chat_protocol: json") as err:
        p.chat_tools("s", [{"role": "user", "text": "q"}], chat.tool_specs())
    assert err.value.status == 400 and "secret detail" not in str(err.value)            # 不轉述對方原文


# ------------------------------ 3b. Claude 線路格式（真的 SDK） ------------------------------

@pytest.fixture()
def claude_server():
    pytest.importorskip("anthropic")
    state = {"requests": [], "replies": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "beta": self.headers.get("anthropic-beta"), "body": body})
            payload = json.dumps(state["replies"].pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


def claude(url: str, model: str):
    import anthropic

    from crudelab.llm.providers import AnthropicProvider
    provider = AnthropicProvider(model, "test-key-not-real")
    provider._client = anthropic.Anthropic(api_key="test-key-not-real", base_url=url, max_retries=0)
    return provider


def message(content, stop="tool_use", model="claude-opus-5-5"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 50, "output_tokens": 10}}


def test_claude_wire_format_with_real_sdk(claude_server):
    claude_server["replies"] += [
        message([{"type": "thinking", "thinking": "", "signature": "sig-1"},
                 {"type": "tool_use", "id": "toolu_1", "name": "get_values", "input": {"code": "BRENT_M1"}},
                 {"type": "tool_use", "id": "toolu_2", "name": "get_signals", "input": {}}]),
        message([{"type": "text", "text": "答案"}], stop="end_turn")]
    p = claude(claude_server["url"], "claude-opus-5-5")
    turn = p.chat_tools("系統", [{"role": "user", "text": "問題"}], chat.tool_specs())
    assert [(c.id, c.name, c.args) for c in turn.calls] == [("toolu_1", "get_values", {"code": "BRENT_M1"}),
                                                            ("toolu_2", "get_signals", {})]
    assert p.last_usage == (50, 10)
    first = claude_server["requests"][0]
    assert first["path"].startswith("/v1/messages") and "server-side-fallback-2026-07-01" in first["beta"]
    assert first["body"]["fallbacks"] == "default" and "tool_choice" not in first["body"]   # 不強制調用
    assert first["body"]["tools"][0]["input_schema"]["additionalProperties"] is False

    final = p.chat_tools("系統", [{"role": "user", "text": "問題"}, {"role": "assistant", "raw": turn.raw},
                                  {"role": "tool_results", "results": [("toolu_1", "{}", False),
                                                                       ("toolu_2", "錯", True)]}],
                         chat.tool_specs())
    assert final.text == "答案" and not final.calls
    msgs = claude_server["requests"][1]["body"]["messages"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"][0] == {
        "type": "thinking", "thinking": "", "signature": "sig-1"}                    # 思考塊原樣帶回
    assert msgs[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}", "is_error": False},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "錯", "is_error": True}]}


def test_claude_refusal_and_plain_endpoint(claude_server):
    claude_server["replies"] += [message([], stop="refusal"),
                                 message([{"type": "text", "text": "好"}], stop="end_turn", model="claude-haiku-5-5")]
    with pytest.raises(LLMError, match="拒絕"):
        claude(claude_server["url"], "claude-opus-5-5").chat_tools("s", [{"role": "user", "text": "q"}], [])
    assert claude(claude_server["url"], "claude-haiku-5-5").chat_tools(
        "s", [{"role": "user", "text": "q"}], chat.tool_specs()).text == "好"
    plain = claude_server["requests"][1]
    assert not (plain["beta"] or "") and "fallbacks" not in plain["body"]          # 沒有後備的型號走普通接口
