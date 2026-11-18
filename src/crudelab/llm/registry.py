"""選擇用哪一家大模型。

    config/llm.yaml 的 provider / model / base_url，環境變量同名的 CRUDELAB_LLM_PROVIDER / _MODEL / _BASE_URL 優先：
    provider   none（預設）/ deepseek / openai / anthropic / compatible
    model      模型名。deepseek 預設 deepseek-chat，anthropic 預設 claude-opus-5-5；其餘必填
    base_url   僅 compatible 需要：任何兼容 chat completions 格式的服務地址
    密鑰       DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / CRUDELAB_LLM_API_KEY（compatible）
               一律通過 credentials.get() 取得，用 crudelab secret set 設定，不寫在任何配置文件裡

所有調用都套上 limits.LimitedProvider：頻率、用量上限（config/llm.yaml）、自動暫停、用量記錄。
想省錢時不用改代碼：換 provider，或把 model 換成同一家更便宜的型號。
"""
from __future__ import annotations

import os

from .. import credentials
from ..schema import LLMSettings
from .base import LLMNotConfigured, LLMProvider

_OPENAI_STYLE = {   # 代號 → (接口地址, 密鑰的環境變量, 預設模型)
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", None),
    "compatible": (None, "CRUDELAB_LLM_API_KEY", None),
}


def _pick(env: str, configured: str | None) -> str:
    return os.environ.get(env, "").strip() or (configured or "").strip()


def _choice(settings: LLMSettings) -> str:
    return _pick("CRUDELAB_LLM_PROVIDER", settings.provider).lower() or "none"


def get_provider(purpose: str = "other", limits: LLMSettings | None = None) -> LLMProvider:
    """回傳當前配置的大模型（已套上調用限制）。沒配置或配置不全時拋 LLMNotConfigured，訊息說明缺什麼。

    purpose 記在用量記錄裡（chat / extract / narrative / test）；limits 一般傳 cfg.llm。
    """
    from .limits import LimitedProvider
    limits = limits or LLMSettings()
    return LimitedProvider(_raw_provider(limits), limits, purpose)


def _raw_provider(settings: LLMSettings) -> LLMProvider:
    from .providers import AnthropicProvider, OpenAICompatibleProvider
    name, model = _choice(settings), _pick("CRUDELAB_LLM_MODEL", settings.model)
    if name == "none":
        raise LLMNotConfigured("還沒有配置大模型（config/llm.yaml 的 provider）")
    if name == "anthropic":
        return AnthropicProvider(model or "claude-opus-5-5", credentials.get("ANTHROPIC_API_KEY"))
    if name not in _OPENAI_STYLE:
        raise LLMNotConfigured(f"不認識的大模型供應商：{name}")
    base_url, key_env, default_model = _OPENAI_STYLE[name]
    base_url = base_url or _pick("CRUDELAB_LLM_BASE_URL", settings.base_url)
    if not base_url:
        raise LLMNotConfigured("compatible 需要設定 base_url（config/llm.yaml）")
    key = credentials.get(key_env)
    if not key:
        raise LLMNotConfigured(f"還沒有設定 {key_env}（用 crudelab secret set {key_env} 設定）")
    if not (model or default_model):
        raise LLMNotConfigured(f"{name} 需要在 config/llm.yaml 的 model 指定模型名")
    return OpenAICompatibleProvider(name, base_url, key, model or default_model, timeout=settings.timeout_seconds)


def status(settings: LLMSettings | None = None) -> dict:
    """給頁面顯示用：配置了沒有、是哪一家哪個模型、缺什麼。不發起任何網絡請求。"""
    settings = settings or LLMSettings()
    try:
        provider = _raw_provider(settings)
    except LLMNotConfigured as exc:
        return {"configured": False, "provider": _choice(settings), "model": None, "message": str(exc)}
    except Exception as exc:    # 例如沒裝對應的 SDK、密鑰文件權限不對
        return {"configured": False, "provider": _choice(settings), "model": None, "message": credentials.redact(exc)}
    return {"configured": True, "provider": provider.name, "model": provider.model, "message": "已配置（尚未驗證連通）"}
