"""接口的訪問控制。所有 /api 路由都經過這裡的 guard，新增路由不需要、也不應該自己寫權限判斷。

登錄功能關閉時（預設）：guard 什麼都不攔，操作者記為 local。
登錄功能開啟時：

  誰能做什麼                      只讀 viewer   研究員 analyst   管理員 admin
  前台：看數據、指標、報告            ✓              ✓               ✓
  前台：建情境 / 事件 / 推演 / 報告、問答              ✓               ✓
  後台：任何操作（包括查看）                                          ✓

規則是按「站點 + 請求方法」統一判斷的，不是逐個路由標註，所以不會漏：
  - 後台的一切都要管理員。
  - 前台的 GET 要登錄；前台會改東西的請求（POST / PATCH / DELETE）要研究員。
    例外是 READ_ONLY_POSTS：用 POST 只是為了傳參數、實際不保存任何東西的試算。
  - PUBLIC 裡的幾個地址不用登錄（登錄本身、查詢自己是誰、站點信息）。健康檢查 /api/health 另外掛載，完全不經過這裡。

另外兩道防護：
  - 會話 cookie 是 HttpOnly + SameSite=Lax，頁面腳本讀不到，別的網站發來的寫請求也帶不上它。
  - 會改東西的請求如果帶了 Origin 頭，必須和本站一致（防跨站請求偽造的第二道保險）。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import auth
from ..models import UserAccount
from .deps import AppState, get_db, get_state

COOKIE = "crudelab_session"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC = ("/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/site")
READ_ONLY_POSTS = (re.compile(r"^/api/refineries/[^/]+/scenario$"),)


def set_session_cookie(response: Response, state: AppState, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=state.settings.session_hours * 3600, httponly=True, samesite="lax",
                        secure=state.settings.cookie_secure, path="/")


def clear_session_cookie(response: Response, state: AppState) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=state.settings.cookie_secure)


def required_role(site: str, method: str, path: str) -> str | None:
    """這個請求最低需要什麼角色。None = 不用登錄。"""
    if path in PUBLIC:
        return None
    if site == "admin":
        return "admin"
    if method in SAFE_METHODS or any(p.match(path) for p in READ_ONLY_POSTS):
        return "viewer"
    return "analyst"


def guard(request: Request, state: AppState = Depends(get_state), session: Session = Depends(get_db)) -> None:
    request.state.user = None
    if not state.settings.auth_enabled:
        return
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "請求來源和本站不一致，已拒絕")
    user = auth.resolve_session(session, request.cookies.get(COOKIE))
    request.state.user = user
    needed = required_role(state.site, request.method, request.url.path)
    if needed is None:
        return
    if user is None:
        raise HTTPException(401, "請先登錄")
    if auth.rank(user.role) < auth.rank(needed):
        raise HTTPException(403, f"權限不足：這個操作需要「{auth.ROLE_NAMES[needed]}」，"
                                 f"你的帳號是「{auth.ROLE_NAMES[user.role]}」")


def current_user(request: Request) -> UserAccount | None:
    return getattr(request.state, "user", None)
