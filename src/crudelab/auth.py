"""帳號與登錄：密碼哈希、帳號增改、登錄驗證、會話。不依賴接口層，命令行也用它。

安全上的幾個決定：
  - 密碼用 scrypt 加鹽哈希，參數和鹽一起存，所以以後調高強度不影響舊帳號。
  - 會話令牌是 32 字節隨機數，數據庫只存它的 SHA-256。
  - 帳號不存在和密碼錯誤回同一個錯誤、花差不多的時間，不讓人探測哪些用戶名存在。
  - 同一個用戶名 15 分鐘內連錯 5 次就暫時鎖定。這個計數只在內存裡，重啟會清零；
    多進程部署時每個進程各算各的，所以它只是基本防護，對外服務時還應該在反向代理上限流。
  - 帳號只停用不刪除；停用或改密碼會立刻讓這個帳號的所有會話失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import AuditLog, UserAccount, UserSession, utcnow

ROLES = ("viewer", "analyst", "admin")           # 由低到高：只讀 / 研究員 / 管理員
ROLE_NAMES = {"viewer": "只讀", "analyst": "研究員", "admin": "管理員"}
MIN_PASSWORD_LENGTH = 10
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1     # 新密碼用的強度；測試裡會調低以加快速度
MAX_FAILURES, FAILURE_WINDOW_SECONDS = 5, 15 * 60
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_failures: dict[str, deque[float]] = defaultdict(deque)


class AuthError(ValueError):
    """帳號操作不合法（用戶名格式、密碼太短、角色不存在…）。"""


class LoginFailed(Exception):
    """用戶名或密碼不對，或帳號已停用。對外一律只說這一句。"""


class TooManyAttempts(Exception):
    """連續輸錯太多次，暫時鎖定。"""


def rank(role: str) -> int:
    return ROLES.index(role)


# ------------------------------ 密碼 ------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH: str | None = None


def _burn_time(password: str) -> None:
    """用戶名不存在時也做一次同樣強度的哈希，讓回應時間看不出用戶名存不存在。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    verify_password(password, _DUMMY_HASH)


def _check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"密碼至少要 {MIN_PASSWORD_LENGTH} 個字符")
    if len(password) > 256:
        raise AuthError("密碼太長")


# ------------------------------ 帳號 ------------------------------

def find(session: Session, username: str) -> UserAccount | None:
    return session.scalar(select(UserAccount).where(func.lower(UserAccount.username) == username.strip().lower()))


def create_user(session: Session, username: str, password: str, role: str, actor: str = "local") -> UserAccount:
    username = username.strip()
    if not _USERNAME.match(username):
        raise AuthError("用戶名要 2–64 個字符，只能用英文字母、數字和 . _ -，並以字母或數字開頭")
    if role not in ROLES:
        raise AuthError(f"角色必須是：{'、'.join(ROLES)}")
    _check_password(password)
    if find(session, username) is not None:
        raise AuthError(f"用戶名 {username} 已存在")
    user = UserAccount(username=username, password_hash=hash_password(password), role=role, created_by=actor)
    session.add(user)
    session.flush()
    session.add(AuditLog(actor=actor, action="user_create", entity="user", entity_id=username, detail={"role": role}))
    return user


def _require(session: Session, user_id: int) -> UserAccount:
    user = session.get(UserAccount, user_id)
    if user is None:
        raise AuthError(f"沒有編號為 {user_id} 的帳號")
    return user


def _other_active_admins(session: Session, user: UserAccount) -> int:
    return session.scalar(select(func.count()).select_from(UserAccount).where(
        UserAccount.role == "admin", UserAccount.active, UserAccount.id != user.id)) or 0


