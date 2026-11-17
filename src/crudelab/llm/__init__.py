"""大模型接口。平台裡所有用到大模型的地方都只依賴這裡，不直接依賴任何一家的 SDK。

分工的規矩：
  - 大模型只處理文字：從新聞裡抽取事件、為報告寫解讀。
  - 大模型不產生數字。數字全部來自指標引擎；它寫的文字裡出現的數字會被逐個核對（guard.py）。
  - 大模型的輸出不直接生效。抽取的事件是草稿，要人確認；解讀單獨存放並標明出處。

換供應商只改環境變量，見 registry.py。沒配置時相關功能回報「未配置」，其餘功能不受影響。
"""
from .base import LLMError, LLMNotConfigured, LLMProvider
from .registry import get_provider, status

__all__ = ["LLMError", "LLMNotConfigured", "LLMProvider", "get_provider", "status"]
