"""FastAPI companion app for multi-level (patient/study/series) annotations."""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import bcrypt
import jwt
import psycopg2
import psycopg2.extras
import requests as http_requests
from zipstream import ZipStream
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import SESSION_TIMEOUT_HOURS, STORAGE_MODE
from cache_manager import (
    evict_study,
    get_cache_status,
    resolve_series_archive,
    run_eviction,
    touch_access,
    untar_zst,
    warm_study,
)
from labelled_table_sync import (
    ensure_labelled_tables,
    find_label_column_conflict,
    rebuild_labelled_tables,
    sync_labelled_rows,
    sync_labelled_schema,
)

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER")
ORTHANC_PASS = os.getenv("ORTHANC_ADMIN_PASSWORD")

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = SESSION_TIMEOUT_HOURS

VALID_LEVELS = ("patient", "study", "series")

_log = logging.getLogger("uvicorn.error")

INIT_SQL = """
CREATE TABLE IF NOT EXISTS annotations (
    id                  SERIAL PRIMARY KEY,
    seriesinstanceuid   TEXT NOT NULL,
    studyinstanceuid    TEXT NOT NULL,
    patient_id          TEXT,
    label               TEXT NOT NULL,
    value               TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now(),
    notes               TEXT,
    UNIQUE(seriesinstanceuid, label, created_by)
);
CREATE INDEX IF NOT EXISTS idx_annotations_label ON annotations(label);
CREATE INDEX IF NOT EXISTS idx_annotations_series ON annotations(seriesinstanceuid);

CREATE TABLE IF NOT EXISTS label_definitions (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    datatype    TEXT NOT NULL DEFAULT 'bool'
                CHECK (datatype IN ('bool', 'int', 'text', 'select')),
    options     TEXT,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT now()
);
"""

MIGRATE_SQL = """
-- Legacy migration: add value column if missing
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'annotations' AND column_name = 'value'
    ) THEN
        ALTER TABLE annotations ADD COLUMN value TEXT;
    END IF;
END $$;

-- Legacy migration: update datatype check constraint
DO $$
DECLARE
    cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
    WHERE conrelid = 'label_definitions'::regclass AND contype = 'c' LIMIT 1;
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE label_definitions DROP CONSTRAINT %I', cname);
    END IF;
    ALTER TABLE label_definitions ADD CONSTRAINT label_definitions_datatype_check
        CHECK (datatype IN ('bool', 'int', 'text', 'select'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Legacy migration: add options column if missing
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'label_definitions' AND column_name = 'options'
    ) THEN
        ALTER TABLE label_definitions ADD COLUMN options TEXT;
    END IF;
END $$;

-- Multi-level migration: add level column to annotations
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'annotations' AND column_name = 'level'
    ) THEN
        ALTER TABLE annotations ADD COLUMN level TEXT NOT NULL DEFAULT 'series';
        ALTER TABLE annotations ADD CONSTRAINT annotations_level_check
            CHECK (level IN ('patient', 'study', 'series'));
    END IF;
END $$;

-- Multi-level migration: relax NOT NULL on seriesinstanceuid / studyinstanceuid
DO $$
BEGIN
    ALTER TABLE annotations ALTER COLUMN seriesinstanceuid DROP NOT NULL;
    ALTER TABLE annotations ALTER COLUMN studyinstanceuid DROP NOT NULL;
EXCEPTION WHEN others THEN NULL;
END $$;

-- Multi-level migration: drop old unique constraint and add partial unique indexes
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'annotations_seriesinstanceuid_label_created_by_key'
    ) THEN
        ALTER TABLE annotations
            DROP CONSTRAINT annotations_seriesinstanceuid_label_created_by_key;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_unique_series
    ON annotations(seriesinstanceuid, label, created_by) WHERE level = 'series';
CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_unique_study
    ON annotations(studyinstanceuid, label, created_by) WHERE level = 'study';
CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_unique_patient
    ON annotations(patient_id, label, created_by) WHERE level = 'patient';

-- Multi-level migration: add level column to label_definitions
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'label_definitions' AND column_name = 'level'
    ) THEN
        ALTER TABLE label_definitions ADD COLUMN level TEXT NOT NULL DEFAULT 'series';
        ALTER TABLE label_definitions ADD CONSTRAINT label_definitions_level_check
            CHECK (level IN ('patient', 'study', 'series'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_annotations_study ON annotations(studyinstanceuid);
CREATE INDEX IF NOT EXISTS idx_annotations_patient ON annotations(patient_id);
CREATE INDEX IF NOT EXISTS idx_annotations_level ON annotations(level);

-- Shared annotations migration: make annotations global (one value per entity+label)
-- Keep created_by for audit, but remove it from uniqueness.
DO $$
DECLARE
    idx_def TEXT;
BEGIN
    SELECT indexdef INTO idx_def FROM pg_indexes
    WHERE indexname = 'idx_ann_unique_series';

    IF idx_def IS NOT NULL AND idx_def LIKE '%created_by%' THEN
        -- Deduplicate: for each (level, entity, label) keep the most recent row
        DELETE FROM annotations a
        USING (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY level,
                    COALESCE(seriesinstanceuid, ''),
                    COALESCE(studyinstanceuid, ''),
                    COALESCE(patient_id, ''),
                    label
                ORDER BY created_at DESC NULLS LAST, id DESC
            ) AS rn
            FROM annotations
        ) ranked
        WHERE a.id = ranked.id AND ranked.rn > 1;

        DROP INDEX IF EXISTS idx_ann_unique_series;
        DROP INDEX IF EXISTS idx_ann_unique_study;
        DROP INDEX IF EXISTS idx_ann_unique_patient;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_shared_series
    ON annotations(seriesinstanceuid, label) WHERE level = 'series';
CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_shared_study
    ON annotations(studyinstanceuid, label) WHERE level = 'study';
CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_shared_patient
    ON annotations(patient_id, label) WHERE level = 'patient';

CREATE TABLE IF NOT EXISTS user_preferences (
    username   TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    level      TEXT NOT NULL CHECK (level IN ('patient', 'study', 'series', '_global')),
    prefs      JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (username, level)
);

-- Cold storage / hot cache (requires existing image_series table in this database)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'image_series'
    ) THEN
        ALTER TABLE image_series ADD COLUMN IF NOT EXISTS dicom_archive_path TEXT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS cache_state (
    studyinstanceuid TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'cold'
        CHECK (status IN ('cold', 'warming', 'hot', 'error')),
    cache_path TEXT,
    warmed_at TIMESTAMPTZ,
    last_accessed_at TIMESTAMPTZ,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_state_status ON cache_state(status);
CREATE INDEX IF NOT EXISTS idx_cache_state_last_accessed ON cache_state(last_accessed_at);

CREATE TABLE IF NOT EXISTS orthanc_resource_map (
    orthanc_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('study', 'series', 'instance')),
    studyinstanceuid TEXT NOT NULL REFERENCES cache_state(studyinstanceuid) ON DELETE CASCADE,
    seriesinstanceuid TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orm_study ON orthanc_resource_map(studyinstanceuid);
"""

