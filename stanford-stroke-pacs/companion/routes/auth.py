"""Auth endpoints: login, logout, session status."""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

import auth as _auth
from auth import create_jwt, get_optional_user
from db import get_conn

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


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
                "SELECT password_hash FROM users WHERE username = %s",
                (body.username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not bcrypt.checkpw(body.password.encode(), row[0].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_jwt(body.username)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        secure=_auth.COOKIE_SECURE,
        samesite="lax",
        max_age=int(_auth.JWT_EXPIRY_HOURS * 3600),
    )
    return {"username": body.username}


@router.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("auth_token")
    return {"ok": True}


@router.get("/api/me")
def me(auth_token: str | None = Cookie(None)):
    username = get_optional_user(auth_token)
    if not username:
        return {"username": None}
    return {"username": username}
