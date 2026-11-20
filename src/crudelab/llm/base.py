from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """調用大模型失敗（網絡、額度、被拒絕、回覆無法解析）。status 是對方返回的 HTTP 狀態碼（有的話）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LLMNotConfigured(LLMError):
    """沒有配置大模型，或缺密鑰。"""


@dataclass
class ToolSpec:
    """給模型的一個工具：名字、說明、參數的 JSON Schema。"""
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """模型要調用的一個工具。args 解析失敗時為 None，原因在 error（交還給模型讓它改正）。"""
    id: str
    name: str
    args: dict | None
    error: str | None = None


@dataclass
class ToolTurn:
    """原生工具調用的一輪回覆：要麼有 calls（要調工具），要麼只有 text（最終回答）。
    raw 是這一輪助手消息的原始內容，下一輪原樣傳回（Claude 的思考塊、DeepSeek 的 reasoning_content 都在裡面，
    不能改，否則對方會拒絕或丟掉推理）。"""
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None


# 原生工具調用的對話記錄用這個中立格式，各家自己轉成自己的消息格式：
#   {"role": "user", "text": ...}                       使用者的話
#   {"role": "assistant", "text": ...}                  以前的回答（純文字）
#   {"role": "assistant", "raw": ToolTurn.raw}          這一題裡要調工具的那一輪（原樣）
#   {"role": "tool_results", "results": [(call_id, 文字, 是否出錯), ...]}   這一輪所有工具的結果，一次交回


class LLMProvider(ABC):
    """一家大模型的接入。子類必須實現 complete；支持原生工具調用的再實現 chat_tools 並把 supports_tools 設為 True。"""

    name: str       # 供應商代號
    model: str      # 實際調用的模型
    last_usage: tuple[int | None, int | None] = (None, None)    # 最近一次調用的（輸入, 輸出）token 數

    @property
    def supports_tools(self) -> bool:
        """是否實現了原生工具調用（chat_tools）。實現了的子類寫 supports_tools = True。"""
        return False

    def chat_tools(self, system: str, messages: list[dict], tools: list[ToolSpec], *,
                   max_tokens: int = 4000) -> ToolTurn:
        """原生工具調用的一輪。messages 是上面的中立格式。失敗拋 LLMError。"""
        raise LLMError(f"{self.name} 不支持原生工具調用")

    @abstractmethod
    def complete(self, system: str, user: str, *, json: bool = False, max_tokens: int = 4000) -> str:
        """給系統指令和用戶內容，回傳模型的文字回覆。json=True 表示要求回覆是一個 JSON 對象。

        失敗時拋 LLMError，訊息要能直接給使用者看。
        """

    def list_models(self) -> list[str]:
        """向供應商查這個密鑰能用的模型名。不支持的供應商回傳空列表。"""
        return []