DIST_DIR = Path(__file__).parent / "dist"

SERIES_FROM_CLAUSE = (
    "image_series s "
    "LEFT JOIN image_study st ON s.studyinstanceuid = st.studyinstanceuid"
)

PATIENT_ID_COL = "study_id"


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(INIT_SQL)
            cur.execute(MIGRATE_SQL)
        ensure_labelled_tables(conn)
        conn.commit()
    finally:
        conn.close()


async def _eviction_loop() -> None:
    while True:
        await asyncio.sleep(900)
        try:
            evicted = run_eviction()
            if evicted:
                _log.info("Cold cache eviction removed %d studies: %s", len(evicted), evicted[:10])
        except Exception:
            _log.exception("Cold cache eviction failed")


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    ev_task: asyncio.Task | None = None
    if STORAGE_MODE == "cold_path_cache":
        ev_task = asyncio.create_task(_eviction_loop())
    try:
        yield
    finally:
        if ev_task is not None:
            ev_task.cancel()
            try:
                await ev_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="SSC Series Annotations", lifespan=lifespan)

if DIST_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")


@app.middleware("http")
async def sliding_jwt(request, call_next):
    """Refresh the JWT on every meaningful request so the session stays alive
    as long as the user is active.  /api/me is excluded so that status-check
    polling does not prevent expiry."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/") or path == "/api/me":
        return response
    token = request.cookies.get("auth_token")
    if token:
        username = decode_jwt(token)
        if username:
            response.set_cookie(
                key="auth_token",
                value=create_jwt(username),
                httponly=True,
                samesite="lax",
                max_age=int(JWT_EXPIRY_HOURS * 3600),
            )
    return response


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def create_jwt(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> str | None:
    """Return the username or None if the token is invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_current_user(auth_token: str | None = Cookie(None)) -> str:
    """FastAPI dependency: extracts username from JWT cookie or raises 401."""
    if not auth_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = decode_jwt(auth_token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return username


def get_optional_user(auth_token: str | None = Cookie(None)) -> str | None:
    """Return username if logged in, None otherwise. Never raises."""
    if not auth_token:
        return None
    return decode_jwt(auth_token)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginRequest, response: Response):
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
        samesite="lax",
        max_age=int(JWT_EXPIRY_HOURS * 3600),
    )
    return {"username": body.username}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("auth_token")
    return {"ok": True}


@app.get("/api/me")
def me(auth_token: str | None = Cookie(None)):
    username = get_optional_user(auth_token)
    if not username:
        return {"username": None}
    return {"username": username}


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------

PREFS_VALID_LEVELS = ("patient", "study", "series", "_global")


@app.get("/api/preferences/{level}")
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


@app.put("/api/preferences/{level}")
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


# ---------------------------------------------------------------------------
# SPA fallback — serve React app for all non-API routes
# ---------------------------------------------------------------------------

def _serve_index():
    index = DIST_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=503, detail="Frontend not built — run npm run build")


# ---------------------------------------------------------------------------
# Series browsing
# ---------------------------------------------------------------------------

SERIES_SORT_WHITELIST = {"patient_id", "import_id", "import_label", "acquisitiondatetime", "modality", "seriesdescription", "number_of_slices"}
STUDY_SORT_WHITELIST = {"patient_id", "import_id", "import_label", "acquisitiondatetime", "studyinstanceuid", "study_type"}
PATIENT_SORT_WHITELIST = {"patient_id", "stroke_date"}


def _label_filter_sql(
    entity_level: str,
    label_level: str | None,
    entity_id_expr: str,
) -> str:
    """Return a SQL fragment ``<entity_id_expr> IN (SELECT ...)`` that filters
    the current entity table by an annotation label at *label_level*.

    *entity_level* is the level of the current listing (patient / study / series).
    *entity_id_expr* is the column expression to match (e.g. ``p.study_id``,
    ``st.studyinstanceuid``, ``s.seriesinstanceuid``).
    *label_level* defaults to *entity_level* when ``None``.
    """
    ll = label_level if label_level in VALID_LEVELS else entity_level

    if entity_level == "patient":
        if ll == "patient":
            return f"{entity_id_expr} IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)"
        if ll == "study":
            return (
                f"{entity_id_expr} IN ("
                "SELECT patient_id FROM image_study WHERE studyinstanceuid IN "
                "(SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s))"
            )
        return (
            f"{entity_id_expr} IN ("
            "SELECT patient_id FROM image_series WHERE seriesinstanceuid IN "
            "(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s))"
        )

    if entity_level == "study":
        if ll == "patient":
            return (
                f"st.patient_id IN "
                "(SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)"
            )
        if ll == "study":
            return f"{entity_id_expr} IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s)"
        return (
            f"{entity_id_expr} IN ("
            "SELECT studyinstanceuid FROM image_series WHERE seriesinstanceuid IN "
            "(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s))"
        )

    # entity_level == "series"
    if ll == "patient":
        return "s.patient_id IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)"
    if ll == "study":
        return "s.studyinstanceuid IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s)"
    return f"{entity_id_expr} IN (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s)"


