"""各家大模型的接入。都還沒有用真實密鑰調用驗證過。

DeepSeek 和 OpenAI 用的是同一種接口格式（chat completions），所以共用一個類；
Claude 的接口不同，用 Anthropic 官方 SDK。
"""
from __future__ import annotations

import httpx

from .base import LLMError, LLMProvider

# 這些 Claude 模型可能因安全策略拒絕請求；按官方建議開啟服務端後備，讓請求自動改由後備模型完成
_CLAUDE_WITH_FALLBACK = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1",
                         "claude-fable-5-2")


class OpenAICompatibleProvider(LLMProvider):
    """DeepSeek、OpenAI，以及其他兼容 chat completions 格式的服務。"""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.name, self.model = name, model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key, self._timeout = api_key, timeout

    def complete(self, system: str, user: str, *, json: bool = False, max_tokens: int = 4000) -> str:
        body: dict = {"model": self.model, "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json:
            body["response_format"] = {"type": "json_object"}
        try:
            response = httpx.post(self._url, json=body, timeout=self._timeout,
                                  headers={"Authorization": f"Bearer {self._key}"})
            response.raise_for_status()
            choice = response.json()["choices"][0]
        # 錯誤訊息只用狀態碼和我們自己的說明：不轉述對方返回的原文，不帶請求內容（裡面有密鑰和發出去的數據）
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            hint = {401: "密鑰無效", 403: "密鑰沒有權限", 402: "帳戶餘額不足", 404: "模型名或接口地址不對",
                    429: "觸發了頻率或額度限制"}.get(code, "服務端錯誤" if code >= 500 else "請求被拒絕")
            raise LLMError(f"{self.name} 回報錯誤 {code}：{hint}") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"連不上 {self.name}（{type(exc).__name__}）") from None
        except (KeyError, IndexError, ValueError):
            raise LLMError(f"{self.name} 的回覆格式不符合預期") from None
        text = (choice.get("message") or {}).get("content")
        if not text:
            raise LLMError(f"{self.name} 沒有回覆內容（結束原因：{choice.get('finish_reason')}）")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.name} 的回覆因長度上限被截斷，結果不完整")
        return text


class AnthropicProvider(LLMProvider):
    """Claude。需要另外安裝：pip install -e ".[anthropic]"。"""

    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError('沒有安裝 anthropic 套件，請運行 pip install -e ".[anthropic]"') from exc
        self._sdk = anthropic
        # 給了 key 就用它（來自密鑰文件）；沒給則交給 SDK 按自己的順序找（環境變量、ant auth login 的登錄檔）
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, user: str, *, json: bool = False, max_tokens: int = 4000) -> str:
        sdk = self._sdk
        if json:
            system += "\n\n只輸出一個 JSON 對象，不要加任何說明文字或代碼塊標記。"
        request = dict(model=self.model, max_tokens=max(max_tokens, 16000), system=system,
                       messages=[{"role": "user", "content": user}])
        try:
            if self.model in _CLAUDE_WITH_FALLBACK:
                response = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
            else:
                response = self._client.messages.create(**request)
        except sdk.AuthenticationError:
            raise LLMError("Claude 的密鑰無效") from None
        except sdk.RateLimitError:
            raise LLMError("Claude 觸發了頻率限制，請稍後再試") from None
        except sdk.APIStatusError as exc:
            raise LLMError(f"Claude 回報錯誤 {exc.status_code}") from None
        except sdk.APIConnectionError:
            raise LLMError("連不上 Claude") from None
        if response.stop_reason == "refusal":
            raise LLMError("Claude 拒絕了這個請求")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude 的回覆因長度上限被截斷，結果不完整")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text:
            raise LLMError("Claude 沒有回覆內容")
        return text
