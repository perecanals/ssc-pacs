"""JWT authentication utilities and FastAPI dependencies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Cookie, Depends, HTTPException

from config import (
    COOKIE_SECURE,  # noqa: F401 — re-exported for runtime patching
    SESSION_ABSOLUTE_TIMEOUT_HOURS,
    SESSION_TIMEOUT_HOURS,
)
from db import _require_env, get_conn
import dataset_access

JWT_SECRET = _require_env("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = SESSION_TIMEOUT_HOURS
JWT_ABSOLUTE_TIMEOUT_SECONDS = int(SESSION_ABSOLUTE_TIMEOUT_HOURS * 3600)


def create_jwt(username: str, iat: int | None = None) -> str:
    now = datetime.now(UTC)
    iat_epoch = int(iat) if iat is not None else int(now.timestamp())
    payload = {
        "sub": username,
        "iat": iat_epoch,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    """Return the decoded payload or None if the token is invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
    iat = payload.get("iat")
    if iat is not None:
        try:
            age = int(datetime.now(UTC).timestamp()) - int(iat)
        except (TypeError, ValueError):
            return None
        if age > JWT_ABSOLUTE_TIMEOUT_SECONDS:
            return None
    return payload


def get_current_user(auth_token: str | None = Cookie(None)) -> str:
    """FastAPI dependency: extracts username from JWT cookie or raises 401."""
    if not auth_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_jwt(auth_token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload["sub"]


def get_optional_user(auth_token: str | None = Cookie(None)) -> str | None:
    """Return username if logged in, None otherwise. Never raises."""
    if not auth_token:
        return None
    payload = decode_jwt(auth_token)
    return payload.get("sub") if payload else None


def require_admin(user: str = Depends(get_current_user)) -> str:
    """FastAPI dependency: 401 if not logged in, 403 if user is not an admin."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_admin FROM users WHERE username = %s",
                (user,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_dataset_scope(user: str = Depends(get_current_user)) -> list[str] | None:
    """FastAPI dependency: the caller's dataset scope (401 if not logged in).

    Returns ``None`` for admins (unrestricted) or a — possibly empty —
    sorted list of allowed dataset tags. Deny-by-default: an empty list
    means the user sees no patient data. Uncached, like ``require_admin``
    (one indexed PK lookup per request).
    """
    scope = dataset_access.fetch_user_scope(user)
    return None if scope is None else sorted(scope)