def _label_value_filter_sql(
    entity_level: str,
    label_level: str | None,
    entity_id_expr: str,
) -> str:
    """Like _label_filter_sql but also matches on annotation value.

    Requires TWO %s params: (label_name, value_pattern).
    """
    ll = label_level if label_level in VALID_LEVELS else entity_level
    vp = "LOWER(COALESCE(value, '')) LIKE LOWER(%s)"

    if entity_level == "patient":
        if ll == "patient":
            return f"{entity_id_expr} IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s AND {vp})"
        if ll == "study":
            return (
                f"{entity_id_expr} IN ("
                "SELECT patient_id FROM image_study WHERE studyinstanceuid IN "
                f"(SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s AND {vp}))"
            )
        return (
            f"{entity_id_expr} IN ("
            "SELECT patient_id FROM image_series WHERE seriesinstanceuid IN "
            f"(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s AND {vp}))"
        )

    if entity_level == "study":
        if ll == "patient":
            return (
                "st.patient_id IN "
                f"(SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s AND {vp})"
            )
        if ll == "study":
            return f"{entity_id_expr} IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s AND {vp})"
        return (
            f"{entity_id_expr} IN ("
            "SELECT studyinstanceuid FROM image_series WHERE seriesinstanceuid IN "
            f"(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s AND {vp}))"
        )

    if ll == "patient":
        return f"s.patient_id IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s AND {vp})"
    if ll == "study":
        return f"s.studyinstanceuid IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s AND {vp})"
    return f"{entity_id_expr} IN (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s AND {vp})"


def _label_select_values_filter_sql(
    entity_level: str,
    label_level: str | None,
    entity_id_expr: str,
) -> str:
    """Like _label_value_filter_sql but matches an exact value from a selected set.

    Requires TWO %s params: (label_name, values_array).
    """
    ll = label_level if label_level in VALID_LEVELS else entity_level
    vp = "COALESCE(value, '') = ANY(%s)"

    if entity_level == "patient":
        if ll == "patient":
            return f"{entity_id_expr} IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s AND {vp})"
        if ll == "study":
            return (
                f"{entity_id_expr} IN ("
                "SELECT patient_id FROM image_study WHERE studyinstanceuid IN "
                f"(SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s AND {vp}))"
            )
        return (
            f"{entity_id_expr} IN ("
            "SELECT patient_id FROM image_series WHERE seriesinstanceuid IN "
            f"(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s AND {vp}))"
        )

    if entity_level == "study":
        if ll == "patient":
            return (
                "st.patient_id IN "
                f"(SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s AND {vp})"
            )
        if ll == "study":
            return f"{entity_id_expr} IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s AND {vp})"
        return (
            f"{entity_id_expr} IN ("
            "SELECT studyinstanceuid FROM image_series WHERE seriesinstanceuid IN "
            f"(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s AND {vp}))"
        )

    if ll == "patient":
        return f"s.patient_id IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s AND {vp})"
    if ll == "study":
        return f"s.studyinstanceuid IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s AND {vp})"
    return f"{entity_id_expr} IN (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s AND {vp})"


def _label_bool_filter_sql(
    entity_level: str,
    label_level: str | None,
    entity_id_expr: str,
    *,
    exists: bool = True,
) -> str:
    """Filter by whether a bool annotation row EXISTS (or NOT EXISTS).

    Requires ONE %s param: label_name.
    """
    ll = label_level if label_level in VALID_LEVELS else entity_level
    op = "IN" if exists else "NOT IN"

    if entity_level == "patient":
        if ll == "patient":
            return f"{entity_id_expr} {op} (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)"
        if ll == "study":
            return (
                f"{entity_id_expr} {op} ("
                "SELECT patient_id FROM image_study WHERE studyinstanceuid IN "
                "(SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s))"
            )
        return (
            f"{entity_id_expr} {op} ("
            "SELECT patient_id FROM image_series WHERE seriesinstanceuid IN "
            "(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s))"
        )

    if entity_level == "study":
        if ll == "patient":
            return (
                f"st.patient_id {op} "
                "(SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)"
            )
        if ll == "study":
            return f"{entity_id_expr} {op} (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s)"
        return (
            f"{entity_id_expr} {op} ("
            "SELECT studyinstanceuid FROM image_series WHERE seriesinstanceuid IN "
            "(SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s))"
        )

    if ll == "patient":
        return f"s.patient_id {op} (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)"
    if ll == "study":
        return f"s.studyinstanceuid {op} (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s)"
    return f"{entity_id_expr} {op} (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s)"


