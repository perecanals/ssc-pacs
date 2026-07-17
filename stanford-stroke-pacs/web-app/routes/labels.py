"""Label definitions, label listing, and label summary endpoints."""

from __future__ import annotations

import json
import math

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user, is_user_admin
from common import (
    EDIT_POLICIES,
    LABEL_NAME_RE,
    VALID_LEVELS,
    can_change_label_policy,
    record_label_value,
)
from db import get_conn
from labelled_table_sync import (
    find_label_column_conflict,
    rebuild_labelled_tables,
    sync_labelled_schema,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Labels (from annotations table)
# ---------------------------------------------------------------------------


@router.get("/api/labels")
def list_labels(
    level: str | None = Query(None),
    user: str = Depends(get_current_user),
):
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


@router.get("/api/labels/summary")
def labels_summary(
    level: str | None = Query(None),
    user: str = Depends(get_current_user),
):
    # Note: summary counts are global (not narrowed to the caller's dataset
    # scope) — label names and aggregate counts only, no patient identifiers
    # or values. Documented limitation; see docs/reference/web_app.md.
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if level and level in VALID_LEVELS:
                count_col = _SUMMARY_COUNT_COL[level]
                cur.execute(
                    f"SELECT a.label, a.level, COUNT(DISTINCT a.{count_col}) AS count, "
                    "ld.instrument, MIN(ld.created_at) AS created_at "
                    "FROM annotations a "
                    "LEFT JOIN label_definitions ld "
                    "  ON ld.name = a.label AND ld.level = a.level "
                    "WHERE a.level = %s "
                    "GROUP BY a.label, a.level, ld.instrument "
                    "ORDER BY a.label",
                    (level,),
                )
            else:
                cur.execute(
                    "SELECT a.label, a.level, COUNT(*) AS count, "
                    "ld.instrument, MIN(ld.created_at) AS created_at "
                    "FROM annotations a "
                    "LEFT JOIN label_definitions ld "
                    "  ON ld.name = a.label AND ld.level = a.level "
                    "GROUP BY a.label, a.level, ld.instrument "
                    "ORDER BY a.label"
                )
            rows = cur.fetchall()
            for row in rows:
                if row.get("created_at"):
                    row["created_at"] = row["created_at"].isoformat()
            return rows
    finally:
        conn.close()


def _select_value_sort_key(value: str) -> tuple:
    """Sort key for select vocabularies: non-numeric strings first (naive
    lexicographic order), then purely numeric strings by numeric value — so
    score-style vocabularies (e.g. ASPECTS) read 0, 1, 2, …, 10 rather than
    the naive 0, 1, 10, 2, … Mirrors compareSelectValues in utils/table.js."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return (0, 0.0, value)
    if not math.isfinite(num):
        return (0, 0.0, value)
    return (1, num, value)


@router.get("/api/labels/{label_name}/values")
def get_label_values(
    label_name: str,
    user: str = Depends(get_current_user),
):
    """Known values (controlled vocabulary) for a select-type label, from the
    indexed ``label_value_options`` table. The vocabulary is global — value
    strings only, never patient data."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM label_value_options WHERE label = %s",
                (label_name,),
            )
            return sorted((r[0] for r in cur.fetchall()), key=_select_value_sort_key)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Label definitions
# ---------------------------------------------------------------------------


LABEL_DEF_COLUMNS = (
    "id, name, description, level, datatype, options, instrument, "
    "created_by, created_at, edit_policy, edit_users"
)


