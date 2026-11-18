"""選擇用哪一家大模型、哪個型號、開不開思考模式。

優先順序（先找到的算數）：
    1. 後台「大模型」頁選的（存在數據庫 app_setting 的 llm.choice；crudelab llm use 寫的也是這裡）
    2. 環境變量 CRUDELAB_LLM_PROVIDER / CRUDELAB_LLM_MODEL / CRUDELAB_LLM_BASE_URL（部署時用）
    3. config/llm.yaml 的 provider / model / base_url（所有機器共用的預設）
密鑰一律通過 credentials.get() 取得（DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / CRUDELAB_LLM_API_KEY），
用 crudelab secret set 設定，不寫在任何配置文件或數據庫裡。

型號不再內置預設值：供應商會改名、會停用舊名字
（例如 DeepSeek 在 2026 年 7 月停用了 deepseek-chat / deepseek-reasoner），寫死的預設遲早失效。
型號名以供應商自己的模型列表為準：後台可以直接向供應商查（list_models），從列表裡選。

思考模式（thinking）：只有 DeepSeek 支持，作為請求參數發送 {"thinking": {"type": "enabled" / "disabled"}}。
不選時明確發 disabled：不依賴對方的預設值（對方的預設可能是開）。

所有調用都套上 limits.LimitedProvider：頻率、用量上限（config/llm.yaml）、自動暫停、用量記錄。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .. import credentials, db
from ..schema import LLMSettings
from .base import LLMNotConfigured, LLMProvider

PROVIDERS = {"none": "不使用", "deepseek": "DeepSeek", "openai": "OpenAI", "anthropic": "Claude",
             "compatible": "其他兼容接口"}
_OPENAI_STYLE = {   # 代號 → (接口地址, 密鑰名)
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "compatible": (None, "CRUDELAB_LLM_API_KEY"),
}
KEY_NAMES = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
             "compatible": "CRUDELAB_LLM_API_KEY"}
SUPPORTS_THINKING = {"deepseek"}
CHOICE_KEY = "llm.choice"


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str
    base_url: str
    thinking: bool
    source: str          # 後台 / 環境變量 / config/llm.yaml：頁面上顯示「現在的設定來自哪裡」


def _stored() -> dict | None:
    """後台選的設定。數據庫還沒初始化（例如單元測試、還沒 init）時當作沒有。"""
    if db._factory is None:
        return None
    from ..models import AppSetting
    try:
        with db.session_scope() as session:
            row = session.get(AppSetting, CHOICE_KEY)
            return dict(row.value) if row is not None else None
    except Exception:          # 表還不存在等：不影響其他配置來源
        return None


def current_choice(settings: LLMSettings) -> Choice:
    stored = _stored()
    if stored and stored.get("provider"):
        return Choice(stored["provider"], (stored.get("model") or "").strip(), (stored.get("base_url") or "").strip()
                      or (settings.base_url or ""), bool(stored.get("thinking")), "後台")
    env_provider = os.environ.get("CRUDELAB_LLM_PROVIDER", "").strip().lower()
    if env_provider:
        return Choice(env_provider, os.environ.get("CRUDELAB_LLM_MODEL", "").strip(),
                      os.environ.get("CRUDELAB_LLM_BASE_URL", "").strip() or (settings.base_url or ""), False,
                      "環境變量")
    return Choice(settings.provider, (settings.model or "").strip(), settings.base_url or "", False,
                  "config/llm.yaml")


def save_choice(session, provider: str, model: str, thinking: bool, actor: str, base_url: str | None = None) -> dict:
    """後台或 crudelab llm use 保存選擇。不發起網絡請求，也不檢查型號是否存在（連通測試會檢查）。"""
    from ..models import AppSetting, AuditLog
    if provider not in PROVIDERS:
        raise LLMNotConfigured(f"不認識的大模型供應商：{provider}")
    model = (model or "").strip()
    if provider != "none" and not model:
        raise LLMNotConfigured("請選擇或填寫模型名")
    if len(model) > 128 or any(c.isspace() for c in model):
        raise LLMNotConfigured("模型名不能有空格，也不能超過 128 個字符")
    if base_url and not base_url.startswith("https://"):
        raise LLMNotConfigured("接口地址必須是 https")
    value = {"provider": provider, "model": model if provider != "none" else "",
             "thinking": bool(thinking) and provider in SUPPORTS_THINKING, "base_url": base_url or ""}
    row = session.get(AppSetting, CHOICE_KEY)
    if row is None:
        session.add(AppSetting(key=CHOICE_KEY, value=value, updated_by=actor))
    else:
        row.value, row.updated_by = value, actor
    session.add(AuditLog(actor=actor, action="llm_choice", entity="llm", entity_id=f"{provider}/{model}"[:128],
                         detail={"thinking": value["thinking"]}))
    return value


def get_provider(purpose: str = "other", limits: LLMSettings | None = None) -> LLMProvider:
    """回傳當前配置的大模型（已套上調用限制）。沒配置或配置不全時拋 LLMNotConfigured，訊息說明缺什麼。

    purpose 記在用量記錄裡（chat / extract / narrative / test / models）；limits 一般傳 cfg.llm。
    """
    from .limits import LimitedProvider
    limits = limits or LLMSettings()
    return LimitedProvider(_raw_provider(limits), limits, purpose)


def model_lister(limits: LLMSettings | None = None, provider: str | None = None):
    """用來向供應商查模型列表的對象（同樣受調用限制）。provider 不給就用當前選的那家。不需要已經選好型號。"""
    from .limits import LimitedProvider
    limits = limits or LLMSettings()
    return LimitedProvider(_raw_provider(limits, require_model=False, provider=provider), limits, "models")


def _raw_provider(settings: LLMSettings, require_model: bool = True, provider: str | None = None) -> LLMProvider:
    from .providers import AnthropicProvider, OpenAICompatibleProvider
    choice = current_choice(settings)
    name = provider or choice.provider
    model = choice.model if name == choice.provider else ""
    if name == "none":
        raise LLMNotConfigured("還沒有選擇大模型（後台「大模型」頁）")
    if name not in PROVIDERS:
        raise LLMNotConfigured(f"不認識的大模型供應商：{name}")
    if require_model and not model:
        raise LLMNotConfigured(f"還沒有選擇 {PROVIDERS[name]} 的模型（後台「大模型」頁可以從供應商的模型列表裡選）")
    timeout = settings.timeout_seconds
    thinking = choice.thinking and name in SUPPORTS_THINKING
    if thinking:
        timeout = max(timeout, 300.0)            # 思考模式回覆慢得多
    if name == "anthropic":
        return AnthropicProvider(model, credentials.get("ANTHROPIC_API_KEY"))
    base_url, key_env = _OPENAI_STYLE[name]
    base_url = base_url or choice.base_url
    if not base_url:
        raise LLMNotConfigured("其他兼容接口需要填寫接口地址（https）")
    key = credentials.get(key_env)
    if not key:
        raise LLMNotConfigured(f"還沒有設定 {key_env}（在服務器上運行 crudelab secret set {key_env}）")
    return OpenAICompatibleProvider(name, base_url, key, model, timeout=timeout,
                                    thinking=thinking if name in SUPPORTS_THINKING else None)


def status(settings: LLMSettings | None = None) -> dict:
    """給頁面顯示用：配置了沒有、是哪一家哪個模型、思考模式、設定來自哪裡、缺什麼。不發起任何網絡請求。"""
    settings = settings or LLMSettings()
    choice = current_choice(settings)
    base = {"provider": choice.provider, "provider_name": PROVIDERS.get(choice.provider, choice.provider),
            "model": choice.model or None, "thinking": choice.thinking, "source": choice.source,
            "supports_thinking": choice.provider in SUPPORTS_THINKING, "key_name": KEY_NAMES.get(choice.provider),
            "key_set": bool(credentials.get(KEY_NAMES[choice.provider])) if choice.provider in KEY_NAMES else False}
    try:
        _raw_provider(settings)
    except LLMNotConfigured as exc:
        return {**base, "configured": False, "message": str(exc)}
    except Exception as exc:    # 例如沒裝對應的 SDK、密鑰文件權限不對
        return {**base, "configured": False, "message": credentials.redact(exc)}
    return {**base, "configured": True, "message": "已配置（連通測試可以確認型號名和密鑰都對）"}
