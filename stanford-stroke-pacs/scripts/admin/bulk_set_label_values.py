#!/usr/bin/env python3
"""Bulk-set annotation label values from a CSV / Excel table (admin backdoor).

Driven entirely by CLI flags. Dry-run by default — nothing is written without
``--execute``. The only interactive prompt is the y/n confirmation when the
target label does not yet exist and must be created (``--yes`` bypasses it).

Writes direct SQL and **deliberately bypasses the API's per-label edit gate**
(``label_definitions.edit_policy``): this *is* the admin backdoor, and its only
authorization is shell + ``.env`` access. Every write still lands in
``annotations_history`` via the row-level trigger, attributed ``bulk:<user>``.

Use ``--edit-policy nobody`` when creating a label to backfill upstream data
that raters must not overwrite — the values then render read-only in the web
app. It applies on label *creation* only (like ``--instrument`` /
``--description``); change an existing label's policy from the Label Access
admin page.

Examples
--------
Dry-run (default; validate and report, no DB writes):

    python scripts/admin/bulk_set_label_values.py \\
        --file /tmp/series_quality.csv \\
        --level series \\
        --id-column seriesinstanceuid \\
        --value-column quality \\
        --label series_quality \\
        --datatype select \\
        --options 'good,acceptable,poor' \\
        --instrument 'manual review'

Apply, auto-confirm label creation:

    python scripts/admin/bulk_set_label_values.py \\
        --file /tmp/study_modality_check.xlsx \\
        --level study \\
        --id-column studyinstanceuid \\
        --value-column has_dwi \\
        --label has_dwi \\
        --datatype bool \\
        --execute --yes
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))

from common import LABEL_NAME_RE, record_label_value  # noqa: E402
from db import DB_CONFIG  # noqa: E402

from labelled_table_sync import (  # noqa: E402
    LEVEL_CONFIGS,
    find_label_column_conflict,
    sync_labelled_rows,
    sync_labelled_schema,
)

VALID_DATATYPES = ("bool", "int", "text", "select")
BOOL_TRUE = {"true", "t", "yes", "y", "1"}
BOOL_FALSE = {"false", "f", "no", "n", "0"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", required=True, type=Path, help="CSV or Excel file.")
    parser.add_argument(
        "--level", required=True, choices=tuple(LEVEL_CONFIGS),
        help="Annotation level: patient, study, or series.",
    )
    parser.add_argument(
        "--id-column", required=True,
        help="Column in the file that holds the entity ID "
             "(seriesinstanceuid / studyinstanceuid / patient_id).",
    )
    parser.add_argument(
        "--value-column", required=True,
        help="Column in the file with the values to assign to the label.",
    )
    parser.add_argument("--label", required=True, help="Label name to set.")

    # Used only when the label does not yet exist and must be created.
    parser.add_argument(
        "--datatype", choices=VALID_DATATYPES,
        help="Datatype for label creation (bool/int/text/select). "
             "Required if the label does not exist.",
    )
    parser.add_argument(
        "--options", default=None,
        help="Comma-separated options for datatype=select.",
    )
    parser.add_argument(
        "--instrument", default=None,
        help="Optional instrument tag (used only on label creation).",
    )
    parser.add_argument(
        "--description", default=None,
        help="Optional description (used only on label creation).",
    )
    parser.add_argument(
        "--edit-policy", default="everyone", choices=("everyone", "nobody", "users"),
        help="Who may edit these values in the web app, used only on label "
             "creation (default: everyone). Use 'nobody' for a backfill of "
             "upstream data that raters must not overwrite.",
    )
    parser.add_argument(
        "--edit-users", default=None,
        help="Comma-separated usernames for --edit-policy=users "
             "(used only on label creation).",
    )

    parser.add_argument(
        "--sheet", default=0,
        help="Sheet name or index for Excel files (default: first sheet).",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply the changes. Default is a dry-run: validate and report only.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Auto-confirm label creation prompt.",
    )
    return parser.parse_args()


def _load_table(path: Path, sheet):
    import pandas as pd

    if not path.exists():
        sys.exit(f"Error: file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    if suffix in (".xlsx", ".xls", ".xlsm"):
        try:
            sheet_arg = int(sheet) if str(sheet).isdigit() else sheet
            return pd.read_excel(path, sheet_name=sheet_arg, dtype=str)
        except ImportError as exc:
            sys.exit(
                f"Error: reading Excel needs an extra dependency ({exc}). "
                "Install with: pip install openpyxl  (or use a CSV)."
            )
    sys.exit(f"Error: unsupported file extension '{suffix}'. Use .csv or .xlsx.")


def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def _coerce_value(raw, datatype: str) -> tuple[str | None, str | None]:
    """Return (value_to_store, error). value=None means "skip this row"."""
    if _is_blank(raw):
        return None, None
    text = str(raw).strip()
    if datatype == "bool":
        lowered = text.lower()
        if lowered in BOOL_TRUE:
            return "true", None
        if lowered in BOOL_FALSE:
            return None, None  # falsy → no annotation row (matches UI semantics)
        return None, f"unrecognized bool value {text!r}"
    if datatype == "int":
        stripped = text.lstrip("-")
        if not stripped.isdigit():
            return None, f"not an integer: {text!r}"
        return text, None
    return text, None


def _audit_user() -> str:
    sudo_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "admin"
    return f"bulk:{sudo_user}"


def _fetch_label_def(conn, label: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, level, datatype, options, instrument "
            "FROM label_definitions WHERE name = %s",
            (label,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "name": row[1], "level": row[2],
            "datatype": row[3], "options": row[4], "instrument": row[5],
        }


def _parse_edit_users(conn, args) -> list[str]:
    """Validate --edit-users against real usernames; return the normalized list.

    Mirrors the API's validate_edit_policy: trim, drop empties, dedupe, sort,
    and reject unknown names. `edit_users` is forced empty unless the policy is
    `users`, so the column can never hold a stale list.
    """
    if args.edit_policy != "users":
        return []
    names = sorted({u.strip() for u in (args.edit_users or "").split(",") if u.strip()})
    if not names:
        sys.exit(
            "Error: --edit-policy=users requires --edit-users 'a,b' "
            "(an empty list is indistinguishable from --edit-policy=nobody)."
        )
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM users WHERE username = ANY(%s)", (names,))
        known = {r[0] for r in cur.fetchall()}
    unknown = [n for n in names if n not in known]
    if unknown:
        sys.exit(f"Error: unknown user(s): {', '.join(unknown)}")
    return names


def _create_label_definition(conn, args, audit_user: str) -> None:
    if not LABEL_NAME_RE.match(args.label):
        sys.exit(
            f"Error: label name {args.label!r} must match {LABEL_NAME_RE.pattern}"
        )
    if not args.datatype:
        sys.exit(
            "Error: label does not exist and --datatype was not provided. "
            "Pass --datatype {bool|int|text|select}."
        )
    if args.datatype == "select":
        if not args.options:
            sys.exit("Error: --options is required when --datatype=select.")
        opts = [o.strip() for o in args.options.split(",") if o.strip()]
        if not opts:
            sys.exit("Error: --options must contain at least one non-empty value.")
        options_json = json.dumps(opts)
    else:
        options_json = None

    conflict = find_label_column_conflict(conn, args.level, args.label)
    if conflict:
        sys.exit(
            f"Error: label name conflicts with existing column generated from {conflict!r}"
        )

    edit_users = _parse_edit_users(conn, args)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO label_definitions "
            "(name, description, level, datatype, options, instrument, "
            " created_by, edit_policy, edit_users) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::text[])",
            (
                args.label,
                (args.description or None),
                args.level,
                args.datatype,
                options_json,
                (args.instrument or None),
                audit_user,
                args.edit_policy,
                edit_users,
            ),
        )
    sync_labelled_schema(conn, args.level)


def _upsert_annotation(
    conn, level: str, entity_id: str, label: str, value: str, datatype: str
) -> None:
    if level == "series":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT studyinstanceuid, patient_id FROM image_series "
                "WHERE seriesinstanceuid = %s",
                (entity_id,),
            )
            row = cur.fetchone()
            study_uid, patient_id = (row[0], row[1]) if row else (None, None)
            cur.execute(
                "INSERT INTO annotations "
                "(level, seriesinstanceuid, studyinstanceuid, patient_id, label, value, created_by) "
                "VALUES ('series', %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (seriesinstanceuid, label) WHERE level = 'series' DO UPDATE "
                "SET value = EXCLUDED.value, created_by = EXCLUDED.created_by, created_at = now()",
                (entity_id, study_uid, patient_id, label, value, _audit_user()),
            )
            if datatype == "select":
                record_label_value(cur, label, value, _audit_user())
    elif level == "study":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT patient_id FROM image_study WHERE studyinstanceuid = %s",
                (entity_id,),
            )
            row = cur.fetchone()
            patient_id = row[0] if row else None
            cur.execute(
                "INSERT INTO annotations "
                "(level, studyinstanceuid, patient_id, label, value, created_by) "
                "VALUES ('study', %s, %s, %s, %s, %s) "
                "ON CONFLICT (studyinstanceuid, label) WHERE level = 'study' DO UPDATE "
                "SET value = EXCLUDED.value, created_by = EXCLUDED.created_by, created_at = now()",
                (entity_id, patient_id, label, value, _audit_user()),
            )
            if datatype == "select":
                record_label_value(cur, label, value, _audit_user())
    else:  # patient
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO annotations "
                "(level, patient_id, label, value, created_by) "
                "VALUES ('patient', %s, %s, %s, %s) "
                "ON CONFLICT (patient_id, label) WHERE level = 'patient' DO UPDATE "
                "SET value = EXCLUDED.value, created_by = EXCLUDED.created_by, created_at = now()",
                (entity_id, label, value, _audit_user()),
            )
            if datatype == "select":
                record_label_value(cur, label, value, _audit_user())


def _existing_entity_ids(conn, level: str, candidate_ids: list[str]) -> set[str]:
    config = LEVEL_CONFIGS[level]
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT {key} FROM {tbl} WHERE {key} = ANY(%s)").format(
                key=sql.Identifier(config.source_key),
                tbl=sql.Identifier(config.source_table),
            ),
            (candidate_ids,),
        )
        return {row[0] for row in cur.fetchall()}


def main() -> None:
    args = _parse_args()
    dry_run = not args.execute
    audit_user = _audit_user()

    df = _load_table(args.file, args.sheet)
    for col in (args.id_column, args.value_column):
        if col not in df.columns:
            sys.exit(
                f"Error: column {col!r} not in file. Available columns: "
                f"{list(df.columns)}"
            )

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        # Tag this connection so the audit trigger attributes writes correctly.
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = %s", (audit_user,))

        # --- Resolve / create the label -----------------------------------
        label_def = _fetch_label_def(conn, args.label)
        if label_def is None:
            print(f"Label {args.label!r} does not exist.")
            if dry_run:
                print(f"  Would create at level={args.level}, "
                      f"datatype={args.datatype}, instrument={args.instrument!r}, "
                      f"edit_policy={args.edit_policy!r}")
            else:
                if not args.yes:
                    answer = input(
                        f"Create label {args.label!r} "
                        f"(level={args.level}, datatype={args.datatype}, "
                        f"instrument={args.instrument!r})? [y/N] "
                    ).strip().lower()
                    if answer != "y":
                        sys.exit("Aborted: label does not exist and creation declined.")
                _create_label_definition(conn, args, audit_user)
                label_def = _fetch_label_def(conn, args.label)
                print(f"Created label {args.label!r} ({label_def['datatype']}, "
                      f"level={label_def['level']}).")
            datatype = args.datatype
            if label_def is not None:
                datatype = label_def["datatype"]
        else:
            if label_def["level"] != args.level:
                sys.exit(
                    f"Error: label {args.label!r} exists at level "
                    f"{label_def['level']!r}, but --level={args.level!r}."
                )
            if args.datatype and args.datatype != label_def["datatype"]:
                sys.exit(
                    f"Error: label exists with datatype "
                    f"{label_def['datatype']!r}; --datatype={args.datatype!r} "
                    f"does not match."
                )
            datatype = label_def["datatype"]
            print(f"Using existing label {args.label!r} "
                  f"(datatype={datatype}, level={label_def['level']}).")

        # --- Validate entities exist -------------------------------------
        raw_ids = [str(v).strip() for v in df[args.id_column].tolist()]
        unique_ids = sorted({i for i in raw_ids if i})
        existing = _existing_entity_ids(conn, args.level, unique_ids)
        missing = [i for i in unique_ids if i not in existing]
        if missing:
            print(f"Warning: {len(missing)} {args.level} ID(s) from the file "
                  f"are not in {LEVEL_CONFIGS[args.level].source_table}; "
                  "rows referencing them will be skipped.")
            for mid in missing[:10]:
                print(f"  missing: {mid}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")

        # --- Walk rows ---------------------------------------------------
        applied = 0
        skipped_blank = 0
        skipped_missing = 0
        skipped_invalid = 0
        touched_ids: list[str] = []
        for _, row in df.iterrows():
            entity_id = str(row[args.id_column]).strip() if not _is_blank(row[args.id_column]) else ""
            if not entity_id:
                skipped_blank += 1
                continue
            if entity_id not in existing:
                skipped_missing += 1
                continue
            value, err = _coerce_value(row[args.value_column], datatype)
            if err:
                skipped_invalid += 1
                print(f"  skip {entity_id}: {err}")
                continue
            if value is None:
                skipped_blank += 1
                continue
            if dry_run:
                applied += 1
                touched_ids.append(entity_id)
                continue
            _upsert_annotation(conn, args.level, entity_id, args.label, value, datatype)
            applied += 1
            touched_ids.append(entity_id)

        # --- Sync labelled mirror table ----------------------------------
        if applied and not dry_run:
            sync_labelled_rows(conn, args.level, touched_ids)

        if dry_run:
            conn.rollback()
            print("\nDry-run summary (no DB changes committed):")
        else:
            conn.commit()
            print("\nDone.")
        print(f"  applied:           {applied}")
        print(f"  skipped (blank):   {skipped_blank}")
        print(f"  skipped (missing): {skipped_missing}")
        print(f"  skipped (invalid): {skipped_invalid}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
