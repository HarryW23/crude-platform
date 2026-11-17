"""登錄、登出、改自己的密碼（兩個站點都有）；帳號管理（只在後台）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import auth
from ...models import UserAccount
from ..deps import AppState, get_actor, get_db, get_state
from ..security import COOKIE, clear_session_cookie, current_user, set_session_cookie

router = APIRouter(tags=["auth"])
admin_router = APIRouter(tags=["users"])


class Login(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None        # 管理員重設別人的密碼


def _me(user: UserAccount | None) -> dict | None:
    return None if user is None else {"username": user.username, "role": user.role,
                                      "role_name": auth.ROLE_NAMES[user.role]}


def _user(u: UserAccount) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "role_name": auth.ROLE_NAMES[u.role],
            "active": u.active, "created_at": u.created_at, "last_login_at": u.last_login_at}


@router.post("/auth/login")
def login(body: Login, response: Response, session: Session = Depends(get_db),
          state: AppState = Depends(get_state)):
    if not state.settings.auth_enabled:
        raise HTTPException(400, "登錄功能沒有啟用")
    try:
        user = auth.authenticate(session, body.username, body.password)
    except auth.TooManyAttempts:
        session.commit()
        raise HTTPException(429, "連續輸錯太多次，請 15 分鐘後再試") from None
    except auth.LoginFailed:
        session.commit()                    # 失敗記錄要留下
        raise HTTPException(401, "用戶名或密碼不正確") from None
    set_session_cookie(response, state, auth.create_session(session, user, state.settings.session_hours))
    return _me(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_db),
           state: AppState = Depends(get_state)):
    auth.revoke(session, request.cookies.get(COOKIE))
    clear_session_cookie(response, state)
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, state: AppState = Depends(get_state)):
    return {"enabled": state.settings.auth_enabled, "user": _me(current_user(request))}


@router.post("/auth/password")
def change_own_password(body: PasswordChange, request: Request, response: Response,
                        session: Session = Depends(get_db), state: AppState = Depends(get_state)):
    """改自己的密碼。成功後其他地方的登錄全部失效，當前這個瀏覽器換發新的登錄。"""
    user = current_user(request)
    if user is None:
        raise HTTPException(400, "登錄功能沒有啟用")
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "目前的密碼不正確")
    try:
        auth.set_password(session, user.id, body.new_password, actor=user.username)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    set_session_cookie(response, state, auth.create_session(session, user, state.settings.session_hours))
    return {"ok": True}


# ------------------------------ 帳號管理（後台） ------------------------------

@admin_router.get("/users")
def list_users(session: Session = Depends(get_db)):
    return [_user(u) for u in session.scalars(select(UserAccount).order_by(UserAccount.id))]


@admin_router.post("/users")
def create_user(body: NewUser, session: Session = Depends(get_db), actor: str = Depends(get_actor)):
    try:
        return _user(auth.create_user(session, body.username, body.password, body.role, actor))
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@admin_router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, session: Session = Depends(get_db),
                actor: str = Depends(get_actor)):
    try:
        user = auth.update_user(session, user_id, role=body.role, active=body.active, actor=actor)
        if body.password is not None:
            auth.set_password(session, user_id, body.password, actor)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _user(user)