def _parse_label_filters(raw: str | None) -> list[dict[str, object]]:
    """Parse the label_filters JSON query param into a validated list."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        raw_values = item.get("values")
        values = []
        if isinstance(raw_values, list):
            values = [str(v).strip() for v in raw_values if str(v).strip()]
        lvl = str(item.get("level", "")).strip()
        datatype = str(item.get("datatype", "")).strip()
        if datatype == "select":
            if not values and value:
                values = [value]
            if not label or not values:
                continue
        elif not label or not value:
            continue
        out.append({
            "label": label,
            "value": value,
            "values": values,
            "level": lvl if lvl in VALID_LEVELS else "",
            "datatype": datatype,
        })
    return out


def _apply_label_filters(parsed_filters, entity_level, entity_id_expr, conditions, params):
    """Append SQL conditions + params for parsed label filters."""
    for lf in parsed_filters:
        if lf["datatype"] == "bool":
            exists = lf["value"] == "true"
            conditions.append(
                _label_bool_filter_sql(entity_level, lf["level"], entity_id_expr, exists=exists)
            )
            params.append(lf["label"])
        elif lf["datatype"] == "select":
            values = [str(v).strip() for v in lf.get("values", []) if str(v).strip()]
            if not values and lf.get("value"):
                values = [str(lf["value"]).strip()]
            if not values:
                continue
            conditions.append(
                _label_select_values_filter_sql(entity_level, lf["level"], entity_id_expr)
            )
            params.extend([lf["label"], values])
        else:
            conditions.append(
                _label_value_filter_sql(entity_level, lf["level"], entity_id_expr)
            )
            params.extend([lf["label"], f"%{lf['value']}%"])


def _format_ann(a: dict) -> dict:
    return {
        "id": a["id"],
        "level": a.get("level", "series"),
        "label": a["label"],
        "value": a["value"],
        "created_by": a["created_by"],
        "created_at": a["created_at"].isoformat() if a["created_at"] else None,
        "notes": a["notes"],
    }


def _attach_annotations(cur, rows, level, id_col):
    """Fetch annotations for a batch of rows keyed by id_col at the given level."""
    if not rows:
        for r in rows:
            r["annotations"] = []
        return
    ids = [r[id_col] for r in rows]
    cur.execute(
        f"SELECT {id_col}, id, level, label, value, created_by, created_at, notes "
        f"FROM annotations WHERE level = %s AND {id_col} = ANY(%s) "
        f"ORDER BY created_at",
        (level, ids),
    )
    ann_map: dict[str, list] = {}
    for a in cur.fetchall():
        ann_map.setdefault(a[id_col], []).append(_format_ann(a))
    for r in rows:
        r["annotations"] = ann_map.get(r[id_col], [])


def _attach_inherited_annotations(cur, rows, child_level):
    """Attach parent-level annotations inherited from above."""
    if not rows:
        return
    if child_level == "series":
        study_uids = list({r["studyinstanceuid"] for r in rows if r.get("studyinstanceuid")})
        patient_ids = list({r["patient_id"] for r in rows if r.get("patient_id")})
        study_anns: dict[str, list] = {}
        patient_anns: dict[str, list] = {}
        if study_uids:
            cur.execute(
                "SELECT studyinstanceuid, id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE level = 'study' AND studyinstanceuid = ANY(%s) "
                "ORDER BY created_at",
                (study_uids,),
            )
            for a in cur.fetchall():
                study_anns.setdefault(a["studyinstanceuid"], []).append(_format_ann(a))
        if patient_ids:
            cur.execute(
                "SELECT patient_id, id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE level = 'patient' AND patient_id = ANY(%s) "
                "ORDER BY created_at",
                (patient_ids,),
            )
            for a in cur.fetchall():
                patient_anns.setdefault(a["patient_id"], []).append(_format_ann(a))
        for r in rows:
            inherited = []
            inherited.extend(patient_anns.get(r.get("patient_id", ""), []))
            inherited.extend(study_anns.get(r.get("studyinstanceuid", ""), []))
            r["inherited_annotations"] = inherited
    elif child_level == "study":
        patient_ids = list({r["patient_id"] for r in rows if r.get("patient_id")})
        patient_anns: dict[str, list] = {}
        if patient_ids:
            cur.execute(
                "SELECT patient_id, id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE level = 'patient' AND patient_id = ANY(%s) "
                "ORDER BY created_at",
                (patient_ids,),
            )
            for a in cur.fetchall():
                patient_anns.setdefault(a["patient_id"], []).append(_format_ann(a))
        for r in rows:
            r["inherited_annotations"] = patient_anns.get(r.get("patient_id", ""), [])
    else:
        for r in rows:
            r["inherited_annotations"] = []


# ---------------------------------------------------------------------------
# Patient browsing
# ---------------------------------------------------------------------------

@app.get("/api/patients")
def list_patients(
    patient_id: str | None = Query(None),
    stroke_date: str | None = Query(None),
    study_import_label: str | None = Query(
        None,
        description=(
            "Exact match on import_label across image_study/image_series; "
            "patient included if any study/series has this label."
        ),
    ),
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            conditions.append(
                f"p.{PATIENT_ID_COL} IN (SELECT DISTINCT patient_id FROM image_study)"
            )

            if patient_id:
                conditions.append(f"p.{PATIENT_ID_COL}::text LIKE %s")
                params.append(f"%{patient_id}%")
            if stroke_date:
                conditions.append("p.stroke_date::text LIKE %s")
                params.append(f"%{stroke_date}%")
            sil = (study_import_label or "").strip()
            if sil:
                conditions.append(
                    f"p.{PATIENT_ID_COL} IN ("
                    "SELECT DISTINCT patient_id FROM image_study st WHERE st.import_label = %s "
                    "UNION "
                    "SELECT DISTINCT patient_id FROM image_series s WHERE s.import_label = %s)"
                )
                params.append(sil)
                params.append(sil)
            if label:
                conditions.append(
                    _label_filter_sql("patient", label_level, f"p.{PATIENT_ID_COL}")
                )
                params.append(label)
            _apply_label_filters(
                _parse_label_filters(label_filters),
                "patient", f"p.{PATIENT_ID_COL}", conditions, params,
            )

            where = "WHERE " + " AND ".join(conditions)
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(*) FROM lvo_clinical_data p {where}", params
            )
            total = cur.fetchone()["count"]

            col_map = {"patient_id": PATIENT_ID_COL, "stroke_date": "stroke_date"}
            col = col_map.get(sort_by, PATIENT_ID_COL)
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            study_labels_agg = (
                "COALESCE(("
                "  SELECT string_agg(lbl, ', ' ORDER BY lbl) FROM ("
                "    SELECT DISTINCT TRIM(sti.import_label) AS lbl "
                "    FROM image_study sti "
                f"    WHERE sti.patient_id = p.{PATIENT_ID_COL} "
                "      AND sti.import_label IS NOT NULL AND TRIM(sti.import_label) <> '' "
                "    UNION "
                "    SELECT DISTINCT TRIM(s.import_label) AS lbl "
                "    FROM image_series s "
                f"    WHERE s.patient_id = p.{PATIENT_ID_COL} "
                "      AND s.import_label IS NOT NULL AND TRIM(s.import_label) <> '' "
                "  ) u"
                "), '') AS study_import_labels"
            )
            cur.execute(
                f"SELECT p.{PATIENT_ID_COL} AS patient_id, p.stroke_date, {study_labels_agg} "
                f"FROM lvo_clinical_data p {where} "
                f"ORDER BY p.{col} {direction} NULLS LAST "
                f"LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()

            _attach_annotations(cur, rows, "patient", "patient_id")
            _attach_inherited_annotations(cur, rows, "patient")

        return {"total": total, "page": page, "per_page": per_page, "items": rows}
    finally:
        conn.close()


@app.get("/api/patients/{patient_id}/studies")
def patient_studies(
    patient_id: str,
    study_import_label: str | None = Query(
        None,
        description="If set, only studies connected to this import_label are returned.",
    ),
):
    """Studies for a patient (expandable sub-rows); optionally filtered by import_label."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sil = (study_import_label or "").strip()
            where_st = "st.patient_id = %s"
            qparams: list = [patient_id]
            if sil:
                where_st += (
                    " AND ("
                    "st.import_label = %s OR EXISTS ("
                    "  SELECT 1 FROM image_series s "
                    "  WHERE s.studyinstanceuid = st.studyinstanceuid AND s.import_label = %s"
                    "))"
                )
                qparams.append(sil)
                qparams.append(sil)
            cur.execute(
                "SELECT st.patient_id, st.import_id, st.import_label, st.acquisitiondatetime, st.studyinstanceuid, "
                "st.studydescription, st.study_type, "
                "COALESCE(("
                "  SELECT string_agg(DISTINCT s.modality, ', ' ORDER BY s.modality) "
                "  FROM image_series s WHERE s.studyinstanceuid = st.studyinstanceuid"
                "), '') AS modality "
                "FROM image_study st "
                f"WHERE {where_st} "
                "ORDER BY st.acquisitiondatetime",
                tuple(qparams),
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            _attach_annotations(cur, rows, "study", "studyinstanceuid")
            _attach_inherited_annotations(cur, rows, "study")

        return rows
    finally:
        conn.close()


@app.get("/api/study-import-labels")
def list_study_import_labels():
    """Distinct non-empty `import_label` values (study+series) for patient-level filter UI."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT import_label FROM ("
                "  SELECT DISTINCT TRIM(import_label) AS import_label FROM image_study "
                "  WHERE import_label IS NOT NULL AND TRIM(import_label) <> '' "
                "  UNION "
                "  SELECT DISTINCT TRIM(import_label) AS import_label FROM image_series "
                "  WHERE import_label IS NOT NULL AND TRIM(import_label) <> '' "
                ") u ORDER BY import_label",
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Study browsing
# ---------------------------------------------------------------------------

@app.get("/api/studies")
def list_studies(
    patient_id: str | None = Query(None),
    import_id: str | None = Query(None),
    import_label: str | None = Query(None),
    modality: str | None = Query(None),
    study_type: str | None = Query(None),
    studydescription: str | None = Query(None),
    acquisitiondatetime: str | None = Query(None),
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            if patient_id:
                conditions.append("st.patient_id LIKE %s")
                params.append(f"%{patient_id}%")
            if import_id:
                conditions.append("st.import_id::text LIKE %s")
                params.append(f"%{import_id}%")
            if import_label:
                conditions.append("LOWER(COALESCE(st.import_label, '')) LIKE LOWER(%s)")
                params.append(f"%{import_label}%")
            if study_type:
                conditions.append("UPPER(st.study_type) = UPPER(%s)")
                params.append(study_type)
            if studydescription:
                conditions.append("LOWER(st.studydescription) LIKE LOWER(%s)")
                params.append(f"%{studydescription}%")
            if acquisitiondatetime:
                conditions.append("st.acquisitiondatetime::text LIKE %s")
                params.append(f"%{acquisitiondatetime}%")
            if modality:
                conditions.append(
                    "st.studyinstanceuid IN ("
                    "  SELECT s2.studyinstanceuid FROM image_series s2 "
                    "  WHERE UPPER(s2.modality) LIKE UPPER(%s))"
                )
                params.append(f"%{modality}%")
            if label:
                conditions.append(
                    _label_filter_sql("study", label_level, "st.studyinstanceuid")
                )
                params.append(label)
            _apply_label_filters(
                _parse_label_filters(label_filters),
                "study", "st.studyinstanceuid", conditions, params,
            )

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(*) FROM image_study st {where}", params
            )
            total = cur.fetchone()["count"]

            col_map = {
                "patient_id": "patient_id",
                "import_id": "import_id",
                "import_label": "import_label",
                "acquisitiondatetime": "acquisitiondatetime",
                "studydescription": "studydescription",
                "study_type": "study_type",
            }
            col = col_map.get(sort_by, "patient_id")
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            cur.execute(
                f"SELECT st.patient_id, st.import_id, st.import_label, st.acquisitiondatetime, "
                f"st.studyinstanceuid, st.studydescription, st.study_type, "
                f"COALESCE(("
                f"  SELECT string_agg(DISTINCT s.modality, ', ' ORDER BY s.modality) "
                f"  FROM image_series s WHERE s.studyinstanceuid = st.studyinstanceuid"
                f"), '') AS modality "
                f"FROM image_study st {where} "
                f"ORDER BY st.{col} {direction} NULLS LAST "
                f"LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            _attach_annotations(cur, rows, "study", "studyinstanceuid")
            _attach_inherited_annotations(cur, rows, "study")

        return {"total": total, "page": page, "per_page": per_page, "items": rows}
    finally:
        conn.close()


@app.get("/api/studies/{studyinstanceuid}/series")
def study_series(studyinstanceuid: str):
    """All series for a given study (for expandable sub-rows)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT s.seriesinstanceuid, s.studyinstanceuid, s.patient_id, s.import_id, s.import_label, "
                "s.modality, s.seriesdescription, s.acquisitiondatetime, s.number_of_slices "
                "FROM image_series s WHERE s.studyinstanceuid = %s "
                "ORDER BY s.acquisitiondatetime, s.seriesdescription",
                (studyinstanceuid,),
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            _attach_annotations(cur, rows, "series", "seriesinstanceuid")
            _attach_inherited_annotations(cur, rows, "series")

        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Series browsing
# ---------------------------------------------------------------------------

@app.get("/api/series")
def list_series(
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    patient_id: str | None = Query(None),
    import_id: str | None = Query(None),
    import_label: str | None = Query(None),
    modality: str | None = Query(None),
    description: str | None = Query(None),
    study_type: str | None = Query(None),
    acquisitiondatetime: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    """Paginated series list, optionally filtered. LEFT JOINs with annotations."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            if label:
                conditions.append(
                    _label_filter_sql("series", label_level, "s.seriesinstanceuid")
                )
                params.append(label)
            if patient_id:
                conditions.append("s.patient_id LIKE %s")
                params.append(f"%{patient_id}%")
            if import_id:
                conditions.append("s.import_id::text LIKE %s")
                params.append(f"%{import_id}%")
            if import_label:
                conditions.append("LOWER(COALESCE(s.import_label, '')) LIKE LOWER(%s)")
                params.append(f"%{import_label}%")
            if modality:
                conditions.append("UPPER(s.modality) LIKE UPPER(%s)")
                params.append(f"%{modality}%")
            if description:
                conditions.append("LOWER(s.seriesdescription) LIKE LOWER(%s)")
                params.append(f"%{description}%")
            if study_type:
                conditions.append("UPPER(st.study_type) = UPPER(%s)")
                params.append(study_type)
            if acquisitiondatetime:
                conditions.append("s.acquisitiondatetime::text LIKE %s")
                params.append(f"%{acquisitiondatetime}%")
            _apply_label_filters(
                _parse_label_filters(label_filters),
                "series", "s.seriesinstanceuid", conditions, params,
            )

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(DISTINCT s.seriesinstanceuid) "
                f"FROM {SERIES_FROM_CLAUSE} {where}",
                params,
            )
            total = cur.fetchone()["count"]

            col = sort_by if sort_by in SERIES_SORT_WHITELIST else "patient_id"
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (s.seriesinstanceuid)
                        s.seriesinstanceuid,
                        s.studyinstanceuid,
                        s.patient_id,
                        s.import_id,
                        s.import_label,
                        st.study_type,
                        s.modality,
                        s.seriesdescription,
                        s.acquisitiondatetime,
                        s.number_of_slices
                    FROM {SERIES_FROM_CLAUSE}
                    {where}
                    ORDER BY s.seriesinstanceuid
                ) sub
                ORDER BY sub.{col} {direction} NULLS LAST
                LIMIT %s OFFSET %s
                """,
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            _attach_annotations(cur, rows, "series", "seriesinstanceuid")
            _attach_inherited_annotations(cur, rows, "series")

        return {"total": total, "page": page, "per_page": per_page, "series": rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cold storage / hot cache API
# ---------------------------------------------------------------------------


@app.get("/api/storage-mode")
def api_storage_mode():
    return {"storage_mode": STORAGE_MODE}


@app.post("/api/studies/{studyinstanceuid}/warm")
def api_warm_study(studyinstanceuid: str, auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
    return warm_study(studyinstanceuid)


@app.post("/api/studies/{studyinstanceuid}/evict")
def api_evict_study(studyinstanceuid: str, auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
    return evict_study(studyinstanceuid)


@app.get("/api/studies/{studyinstanceuid}/cache-status")
def api_cache_status(studyinstanceuid: str):
    return get_cache_status(studyinstanceuid)


# ---------------------------------------------------------------------------
# DICOM zip download
# ---------------------------------------------------------------------------

@app.get("/api/series/{seriesinstanceuid}/dicom-zip")
def download_dicom_zip(seriesinstanceuid: str):
    """Stream a `.zip` of the series' DICOMs.

    Cold mode: extract the cold `.tar.zst` archive to a tempdir, then
    stream a zip of that directory. macOS's built-in Archive Utility
    handles standard zip but not tar.zst, hence the conversion.

    Legacy mode: zip the loose DICOM directory directly.

    Both modes produce a zip whose single top-level folder is
    `{patient_id}_{seriesdescription}` (sanitized), so the unzipped
    output is self-identifying.
    """
    import re

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT patient_id, acquisitiondatetime, seriesdescription, dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE seriesinstanceuid = %s LIMIT 1",
                (seriesinstanceuid,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Series not found")

    pid = row.get("patient_id") or "unknown"
    dt = row.get("acquisitiondatetime")
    date_str = dt.strftime("%Y%m%d") if dt else "nodate"
    desc = row.get("seriesdescription") or "series"
    safe = re.sub(r"[^\w\-.]", "_", f"{pid}-{date_str}-{desc}")
    folder_name = re.sub(r"[^\w\-.]", "_", f"{pid}_{desc}")
    filename = f"{safe}.zip"

    if STORAGE_MODE == "cold_path_cache":
        arch = resolve_series_archive(row.get("dicom_archive_path"), row.get("dicom_dir_path"))
        if arch and arch.is_file():
            tmpdir = tempfile.mkdtemp(prefix="dicom-zip-")
            try:
                untar_zst(arch, Path(tmpdir))
                zs = ZipStream.from_path(tmpdir, arcname=folder_name)
                content_length = len(zs)

                def gen():
                    try:
                        yield from zs
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)

                return StreamingResponse(
                    gen(),
                    media_type="application/zip",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Length": str(content_length),
                    },
                )
            except Exception:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise

    if not row.get("dicom_dir_path"):
        raise HTTPException(status_code=404, detail="DICOM path not found for this series")

    dicom_dir = Path(row["dicom_dir_path"])
    if not dicom_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"DICOM directory does not exist: {dicom_dir}")

    zs = ZipStream.from_path(str(dicom_dir), arcname=folder_name)

    return StreamingResponse(
        zs,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zs)),
        },
    )


