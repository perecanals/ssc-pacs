#!/usr/bin/env python3
"""Register, list and rename datasets (the `dataset` registry, Alembic 0026).

A dataset has an immutable **slug** — the prefix of every patient_key
(``<slug>__<patient_id>``) and, later, of on-disk paths — and a mutable display
**name**, which is what users see, what ``users.allowed_datasets`` grants, what
filters and saved exports name, and what ingestion YAMLs set as ``dataset:``.
Ingestion refuses an unregistered dataset, so register one before its first
batch; it can be granted to users right away.

Usage:
    python scripts/admin/manage_datasets.py list
    python scripts/admin/manage_datasets.py add --slug outerlimits --name OUTERLIMITS
    python scripts/admin/manage_datasets.py rename crisp2-lvo 'CRISP2 LVO'            # dry-run
    python scripts/admin/manage_datasets.py rename crisp2-lvo 'CRISP2 LVO' --execute

A rename changes the name everywhere it is stored, in one transaction:
``dataset.name`` (``patient.dataset`` follows through ON UPDATE CASCADE), user
grants, saved Data Exports reports and jobs (their cohort and the grant snapshot
that access checks compare against), saved session filters, and the
``patient_labelled`` mirror. The slug — and so every patient_key — never
changes.

After a rename, remember:
  - ingestion YAMLs and config.toml ``[web-app] clinical_data_dataset`` must use
    the NEW name;
  - the web app's DICOMweb proxy caches scopes for up to 5 minutes — restart it
    (or wait) if OHIF briefly 403s.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import get_conn  # noqa: E402

from labelled_table_sync import sync_labelled_rows  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

LIST_SQL = """
SELECT d.slug, d.name,
       (SELECT count(*) FROM patient p WHERE p.dataset = d.name) AS enrollments,
       (SELECT count(*) FROM users u WHERE d.name = ANY(u.allowed_datasets)) AS grants
FROM dataset d ORDER BY d.name
"""

# Each statement renames %(old)s -> %(new)s in one stored copy of the name.
RENAME_SQL = {
    "user grants": """
        UPDATE users
        SET allowed_datasets = ARRAY(
                SELECT DISTINCT unnest(array_replace(allowed_datasets, %(old)s, %(new)s))
                ORDER BY 1)
        WHERE %(old)s = ANY(allowed_datasets)
    """,
    "export reports": """
        UPDATE data_exports_reports
        SET configuration = jsonb_set(configuration, '{dataset}', to_jsonb(%(new)s::text))
        WHERE configuration->>'dataset' = %(old)s
    """,
    "export jobs (cohort)": """
        UPDATE data_exports_jobs
        SET configuration = jsonb_set(configuration, '{dataset}', to_jsonb(%(new)s::text))
        WHERE configuration->>'dataset' = %(old)s
    """,
    "export jobs (grant snapshot)": """
        UPDATE data_exports_jobs
        SET configuration = jsonb_set(
                configuration, '{authorized_datasets}',
                (SELECT jsonb_agg(CASE WHEN v = %(old)s THEN %(new)s ELSE v END)
                 FROM jsonb_array_elements_text(configuration->'authorized_datasets') v))
        WHERE jsonb_typeof(configuration->'authorized_datasets') = 'array'
          AND configuration->'authorized_datasets' ? %(old)s
    """,
    "saved session filters": """
        UPDATE user_preferences
        SET prefs = jsonb_set(prefs, '{session,filters,dataset}', to_jsonb(%(new)s::text))
        WHERE prefs #>> '{session,filters,dataset}' = %(old)s
    """,
}


def _list(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(LIST_SQL)
        rows = cur.fetchall()
    if not rows:
        print("No datasets registered.")
        return 0
    print(f"{'slug':<20} {'name':<24} {'enrollments':>11} {'grants':>6}")
    for slug, name, enrollments, grants in rows:
        print(f"{slug:<20} {name:<24} {enrollments:>11} {grants:>6}")
    return 0


def _add(conn, slug: str, name: str) -> int:
    slug, name = slug.strip(), name.strip()
    if not SLUG_RE.fullmatch(slug):
        sys.exit(f"Error: slug {slug!r} must be lower-case letters/digits joined by "
                 "single hyphens (e.g. crisp2-lvo). It is permanent.")
    if not name:
        sys.exit("Error: --name must be non-empty.")
    with conn.cursor() as cur:
        cur.execute("SELECT slug, name FROM dataset WHERE slug = %s OR name = %s", (slug, name))
        clash = cur.fetchone()
        if clash:
            sys.exit(f"Error: already registered: slug={clash[0]!r} name={clash[1]!r}.")
        cur.execute("INSERT INTO dataset (slug, name) VALUES (%s, %s)", (slug, name))
    conn.commit()
    print(f"Registered dataset {name!r} (slug {slug!r}).")
    return 0


def _rename(conn, slug: str, new: str, execute: bool) -> int:
    new = new.strip()
    if not new:
        sys.exit("Error: the new name must be non-empty.")
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM dataset WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if row is None:
            sys.exit(f"Error: no dataset with slug {slug!r}.")
        old = row[0]
        if old == new:
            sys.exit(f"Error: {slug!r} is already named {new!r}.")
        cur.execute("SELECT slug FROM dataset WHERE name = %s", (new,))
        if cur.fetchone():
            sys.exit(f"Error: the name {new!r} is taken.")

        counts = {}
        # patient.dataset follows via ON UPDATE CASCADE.
        cur.execute("UPDATE dataset SET name = %s WHERE slug = %s", (new, slug))
        cur.execute(
            "UPDATE patient SET updated_at = now() WHERE dataset = %s RETURNING patient_key",
            (new,),
        )
        patient_keys = [r[0] for r in cur.fetchall()]
        counts["enrollments"] = len(patient_keys)
        for what, statement in RENAME_SQL.items():
            cur.execute(statement, {"old": old, "new": new})
            counts[what] = cur.rowcount
    # The mirror copies patient.dataset; the cascade does not reach it.
    if patient_keys:
        sync_labelled_rows(conn, "patient", patient_keys)

    mode = "Renamed" if execute else "Would rename"
    print(f"{mode} dataset {slug!r}: {old!r} -> {new!r}")
    for what, n in counts.items():
        print(f"  {what + ':':<30} {n}")
    if execute:
        conn.commit()
        print("Committed. Update ingestion YAMLs / clinical_data_dataset to the new name.")
    else:
        conn.rollback()
        print("Dry-run: rolled back. Re-run with --execute to apply.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List registered datasets with usage counts.")
    add = sub.add_parser("add", help="Register a dataset.")
    add.add_argument("--slug", required=True, help="Permanent key prefix, e.g. outerlimits.")
    add.add_argument("--name", required=True, help="Display name, e.g. OUTERLIMITS.")
    rename = sub.add_parser("rename", help="Rename a dataset's display name.")
    rename.add_argument("slug", help="The dataset's slug (unchanged by a rename).")
    rename.add_argument("new_name", help="The new display name.")
    rename.add_argument("--execute", action="store_true",
                        help="Apply. Without it, preview and roll back.")
    args = parser.parse_args()

    conn = get_conn()
    try:
        if args.command == "list":
            return _list(conn)
        if args.command == "add":
            return _add(conn, args.slug, args.name)
        return _rename(conn, args.slug, args.new_name, args.execute)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