def update_user(session: Session, user_id: int, *, role: str | None = None, active: bool | None = None,
                actor: str = "local") -> UserAccount:
    """改角色或停用 / 啟用。不允許把最後一個可用的管理員降級或停用，否則沒有人能再管理帳號。"""
    user = _require(session, user_id)
    if role is not None and role not in ROLES:
        raise AuthError(f"角色必須是：{'、'.join(ROLES)}")
    loses_admin = user.role == "admin" and user.active and ((role is not None and role != "admin") or active is False)
    if loses_admin and _other_active_admins(session, user) == 0:
        raise AuthError("這是最後一個可用的管理員帳號，不能降級或停用")
    changes = {}
    if role is not None and role != user.role:
        changes["role"] = [user.role, role]
        user.role = role
    if active is not None and active != user.active:
        changes["active"] = [user.active, active]
        user.active = active
        if not active:
            revoke_all(session, user.id)
    if changes:
        session.add(AuditLog(actor=actor, action="user_update", entity="user", entity_id=user.username, detail=changes))
    return user


def set_password(session: Session, user_id: int, password: str, actor: str = "local") -> None:
    """設新密碼，並讓這個帳號已有的登錄全部失效。"""
    user = _require(session, user_id)
    _check_password(password)
    user.password_hash = hash_password(password)
    revoke_all(session, user.id)
    session.add(AuditLog(actor=actor, action="user_password", entity="user", entity_id=user.username, detail=None))


def count_admins(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(UserAccount).where(
        UserAccount.role == "admin", UserAccount.active)) or 0


# ------------------------------ 登錄與會話 ------------------------------

MAX_TRACKED_NAMES = 10_000      # 最多記多少個用戶名的失敗次數：防止有人用大量隨機用戶名把內存撐大


def _recent_failures(key: str) -> deque[float]:
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    if len(_failures) > MAX_TRACKED_NAMES:                       # 清掉已經過了窗口的記錄
        for name in [k for k, v in _failures.items() if not v or v[-1] < cutoff]:
            del _failures[name]
    attempts = _failures[key]
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return attempts


def authenticate(session: Session, username: str, password: str) -> UserAccount:
    """驗證用戶名和密碼。失敗拋 LoginFailed；連錯太多次拋 TooManyAttempts。"""
    key = username.strip().lower()
    attempts = _recent_failures(key)
    if len(attempts) >= MAX_FAILURES:
        raise TooManyAttempts
    user = find(session, username)
    if user is None:
        _burn_time(password)
        ok = False
    else:
        ok = verify_password(password, user.password_hash) and user.active
    if not ok:
        attempts.append(time.monotonic())
        session.add(AuditLog(actor=key[:64] or "?", action="login_failed", entity="user", entity_id=key[:128] or "?",
                             detail=None))
        raise LoginFailed
    attempts.clear()
    user.last_login_at = utcnow()
    session.add(AuditLog(actor=user.username, action="login", entity="user", entity_id=user.username, detail=None))
    return user


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(session: Session, user: UserAccount, hours: int) -> str:
    """建立一次登錄，回傳要放進 cookie 的令牌（只在這一刻存在，數據庫裡沒有原文）。"""
    token = secrets.token_urlsafe(32)
    session.add(UserSession(token_hash=_hash_token(token), user_id=user.id,
                            expires_at=utcnow() + timedelta(hours=hours)))
    return token


def resolve_session(session: Session, token: str | None, now: datetime | None = None) -> UserAccount | None:
    """令牌對應的帳號。令牌不存在、過期、被登出，或帳號已停用，都回 None。"""
    if not token:
        return None
    row = session.scalar(select(UserSession).where(UserSession.token_hash == _hash_token(token)))
    if row is None or row.revoked_at is not None or row.expires_at <= (now or utcnow()):
        return None
    user = session.get(UserAccount, row.user_id)
    return user if user is not None and user.active else None


def revoke(session: Session, token: str | None) -> None:
    if token:
        session.execute(update(UserSession).where(UserSession.token_hash == _hash_token(token),
                                                  UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))


def revoke_all(session: Session, user_id: int) -> None:
    session.execute(update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
                    .values(revoked_at=utcnow()))