# ---------------------------------------------------------------------------
# Annotations CRUD
# ---------------------------------------------------------------------------

class AnnotationCreate(BaseModel):
    level: str = "series"
    seriesinstanceuid: str | None = None
    studyinstanceuid: str | None = None
    patient_id: str | None = None
    label: str
    value: str | None = None
    notes: str | None = None


@app.get("/api/series/{seriesinstanceuid}/annotations")
def get_annotations(seriesinstanceuid: str):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE seriesinstanceuid = %s "
                "ORDER BY created_at",
                (seriesinstanceuid,),
            )
            return cur.fetchall()
    finally:
        conn.close()


_UPSERT_SQL = {
    "series": (
        "INSERT INTO annotations "
        "(level, seriesinstanceuid, studyinstanceuid, patient_id, label, value, created_by, notes) "
        "VALUES ('series', %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (seriesinstanceuid, label) WHERE level = 'series' DO UPDATE "
        "SET value = EXCLUDED.value, "
        "created_by = EXCLUDED.created_by, "
        "notes = COALESCE(EXCLUDED.notes, annotations.notes), "
        "created_at = now() "
        "RETURNING id, level, label, value, created_by, created_at, notes"
    ),
    "study": (
        "INSERT INTO annotations "
        "(level, studyinstanceuid, patient_id, label, value, created_by, notes) "
        "VALUES ('study', %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (studyinstanceuid, label) WHERE level = 'study' DO UPDATE "
        "SET value = EXCLUDED.value, "
        "created_by = EXCLUDED.created_by, "
        "notes = COALESCE(EXCLUDED.notes, annotations.notes), "
        "created_at = now() "
        "RETURNING id, level, label, value, created_by, created_at, notes"
    ),
    "patient": (
        "INSERT INTO annotations "
        "(level, patient_id, label, value, created_by, notes) "
        "VALUES ('patient', %s, %s, %s, %s, %s) "
        "ON CONFLICT (patient_id, label) WHERE level = 'patient' DO UPDATE "
        "SET value = EXCLUDED.value, "
        "created_by = EXCLUDED.created_by, "
        "notes = COALESCE(EXCLUDED.notes, annotations.notes), "
        "created_at = now() "
        "RETURNING id, level, label, value, created_by, created_at, notes"
    ),
}


