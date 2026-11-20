"""最小的 MCP 客戶端（Streamable HTTP 傳輸）：只做「調用遠端工具、讀資源」這件事。

流程按 MCP 規範：initialize → notifications/initialized → tools/call / resources/read。
  - 每個請求是一個 JSON-RPC 消息，POST 到同一個地址；回覆可能是 application/json，也可能是 text/event-stream（SSE）
  - initialize 的回覆頭裡如果有 Mcp-Session-Id，之後的請求都要帶上
  - 初始化之後的請求帶 MCP-Protocol-Version 頭
  - 工具結果優先讀 result.structuredContent；result.isError = true 是工具層的業務錯誤；JSON-RPC error 是協議錯誤

安全：token 只放在 Authorization 頭裡，不進網址、不進日誌；錯誤訊息只用我們自己的說明和狀態碼，
對方返回的文字先脫敏再截短，不原樣轉述。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .. import credentials


class MCPError(RuntimeError):
    """調用失敗（網絡、認證、協議）。訊息可以直接給人看，不含 token。"""


class MCPToolError(MCPError):
    """工具返回 isError = true：業務錯誤（例如參數不對）。"""


class MCPRateLimited(MCPToolError):
    """對方說今天這個工具的調用次數到上限了。"""


RATE_LIMIT_HINTS = ("上限", "rate limit", "too many")


def _sse_messages(text: str) -> list[dict]:
    """把 SSE 響應體拆成 JSON 消息：每個事件的 data 行拼起來是一個 JSON。"""
    out, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            try:
                out.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return out


class MCPClient:
    def __init__(self, name: str, url: str, token: str, protocol: str = "2025-11-25",
                 client: httpx.Client | None = None, timeout: float = 30.0):
        self.name, self.url, self.protocol = name, url, protocol
        self._token = token
        self._client = client or httpx.Client(timeout=timeout)
        self._own_client = client is None
        self._session: str | None = None
        self._open = False
        self._next_id = 0
        self.server_info: dict = {}

    # ------------------------------ 傳輸 ------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self._token}"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self._open:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def _clean(self, text: Any, limit: int = 160) -> str:
        return credentials.redact(str(text)).replace(self._token, "***")[:limit]

    def _send(self, method: str, params: dict | None = None, notification: bool = False) -> dict | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self._next_id += 1
            message["id"] = self._next_id
        try:
            resp = self._client.post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPError(f"連不上{self.name}（{type(exc).__name__}）") from None
        if resp.status_code in (401, 403):
            raise MCPError(f"{self.name}拒絕了請求（{resp.status_code}）：token 無效、過期或沒有權限")
        if resp.status_code == 429:
            raise MCPRateLimited(f"{self.name}說請求太頻繁（429），請稍後再試")
        if resp.status_code == 404 and self._session:
            raise MCPError(f"{self.name}的會話已過期（404），下次調用會重新連接")
        if resp.status_code >= 300:
            raise MCPError(f"{self.name}回報錯誤 {resp.status_code}")
        if notification:
            return None
        session = resp.headers.get("mcp-session-id")
        if session:
            self._session = session
        content_type = resp.headers.get("content-type", "")
        try:
            messages = _sse_messages(resp.text) if "text/event-stream" in content_type else [resp.json()]
        except ValueError:
            raise MCPError(f"{self.name}的回覆不是 JSON") from None
        reply = next((m for m in messages if isinstance(m, dict) and m.get("id") == message["id"]), None)
        if reply is None:
            raise MCPError(f"{self.name}沒有回覆這個請求（{method}）")
        if "error" in reply:
            err = reply["error"] or {}
            raise MCPError(f"{self.name}協議錯誤 {err.get('code')}：{self._clean(err.get('message', ''))}")
        return reply.get("result") or {}

    # ------------------------------ 會話 ------------------------------
    def open(self) -> None:
        if self._open:
            return
        result = self._send("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                           "clientInfo": {"name": "crudelab", "version": "1"}}) or {}
        self.protocol = result.get("protocolVersion") or self.protocol
        self.server_info = result.get("serverInfo") or {}
        self._open = True
        self._send("notifications/initialized", notification=True)

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def list_tools(self) -> list[str]:
        self.open()
        return [t.get("name") for t in (self._send("tools/list", {}) or {}).get("tools", [])]

    def call(self, tool: str, arguments: dict | None = None) -> Any:
        """調用一個工具，回傳 structuredContent。"""
        self.open()
        result = self._send("tools/call", {"name": tool, "arguments": arguments or {}}) or {}
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
            if any(h in text.lower() for h in RATE_LIMIT_HINTS):
                raise MCPRateLimited(f"{self.name}的 {tool} 今天的調用次數到上限了，明天再試")
            raise MCPToolError(f"{self.name}的 {tool} 返回錯誤：{self._clean(text) or '（沒有說明）'}")
        if "structuredContent" not in result:
            raise MCPError(f"{self.name}的 {tool} 沒有返回結構化結果")
        return result["structuredContent"]

    def read_resource(self, uri: str) -> Any:
        """讀一個資源。內容是 JSON 文本時解析成對象，否則回傳文本。"""
        self.open()
        contents = (self._send("resources/read", {"uri": uri}) or {}).get("contents", [])
        texts = [c.get("text", "") for c in contents if isinstance(c, dict)]
        joined = "".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined
