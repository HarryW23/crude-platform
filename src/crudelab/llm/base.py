from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(RuntimeError):
    """調用大模型失敗（網絡、額度、被拒絕、回覆無法解析）。"""


class LLMNotConfigured(LLMError):
    """沒有配置大模型，或缺密鑰。"""


class LLMProvider(ABC):
    """一家大模型的接入。子類只需要實現 complete。"""

    name: str       # 供應商代號
    model: str      # 實際調用的模型

    @abstractmethod
    def complete(self, system: str, user: str, *, json: bool = False, max_tokens: int = 4000) -> str:
        """給系統指令和用戶內容，回傳模型的文字回覆。json=True 表示要求回覆是一個 JSON 對象。

        失敗時拋 LLMError，訊息要能直接給使用者看。
        """