@app.post("/api/annotations", status_code=201)
def create_annotation(body: AnnotationCreate, auth_token: str | None = Cookie(None)):
    username = get_current_user(auth_token)
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail="level must be patient, study, or series")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = _UPSERT_SQL[body.level]
            if body.level == "series":
                if not body.seriesinstanceuid:
                    raise HTTPException(status_code=400, detail="seriesinstanceuid required for series-level")
                params = (
                    body.seriesinstanceuid, body.studyinstanceuid, body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            elif body.level == "study":
                if not body.studyinstanceuid:
                    raise HTTPException(status_code=400, detail="studyinstanceuid required for study-level")
                params = (
                    body.studyinstanceuid, body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            else:
                if not body.patient_id:
                    raise HTTPException(status_code=400, detail="patient_id required for patient-level")
                params = (
                    body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            cur.execute(sql, params)
            row = cur.fetchone()
        entity_id = body.seriesinstanceuid if body.level == "series" else (
            body.studyinstanceuid if body.level == "study" else body.patient_id
        )
        sync_labelled_rows(conn, body.level, [entity_id])
        conn.commit()
        return row
    finally:
        conn.close()


@app.delete("/api/annotations/{annotation_id}", status_code=204)
def delete_annotation(annotation_id: int, auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, level, patient_id, studyinstanceuid, seriesinstanceuid "
                "FROM annotations WHERE id = %s",
                (annotation_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Annotation not found")
            cur.execute(
                "DELETE FROM annotations WHERE id = %s", (annotation_id,)
            )
        entity_id = row["seriesinstanceuid"] if row["level"] == "series" else (
            row["studyinstanceuid"] if row["level"] == "study" else row["patient_id"]
        )
        sync_labelled_rows(conn, row["level"], [entity_id])
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

@app.get("/api/labels")
def list_labels(level: str | None = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if level and level in VALID_LEVELS:
                cur.execute(
                    "SELECT DISTINCT label FROM annotations WHERE level = %s ORDER BY label",
                    (level,),
                )
            else:
                cur.execute("SELECT DISTINCT label FROM annotations ORDER BY label")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


_SUMMARY_COUNT_COL = {
    "patient": "patient_id",
    "study": "studyinstanceuid",
    "series": "seriesinstanceuid",
}


@app.get("/api/labels/summary")
def labels_summary(level: str | None = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if level and level in VALID_LEVELS:
                count_col = _SUMMARY_COUNT_COL[level]
                cur.execute(
                    f"SELECT label, level, COUNT(DISTINCT {count_col}) AS count "
                    f"FROM annotations WHERE level = %s GROUP BY label, level ORDER BY label",
                    (level,),
                )
            else:
                cur.execute(
                    "SELECT label, level, COUNT(*) AS count "
                    "FROM annotations GROUP BY label, level ORDER BY label"
                )
            return cur.fetchall()
    finally:
        conn.close()


@app.get("/api/labels/{label_name}/values")
def get_label_values(label_name: str):
    """Return the distinct annotation values already used for a label."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT value FROM annotations "
                "WHERE label = %s AND value IS NOT NULL AND value != '' "
                "ORDER BY value",
                (label_name,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Label definitions
# ---------------------------------------------------------------------------

class LabelDefinitionCreate(BaseModel):
    name: str
    description: str | None = None
    level: str = "series"
    datatype: str = "bool"
    options: list[str] | None = None


@app.get("/api/label-definitions")
def list_label_definitions(level: str | None = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if level and level in VALID_LEVELS:
                cur.execute(
                    "SELECT id, name, description, level, datatype, options, created_by, created_at "
                    "FROM label_definitions WHERE level = %s ORDER BY name",
                    (level,),
                )
            else:
                cur.execute(
                    "SELECT id, name, description, level, datatype, options, created_by, created_at "
                    "FROM label_definitions ORDER BY name"
                )
            rows = cur.fetchall()
            for r in rows:
                if r.get("created_at"):
                    r["created_at"] = r["created_at"].isoformat()
                r["options"] = json.loads(r["options"]) if r.get("options") else []
            return rows
    finally:
        conn.close()


@app.post("/api/label-definitions", status_code=201)
def create_label_definition(body: LabelDefinitionCreate, auth_token: str | None = Cookie(None)):
    username = get_current_user(auth_token)
    if body.datatype not in ("bool", "int", "text", "select"):
        raise HTTPException(status_code=400, detail="datatype must be bool, int, text, or select")
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail="level must be patient, study, or series")
    options_json = json.dumps(body.options) if body.options else None
    conn = get_conn()
    try:
        conflict = find_label_column_conflict(conn, body.level, body.name.strip())
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"Label name conflicts with existing column generated from '{conflict}'",
            )
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO label_definitions (name, description, level, datatype, options, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, name, description, level, datatype, options, created_by, created_at",
                (body.name.strip(), body.description, body.level, body.datatype, options_json, username),
            )
            row = cur.fetchone()
            if row.get("created_at"):
                row["created_at"] = row["created_at"].isoformat()
            row["options"] = json.loads(row["options"]) if row.get("options") else []
        sync_labelled_schema(conn, body.level)
        conn.commit()
        return row
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Label with this name already exists")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snapshot refresh
# ---------------------------------------------------------------------------

def _rebuild_snapshots(conn):
    """Rebuild the three snapshot tables from source data + annotations."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT name, level, datatype FROM label_definitions ORDER BY level, name"
        )
        label_defs = cur.fetchall()

    counts = {}

    for level_name, source_table, id_col, base_cols in [
        ("patient", "lvo_clinical_data", "patient_id",
         f"{PATIENT_ID_COL} AS patient_id, stroke_date"),
        ("study", "image_study", "studyinstanceuid",
         "patient_id, acquisitiondatetime, study_type, studyinstanceuid"),
        ("series", "image_series", "seriesinstanceuid",
         "patient_id, acquisitiondatetime, modality, seriesdescription, seriesinstanceuid"),
    ]:
        snapshot_table = f"snapshot_{level_name}s"
        level_labels = [ld for ld in label_defs if ld["level"] == level_name]

        pivot_cols = ""
        pivot_joins = ""
        for i, ld in enumerate(level_labels):
            alias = f"a{i}"
            safe_name = ld["name"].replace(" ", "_").replace("-", "_").lower()
            pivot_cols += f", {alias}.value AS label_{safe_name}"
            pivot_joins += (
                f" LEFT JOIN annotations {alias} ON {alias}.level = '{level_name}' "
                f"AND {alias}.{id_col} = src.{id_col} "
                f"AND {alias}.label = '{ld['name']}' "
            )

        src_alias = "src"
        if level_name == "patient":
            src_select = f"SELECT {base_cols} FROM {source_table}"
        else:
            src_select = f"SELECT {base_cols} FROM {source_table}"

        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {snapshot_table}")
            cur.execute(
                f"CREATE TABLE {snapshot_table} AS "
                f"SELECT DISTINCT ON ({src_alias}.{id_col}) "
                f"{src_alias}.*{pivot_cols} "
                f"FROM ({src_select}) {src_alias}{pivot_joins}"
            )
            cur.execute(f"SELECT COUNT(*) FROM {snapshot_table}")
            counts[snapshot_table] = cur.fetchone()[0]

    conn.commit()
    return counts


@app.post("/api/snapshots/refresh")
def refresh_snapshots(auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
    conn = get_conn()
    try:
        counts = _rebuild_snapshots(conn)
        return {"ok": True, "counts": counts}
    finally:
        conn.close()


@app.post("/api/labelled-tables/refresh")
def refresh_labelled_tables(
    auth_token: str | None = Cookie(None),
    level: list[str] | None = Query(None),
):
    get_current_user(auth_token)
    conn = get_conn()
    try:
        counts = rebuild_labelled_tables(conn, levels=level)
        conn.commit()
        return {"ok": True, "counts": counts}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OHIF link resolver
# ---------------------------------------------------------------------------

@app.get("/api/ohif-link/{studyinstanceuid}")
def ohif_link(
    studyinstanceuid: str,
    seriesinstanceuid: str | None = Query(None),
):
    """Resolve a StudyInstanceUID to an OHIF viewer URL via Orthanc lookup."""
    if STORAGE_MODE == "cold_path_cache":
        cs = get_cache_status(studyinstanceuid)
        st = cs.get("status") or "cold"
        if st == "warming":
            return {"status": "warming", "url": None}
        if st == "cold":
            return {
                "status": "cold",
                "url": None,
                "detail": "Study not warmed yet; POST /api/studies/{uid}/warm first",
            }
        if st == "error":
            raise HTTPException(
                status_code=503,
                detail=cs.get("error_message") or "Hot cache error for this study",
            )
        if st == "hot":
            # Defensive: verify files are actually on disk before trusting
            # cache_state. The row can drift from reality if files are moved
            # out-of-band (manual mv, eviction outside evict_study, etc.).
            # If the probe shows no files, clear the stale row and report
            # cold so the frontend triggers a fresh warm.
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT dicom_dir_path FROM image_series "
                        "WHERE studyinstanceuid = %s AND dicom_dir_path IS NOT NULL "
                        "LIMIT 1",
                        (studyinstanceuid,),
                    )
                    row = cur.fetchone()
                files_present = False
                if row and row[0]:
                    try:
                        files_present = bool(os.listdir(row[0]))
                    except OSError:
                        files_present = False
                if not files_present:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM cache_state WHERE studyinstanceuid = %s",
                            (studyinstanceuid,),
                        )
                    conn.commit()
                    return {
                        "status": "cold",
                        "url": None,
                        "detail": "Cache state was stale; files missing on disk",
                    }
            finally:
                conn.close()
            touch_access(studyinstanceuid)

    resp = http_requests.post(
        f"{ORTHANC_URL}/tools/lookup",
        data=studyinstanceuid,
        auth=(ORTHANC_USER, ORTHANC_PASS),
        timeout=5,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Orthanc lookup failed")
    for entry in resp.json():
        if entry.get("Type") == "Study":
            if seriesinstanceuid:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM image_series "
                            "WHERE studyinstanceuid = %s AND seriesinstanceuid = %s "
                            "LIMIT 1",
                            (studyinstanceuid, seriesinstanceuid),
                        )
                        if cur.fetchone() is None:
                            raise HTTPException(
                                status_code=404,
                                detail="Series not found in study",
                            )
                finally:
                    conn.close()

            query = {"StudyInstanceUIDs": studyinstanceuid}
            if seriesinstanceuid:
                query["SeriesInstanceUIDs"] = seriesinstanceuid
            url = f"{ORTHANC_URL}/ohif/viewer?{urlencode(query)}"
            if STORAGE_MODE == "cold_path_cache":
                return {"status": "ready", "url": url}
            return {"url": url}
    raise HTTPException(status_code=404, detail="Study not found in Orthanc")


# ---------------------------------------------------------------------------
# SPA catch-all — MUST be last
# ---------------------------------------------------------------------------

@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    return _serve_index()
