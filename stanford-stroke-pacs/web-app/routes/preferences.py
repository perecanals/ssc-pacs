"""User preferences endpoints."""

from __future__ import annotations

import json

import psycopg2.extras
from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from auth import get_current_user, get_optional_user
from db import get_conn

router = APIRouter()

PREFS_VALID_LEVELS = ("patient", "study", "series", "_global")


@router.get("/api/preferences/{level}")
def get_preferences(level: str, auth_token: str | None = Cookie(None)):
    if level not in PREFS_VALID_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid level")
    username = get_optional_user(auth_token)
    if not username:
        return {"prefs": {}}
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT prefs FROM user_preferences WHERE username = %s AND level = %s",
                (username, level),
            )
            row = cur.fetchone()
            return {"prefs": row["prefs"] if row else {}}
    finally:
        conn.close()


class PrefsBody(BaseModel):
    prefs: dict


@router.put("/api/preferences/{level}")
def put_preferences(level: str, body: PrefsBody, auth_token: str | None = Cookie(None)):
    if level not in PREFS_VALID_LEVELS:
        raise HTTPException(status_code=400, detail="Invalid level")
    username = get_current_user(auth_token)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_preferences (username, level, prefs, updated_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (username, level)
                   DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = now()""",
                (username, level, json.dumps(body.prefs)),
            )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
