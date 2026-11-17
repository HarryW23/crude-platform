"""按環境變量選擇大模型。

    CRUDELAB_LLM_PROVIDER   none（預設）/ deepseek / openai / anthropic / compatible
    CRUDELAB_LLM_MODEL      模型名。deepseek 預設 deepseek-chat，anthropic 預設 claude-opus-5-5；其餘必填
    CRUDELAB_LLM_BASE_URL   僅 compatible 需要：任何兼容 chat completions 格式的服務地址
    密鑰                    DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / CRUDELAB_LLM_API_KEY（compatible）

想省錢時不用改代碼：換 CRUDELAB_LLM_PROVIDER，或把 CRUDELAB_LLM_MODEL 換成同一家更便宜的型號。
"""
from __future__ import annotations

import os

from .base import LLMNotConfigured, LLMProvider

_OPENAI_STYLE = {   # 代號 → (接口地址, 密鑰的環境變量, 預設模型)
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", None),
    "compatible": (None, "CRUDELAB_LLM_API_KEY", None),
}


def _choice() -> str:
    return os.environ.get("CRUDELAB_LLM_PROVIDER", "none").strip().lower() or "none"


def get_provider() -> LLMProvider:
    """回傳當前配置的大模型。沒配置或配置不全時拋 LLMNotConfigured，訊息說明缺什麼。"""
    from .providers import AnthropicProvider, OpenAICompatibleProvider
    name, model = _choice(), os.environ.get("CRUDELAB_LLM_MODEL", "").strip()
    if name == "none":
        raise LLMNotConfigured("還沒有配置大模型（環境變量 CRUDELAB_LLM_PROVIDER）")
    if name == "anthropic":
        return AnthropicProvider(model or "claude-opus-5-5")
    if name not in _OPENAI_STYLE:
        raise LLMNotConfigured(f"不認識的大模型供應商：{name}")
    base_url, key_env, default_model = _OPENAI_STYLE[name]
    base_url = base_url or os.environ.get("CRUDELAB_LLM_BASE_URL", "").strip()
    if not base_url:
        raise LLMNotConfigured("compatible 需要設定 CRUDELAB_LLM_BASE_URL")
    if not os.environ.get(key_env):
        raise LLMNotConfigured(f"缺少環境變量 {key_env}")
    if not (model or default_model):
        raise LLMNotConfigured(f"{name} 需要用 CRUDELAB_LLM_MODEL 指定模型名")
    return OpenAICompatibleProvider(name, base_url, os.environ[key_env], model or default_model)


def status() -> dict:
    """給頁面顯示用：配置了沒有、是哪一家哪個模型、缺什麼。不發起任何網絡請求。"""
    try:
        provider = get_provider()
    except LLMNotConfigured as exc:
        return {"configured": False, "provider": _choice(), "model": None, "message": str(exc)}
    except Exception as exc:    # 例如沒裝對應的 SDK
        return {"configured": False, "provider": _choice(), "model": None, "message": str(exc)}
    return {"configured": True, "provider": provider.name, "model": provider.model, "message": "已配置（尚未驗證連通）"}
