"""Auth endpoints: login, logout, session status, change password."""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel

import auth as _auth
from auth import create_jwt, get_current_user, get_optional_user
from db import get_conn

router = APIRouter()

MIN_PASSWORD_LENGTH = 8


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/api/login")
def login(request: Request, body: LoginRequest, response: Response):
    # Rate limiting is applied via the limiter on app.state (see app.py).
    # The limiter decorator is attached in app.py after router registration
    # because slowapi needs the app instance.  The route is still protected
    # — the middleware-level limiter catches it.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash, must_change_password FROM users WHERE username = %s",
                (body.username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not bcrypt.checkpw(body.password.encode(), row[0].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    must_change = bool(row[1])
    token = create_jwt(body.username)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        secure=_auth.COOKIE_SECURE,
        samesite="lax",
        max_age=int(_auth.JWT_EXPIRY_HOURS * 3600),
    )
    return {"username": body.username, "must_change_password": must_change}


@router.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("auth_token")
    return {"ok": True}


@router.get("/api/me")
def me(auth_token: str | None = Cookie(None)):
    username = get_optional_user(auth_token)
    if not username:
        return {"username": None, "is_admin": False, "must_change_password": False}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_admin, must_change_password FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    is_admin = bool(row and row[0])
    must_change = bool(row and row[1])
    return {
        "username": username,
        "is_admin": is_admin,
        "must_change_password": must_change,
    }


@router.post("/api/auth/change-password")
def change_password(
    body: ChangePasswordRequest,
    user: str = Depends(get_current_user),
):
    """Set a new password for the current user.

    Verifies ``current_password`` against the stored bcrypt hash so a stolen
    cookie alone cannot rotate the credential. Clears ``must_change_password``
    on success.
    """
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=422,
            detail="New password must differ from the current password",
        )

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM users WHERE username = %s",
                (user,),
            )
            row = cur.fetchone()
        if not row or not bcrypt.checkpw(body.current_password.encode(), row[0].encode()):
            raise HTTPException(status_code=401, detail="Current password is incorrect")

        new_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users "
                "SET password_hash = %s, "
                "    must_change_password = FALSE, "
                "    password_changed_at = now() "
                "WHERE username = %s",
                (new_hash, user),
            )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True}