def _clean_optional_text(value: str | None) -> str | None:
    """Normalize a free-text optional field: trim, treat empty as NULL."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def serialize_label_def_row(row: dict) -> dict:
    if row.get("created_at"):
        row["created_at"] = row["created_at"].isoformat()
    row["options"] = json.loads(row["options"]) if row.get("options") else []
    # Sorted on the way out, like admin._serialize_user_row does for
    # allowed_datasets: it is what lets the frontend and tests assert exact lists.
    row["edit_users"] = sorted(row.get("edit_users") or [])
    return row


def _merge_select_value_options(cur, rows: list[dict]) -> None:
    """Replace each select-type def's ``options`` with the effective vocabulary:
    curated ``label_definitions.options`` ∪ live ``label_value_options`` — so
    values created inline reach the column filter. One batched query."""
    select_names = [r["name"] for r in rows if r.get("datatype") == "select"]
    if not select_names:
        return
    cur.execute(
        "SELECT label, value FROM label_value_options "
        "WHERE label = ANY(%s) ORDER BY value",
        (select_names,),
    )
    observed: dict[str, list[str]] = {}
    for r in cur.fetchall():
        observed.setdefault(r["label"], []).append(r["value"])
    for row in rows:
        if row.get("datatype") != "select":
            continue
        merged = dict.fromkeys([*row.get("options", []), *observed.get(row["name"], [])])
        row["options"] = sorted(merged, key=_select_value_sort_key)


def validate_edit_policy(cur, policy: str, users: list[str] | None) -> list[str]:
    """Validate an (edit_policy, edit_users) pair; return the normalized users.

    Normalized like set_user_datasets does for datasets: trim, drop empties,
    dedupe, sort. Usernames must exist (422 otherwise — same contract as the
    dataset grants, and the detail reaches the admin page's error banner).

    ``edit_users`` is forced empty unless the policy is ``users``, so the column
    can never hold a stale list that silently reactivates on a later flip.
    """
    if policy not in EDIT_POLICIES:
        raise HTTPException(
            status_code=400,
            detail=f"edit_policy must be one of: {', '.join(EDIT_POLICIES)}",
        )
    if policy != "users":
        return []
    names = sorted({u.strip() for u in (users or []) if u.strip()})
    if not names:
        raise HTTPException(
            status_code=422,
            detail="edit_policy 'users' needs at least one username "
                   "(an empty list is indistinguishable from 'nobody')",
        )
    cur.execute("SELECT username FROM users WHERE username = ANY(%s)", (names,))
    known = {r["username"] for r in cur.fetchall()}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown user(s): {', '.join(unknown)}",
        )
    return names


class LabelDefinitionCreate(BaseModel):
    name: str
    description: str | None = None
    level: str = "series"
    datatype: str = "bool"
    options: list[str] | None = None
    instrument: str | None = None
    edit_policy: str = "everyone"
    edit_users: list[str] | None = None


class LabelDefinitionUpdate(BaseModel):
    description: str | None = None
    instrument: str | None = None
    edit_policy: str | None = None
    edit_users: list[str] | None = None


@router.get("/api/label-definitions")
def list_label_definitions(
    level: str | None = Query(None),
    user: str = Depends(get_current_user),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if level and level in VALID_LEVELS:
                cur.execute(
                    f"SELECT {LABEL_DEF_COLUMNS} "
                    "FROM label_definitions WHERE level = %s ORDER BY name",
                    (level,),
                )
            else:
                cur.execute(
                    f"SELECT {LABEL_DEF_COLUMNS} "
                    "FROM label_definitions ORDER BY name"
                )
            rows = [serialize_label_def_row(r) for r in cur.fetchall()]
            _merge_select_value_options(cur, rows)
            return rows
    finally:
        conn.close()


@router.post("/api/label-definitions", status_code=201)
def create_label_definition(
    body: LabelDefinitionCreate,
    username: str = Depends(get_current_user),
):
    if body.datatype not in ("bool", "int", "text", "select"):
        raise HTTPException(status_code=400, detail="datatype must be bool, int, text, or select")
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail="level must be patient, study, or series")
    if not LABEL_NAME_RE.match((body.name or "").strip()):
        raise HTTPException(
            status_code=400,
            detail="name must match ^[A-Za-z][A-Za-z0-9_]{0,62}$ (letters, digits, underscores; must start with a letter; max 63 chars)",
        )
    options_json = json.dumps(body.options) if body.options else None
    instrument = _clean_optional_text(body.instrument)
    conn = get_conn()
    try:
        conflict = find_label_column_conflict(conn, body.level, body.name.strip())
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"Label name conflicts with existing column generated from '{conflict}'",
            )
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Any user may create a *restricted* label — a policy only ever
            # narrows who may write. The creator is the owner and can relax it.
            edit_users = validate_edit_policy(cur, body.edit_policy, body.edit_users)
            cur.execute(
                "INSERT INTO label_definitions "
                "(name, description, level, datatype, options, instrument, "
                " created_by, edit_policy, edit_users) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::text[]) "
                f"RETURNING {LABEL_DEF_COLUMNS}",
                (
                    body.name.strip(),
                    _clean_optional_text(body.description),
                    body.level,
                    body.datatype,
                    options_json,
                    instrument,
                    username,
                    body.edit_policy,
                    edit_users,
                ),
            )
            row = serialize_label_def_row(cur.fetchone())
            # Seed the vocabulary table with the curated options so they are
            # available to the inline dropdown and column filter from the start.
            if body.datatype == "select" and body.options:
                for opt in body.options:
                    record_label_value(cur, body.name.strip(), opt, username)
        sync_labelled_schema(conn, body.level)
        conn.commit()
        return row
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Label with this name already exists")
    finally:
        conn.close()


@router.patch("/api/label-definitions/{label_id}")
def update_label_definition(
    label_id: int,
    body: LabelDefinitionUpdate,
    user: str = Depends(get_current_user),
):
    """Edit `description`, `instrument`, and/or the edit policy on a label.

    Editing `name`, `level`, `datatype`, or `options` is intentionally
    out of scope — those are baked into the labelled-table sync and
    annotation entity-id constraints; renaming/retyping belongs in a
    dedicated migration flow.

    `description`/`instrument` stay editable by any authenticated user, as they
    always have been. Changing `edit_policy`/`edit_users` is restricted to the
    label's owner or an admin (`can_change_label_policy`) — otherwise the
    protection would be self-defeating, since anyone could simply unlock a label
    and then edit it.
    """
    fields = body.model_dump(exclude_unset=True)
    wants_policy = "edit_policy" in fields or "edit_users" in fields
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="No editable fields provided "
                   "(allowed: description, instrument, edit_policy, edit_users)",
        )

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT {LABEL_DEF_COLUMNS} FROM label_definitions WHERE id = %s",
                (label_id,),
            )
            existing = cur.fetchone()
            if existing is None:
                raise HTTPException(status_code=404, detail="Label definition not found")

            updates: list[str] = []
            params: list[object] = []
            if "description" in fields:
                updates.append("description = %s")
                params.append(_clean_optional_text(fields["description"]))
            if "instrument" in fields:
                updates.append("instrument = %s")
                params.append(_clean_optional_text(fields["instrument"]))

            if wants_policy:
                if not can_change_label_policy(
                    existing, user, is_user_admin(user)
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Only the label's owner or an admin may change "
                               "who can edit it",
                    )
                # Policy and users move together: changing one without the other
                # would let a stale list decide the outcome.
                policy = fields.get("edit_policy", existing["edit_policy"])
                users = fields.get("edit_users", existing["edit_users"])
                edit_users = validate_edit_policy(cur, policy, users)
                updates.append("edit_policy = %s")
                params.append(policy)
                updates.append("edit_users = %s::text[]")
                params.append(edit_users)

            params.append(label_id)
            cur.execute(
                f"UPDATE label_definitions SET {', '.join(updates)} "
                f"WHERE id = %s RETURNING {LABEL_DEF_COLUMNS}",
                params,
            )
            row = serialize_label_def_row(cur.fetchone())
        conn.commit()
        return row
    finally:
        conn.close()


@router.get("/api/instruments")
def list_instruments(user: str = Depends(get_current_user)):
    """Distinct non-null instrument values from label_definitions with counts."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT instrument AS name, COUNT(*) AS count "
                "FROM label_definitions "
                "WHERE instrument IS NOT NULL AND instrument <> '' "
                "GROUP BY instrument "
                "ORDER BY count DESC, instrument ASC"
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Labelled-table refresh
# ---------------------------------------------------------------------------


@router.post("/api/labelled-tables/refresh")
def refresh_labelled_tables(
    level: list[str] | None = Query(None),
    user: str = Depends(get_current_user),
):
    conn = get_conn()
    try:
        counts = rebuild_labelled_tables(conn, levels=level)
        conn.commit()
        return {"ok": True, "counts": counts}
    finally:
        conn.close()
