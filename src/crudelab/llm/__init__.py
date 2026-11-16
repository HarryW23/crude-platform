"""大模型抽象接口（預留，第四期使用）。

平台裡所有需要大模型的地方（新聞事件抽取、報告撰寫、對話查詢）都只依賴 LLMProvider，
不直接依賴任何一家的 SDK。換供應商 = 寫一個新的 LLMProvider 子類並改配置。
原則：大模型只處理文字，不產生數字；數字一律來自指標引擎。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMMessage:
    role: str      # "system" / "user" / "assistant"
    content: str


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def complete(self, messages: list[LLMMessage], *, json_schema: dict | None = None,
                 max_tokens: int = 2000) -> str:
        """回傳文字；給了 json_schema 時回傳符合該結構的 JSON 字符串。"""
