"""密鑰的唯一出入口：數據源的 API key、大模型的 API key、數據庫連接串。

規矩（違反任何一條都算洩漏風險）：
  1. 密鑰只從兩個地方來：環境變量，或密鑰文件。不進 git、不進 config/、不進數據庫、不進 data/ 文件夾。
  2. 代碼裡取密鑰只能調用這裡的 get()，不要自己讀 os.environ。這樣脫敏才知道有哪些值要遮。
  3. 密鑰不出現在任何輸出裡：接口回應、頁面、日誌、錯誤訊息、操作日誌、命令行輸出。
     對外部服務的錯誤，只報狀態碼和我們自己寫的說明，不轉述對方返回的原文，也不打印請求網址。
  4. 作為最後一道保險，所有日誌和接口的錯誤回應都過一遍 redact()，把已知的密鑰值換成 ***。
     這是兜底，不是可以放心亂打印的理由。

密鑰文件：預設 ~/.crudelab/secrets.env（可用 CRUDELAB_SECRETS_FILE 改），格式是每行 NAME=value。
  - 放在項目文件夾之外，所以打包代碼、備份 data/、推送 git 都帶不走它。
  - 在 Mac / Linux 上必須只有本人可讀寫（權限 600），否則拒絕使用並說明怎麼改。
  - 用 `crudelab secret set NAME` 寫入：輸入時不回顯，也不會留在終端的歷史記錄裡。
環境變量優先於文件，方便服務器上用平台自帶的密鑰管理注入。
"""
from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# 系統認得的密鑰。新增數據源或模型供應商時在這裡登記，否則 get() 會拒絕、脫敏也遮不到。
KNOWN: dict[str, str] = {
    "EIA_API_KEY": "EIA 公開數據（免費申請）",
    "DEEPSEEK_API_KEY": "DeepSeek 大模型",
    "OPENAI_API_KEY": "OpenAI 大模型",
    "ANTHROPIC_API_KEY": "Claude 大模型",
    "CRUDELAB_LLM_API_KEY": "其他兼容接口的大模型（CRUDELAB_LLM_PROVIDER=compatible）",
    "DATABASE_URL": "數據庫連接串（用 PostgreSQL 時含密碼）",
    "LSEG_APP_KEY": "路透 / LSEG 數據 API（預留）",
    "LSEG_USERNAME": "路透 / LSEG 帳號（預留）",
    "LSEG_PASSWORD": "路透 / LSEG 密碼（預留）",
    "KPLER_API_TOKEN": "Kpler 數據 API（預留）",
    "CN_VENDOR_API_KEY": "國內資訊商數據 API（預留）",
}
MASK = "***"
_MIN_REDACT_LENGTH = 6          # 比這更短的值不做全文替換，否則會把正常文字遮得面目全非
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
# 不認識的密鑰也盡量遮：網址參數裡的 key / token、Bearer 令牌、常見的 sk- 開頭的密鑰
_PATTERNS = (
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|apikey|password|secret)=([^&\s\"'<>]+)"), r"\1=" + MASK),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 " + MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), MASK),
    (re.compile(r"(?i)(://[^:/\s@]+):([^@/\s]+)@"), r"\1:" + MASK + "@"),      # 連接串裡的 用戶:密碼@
)
_cache: tuple[tuple, dict[str, str]] | None = None


class CredentialError(RuntimeError):
    """密鑰文件不安全或無法讀寫。訊息裡不含任何密鑰內容。"""


def secrets_path() -> Path:
    custom = os.environ.get("CRUDELAB_SECRETS_FILE", "").strip()
    return Path(custom).expanduser() if custom else Path.home() / ".crudelab" / "secrets.env"


def _check_private(path: Path) -> None:
    if os.name != "posix":          # Windows 沒有同樣的權限位；文件在用戶自己的主目錄下，由系統的帳戶隔離保護
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError(f"密鑰文件 {path} 能被其他用戶讀取，已拒絕使用。請運行：chmod 600 {path}")


def _read_file() -> dict[str, str]:
    global _cache
    path = secrets_path()
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    key = (str(path), info.st_mtime_ns, info.st_size, info.st_mode)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    _check_private(path)
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line.strip())
        if match and match.group(2):
            values[match.group(1)] = match.group(2)
    _cache = (key, values)
    return values


def get(name: str) -> str | None:
    """取一個密鑰。沒設定回 None。環境變量優先於密鑰文件。"""
    if name not in KNOWN:
        raise KeyError(f"{name} 沒有在 credentials.KNOWN 裡登記")
    return os.environ.get(name) or _read_file().get(name) or None


def source(name: str) -> str | None:
    """這個密鑰目前來自哪裡：「環境變量」「密鑰文件」，或 None（沒設定）。"""
    if os.environ.get(name):
        return "環境變量"
    return "密鑰文件" if _read_file().get(name) else None


def _write_file(values: dict[str, str]) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    # 先寫到同目錄的臨時文件（建立時就是 600），再原子替換：任何時刻都不存在一個別人能讀的版本
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".secrets-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# crudelab 密鑰文件。不要提交到 git，不要發給別人。用 crudelab secret set / unset 管理。\n")
            fh.writelines(f"{k}={v}\n" for k, v in sorted(values.items()))
        if os.name == "posix":
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def set_secret(name: str, value: str) -> None:
    if name not in KNOWN:
        raise CredentialError(f"不認識的密鑰名 {name}。可用的：{'、'.join(KNOWN)}")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise CredentialError("密鑰不能為空，也不能包含換行")
    _write_file({**_read_file(), name: value})


def unset_secret(name: str) -> bool:
    values = dict(_read_file())
    if name not in values:
        return False
    del values[name]
    _write_file(values)
    return True


# ------------------------------ 脫敏 ------------------------------

def _known_values() -> list[str]:
    try:
        from_file = _read_file()
    except CredentialError:
        from_file = {}
    values = set()
    for name in KNOWN:
        for value in (os.environ.get(name), from_file.get(name)):
            if not value:
                continue
            values.add(value)
            if "://" in value:                      # 連接串：密碼單獨也要遮
                password = urlsplit(value).password
                if password:
                    values.add(password)
    return sorted((v for v in values if len(v) >= _MIN_REDACT_LENGTH), key=len, reverse=True)


def redact(text: object) -> str:
    """把文字裡的密鑰換成 ***：先換已知的密鑰值，再按常見樣式（key=…、Bearer …、sk-…、用戶:密碼@）兜底。"""
    out = str(text)
    for value in _known_values():
        out = out.replace(value, MASK)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        if record.exc_info:                         # 把異常堆棧先排成文字再遮，之後就不會再按原樣排一次
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction() -> None:
    """給所有日誌輸出加上脫敏，並關掉會把完整請求網址打出來的第三方日誌。重複調用無害。"""
    for name in ("httpx", "httpcore"):              # 這兩個在 INFO 級別會打印完整網址（EIA 的 key 在網址裡）
        logging.getLogger(name).setLevel(logging.WARNING)
    target = logging.getLogger()
    loggers = [target, *(logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access", "alembic"))]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(f, _RedactingFilter) for f in handler.filters):
                handler.addFilter(_RedactingFilter())
        if not any(isinstance(f, _RedactingFilter) for f in logger.filters):
            logger.addFilter(_RedactingFilter())
