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

    # A dataset's clinical table (Alembic 0027): upload it and register it in one step
    python scripts/admin/manage_datasets.py import-clinical --dataset PRECISE \
        --file precise_clinical.xlsx --id-column record_id --date-column stroke_date
    python scripts/admin/manage_datasets.py import-clinical ... --execute
    # adopt an existing table instead of a file (leaves a view under the old name)
    python scripts/admin/manage_datasets.py import-clinical --dataset CRISP2/LVO \
        --from-table clinical_data --id-column study_id --date-column stroke_date \
        --timepoint-strategy crisp2_puncture --execute
    python scripts/admin/manage_datasets.py clear-clinical --dataset PRECISE --execute

Clinical tables: each dataset may have one, ``<slug with - as _>_clinical_data``.
Its id column holds the dataset's patient ids; its date column is the episode
date shown on the patient tab (preferred over the imaging date, for that
dataset's patients only). ``--timepoint-strategy crisp2_puncture`` marks the
table that carries the CRISP2 puncture columns timepoints anchor on. The import
validates first (ids present and unique, dates parseable, coverage of the
dataset's enrolled patients, file ids that match no enrolled patient) and then,
with ``--execute``, writes the table, a unique index on the id column, read
grants, and the registration in one transaction. An unknown dataset is offered
for creation (y/N), as at ingestion.

A rename changes the name everywhere it is stored, in one transaction:
``dataset.name`` (``patient.dataset`` follows through ON UPDATE CASCADE), user
grants, saved Data Exports reports and jobs (their cohort and the grant snapshot
that access checks compare against), saved session filters, and the
``patient_labelled`` mirror. The slug — and so every patient_key — never
changes.

After a rename, remember:
  - ingestion YAMLs must use the NEW name;
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
       (SELECT count(*) FROM users u WHERE d.name = ANY(u.allowed_datasets)) AS grants,
       COALESCE(d.clinical_table || ' (' || d.clinical_id_column || ', '
                || d.clinical_date_column || ')', '-') AS clinical,
       COALESCE(d.timepoint_strategy, '-') AS timepoints
FROM dataset d ORDER BY d.name
"""

# Compatibility view left under a clinical table's former name when it is
# adopted (researchers' saved SQL keeps working); deprecated, see
# docs/operations/linking_patients.md.
LEGACY_CLINICAL_VIEW = "clinical_data"
CLINICAL_SUFFIX = "_clinical_data"
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
STRATEGIES = ("crisp2_puncture",)
# Columns the 'crisp2_puncture' timepoint strategy reads (clinical_sources.py).
CRISP2_ANCHOR_COLUMNS = ("femoral_sheath_time", "receiving_arrival_time", "time_recognized")

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
    print(f"{'slug':<16} {'name':<16} {'enrollments':>11} {'grants':>6}  clinical table / timepoints")
    for slug, name, enrollments, grants, clinical, timepoints in rows:
        print(f"{slug:<16} {name:<16} {enrollments:>11} {grants:>6}  {clinical} / {timepoints}")
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
        print("Committed. Update ingestion YAMLs to the new name.")
    else:
        conn.rollback()
        print("Dry-run: rolled back. Re-run with --execute to apply.")
    return 0


def _snake(name: str) -> str:
    """A file header as a plain SQL identifier (``Study ID`` -> ``study_id``)."""
    ident = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return ident if not ident[:1].isdigit() else f"c_{ident}"


def _read_clinical_file(path: Path, sheet):
    import pandas as pd  # noqa: PLC0415

    if not path.is_file():
        sys.exit(f"Error: no such file: {path}")
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name=sheet)
    else:
        df = pd.read_csv(path)
    renamed = {c: _snake(c) for c in df.columns}
    clashes = {v for v in renamed.values() if list(renamed.values()).count(v) > 1}
    if clashes or not all(IDENT_RE.fullmatch(v) for v in renamed.values()):
        sys.exit(f"Error: column headers do not map to distinct identifiers: {sorted(clashes)}")
    return df.rename(columns=renamed)


def _id_text(value):
    import pandas as pd  # noqa: PLC0415

    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _pg_type(dtype) -> str:
    kind = getattr(dtype, "kind", "O")
    return {"b": "boolean", "i": "bigint", "u": "bigint", "f": "double precision",
            "M": "timestamp"}.get(kind, "text")


def _relation_exists(cur, name: str) -> str | None:
    """'table', 'view' or None for a public relation."""
    cur.execute(
        "SELECT CASE relkind WHEN 'v' THEN 'view' WHEN 'r' THEN 'table' END "
        "FROM pg_class WHERE oid = to_regclass(%s)",
        (f"public.{name}",),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _columns(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _select_grantees(cur, relation: str) -> list[str]:
    cur.execute(
        "SELECT DISTINCT a.grantee::regrole::text FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = to_regclass(%s) AND a.privilege_type = 'SELECT' "
        "AND a.grantee <> 0 AND a.grantee <> c.relowner",
        (f"public.{relation}",),
    )
    return [r[0] for r in cur.fetchall()]


def _validation_report(cur, dataset: str, ids, dates) -> list[str]:
    """Problems that block the import; prints the informational part."""
    import pandas as pd  # noqa: PLC0415

    blocking = []
    ids = pd.Series(ids, dtype="object").map(lambda v: None if pd.isna(v) else str(v).strip())
    if ids.isna().any() or (ids == "").any():
        blocking.append(f"{int(ids.isna().sum() + (ids == '').sum())} row(s) have no id")
    dupes = sorted(ids[ids.duplicated(keep=False) & ids.notna()].unique())
    if dupes:
        blocking.append(f"{len(dupes)} id(s) appear more than once: {', '.join(dupes[:10])}")
    raw_dates = pd.Series(dates, dtype="object")
    parsed = pd.to_datetime(raw_dates, errors="coerce", format="mixed")
    bad = raw_dates[parsed.isna() & raw_dates.notna() & (raw_dates.astype(str).str.strip() != "")]
    print(f"  rows:                    {len(ids)}")
    print(f"  episode dates:           {int(parsed.notna().sum())} parsed, "
          f"{int(raw_dates.isna().sum())} empty, {len(bad)} unparseable")
    for value in list(bad.astype(str).unique())[:10]:
        print(f"    unparseable: {value!r}")
    cur.execute("SELECT patient_id FROM patient WHERE dataset = %s", (dataset,))
    enrolled = {r[0] for r in cur.fetchall()}
    file_ids = set(ids.dropna())
    covered = enrolled & file_ids
    unmatched = sorted(file_ids - enrolled)
    print(f"  coverage:                {len(covered)} of {len(enrolled)} enrolled "
          f"patient(s) have a row")
    print(f"  ids not enrolled:        {len(unmatched)}"
          + (f" (e.g. {', '.join(unmatched[:10])})" if unmatched else ""))
    return blocking


def _import_clinical(conn, args) -> int:
    from dataset_registry import resolve_or_offer_dataset  # noqa: PLC0415

    execute = args.execute
    slug, created = resolve_or_offer_dataset(conn, args.dataset, dry_run=not execute)
    table = slug.replace("-", "_") + CLINICAL_SUFFIX
    id_col, date_col = _snake(args.id_column), _snake(args.date_column)
    strategy = args.timepoint_strategy
    with conn.cursor() as cur:
        cur.execute("SELECT clinical_table FROM dataset WHERE name = %s", (args.dataset,))
        row = cur.fetchone()
        current = row[0] if row else None

        if args.file:
            df = _read_clinical_file(args.file, args.sheet)
            columns = list(df.columns)
            source = str(args.file)
            if id_col in df.columns:
                # Stored as text: patient ids are text, and a numeric column
                # would read 2076 as 2076.0 and match nobody.
                df[id_col] = df[id_col].map(_id_text)
        else:
            if not IDENT_RE.fullmatch(args.from_table) or _relation_exists(cur, args.from_table) != "table":
                sys.exit(f"Error: {args.from_table!r} is not an existing table.")
            columns = _columns(cur, args.from_table)
            source = f"table {args.from_table}"
        missing = [c for c in (id_col, date_col) if c not in columns]
        if missing:
            sys.exit(f"Error: {source} has no column(s) {missing}; it has: {', '.join(columns)}")
        if strategy == "crisp2_puncture":
            lacking = [c for c in CRISP2_ANCHOR_COLUMNS if c not in columns]
            if lacking:
                sys.exit(f"Error: timepoint strategy crisp2_puncture needs column(s) {lacking}.")

        target = _relation_exists(cur, table)
        adopting_in_place = not args.file and args.from_table == table
        if target and not adopting_in_place and not args.replace:
            sys.exit(f"Error: {table} already exists; pass --replace to overwrite it.")
        if args.file:
            ids, dates = df[id_col].tolist(), df[date_col].tolist()
        else:
            cur.execute(
                f'SELECT "{id_col}"::text, "{date_col}"::text FROM "{args.from_table}"'
            )
            pairs = cur.fetchall()
            ids, dates = [p[0] for p in pairs], [p[1] for p in pairs]

        print(f"{'Importing' if execute else 'Would import'} the clinical table of "
              f"{args.dataset!r}{' (new dataset)' if created else ''}")
        print(f"  source:                  {source}")
        print(f"  table:                   {table}"
              + (f" (replacing {current})" if current and current != table else ""))
        print(f"  id / date columns:       {id_col} / {date_col}")
        print(f"  timepoint strategy:      {strategy or '-'}")
        blocking = _validation_report(cur, args.dataset, ids, dates)
        if blocking:
            conn.rollback()
            for problem in blocking:
                print(f"  BLOCKING: {problem}")
            print("Nothing written.")
            return 1
        if not execute:
            conn.rollback()
            print("Dry-run: nothing written. Re-run with --execute to apply.")
            return 0

        # The compatibility view freezes the table's column list: rebuild it
        # around a replace, keeping its grants.
        view_grants = None
        if _relation_exists(cur, LEGACY_CLINICAL_VIEW) == "view":
            cur.execute(
                "SELECT 1 FROM information_schema.view_table_usage "
                "WHERE view_name = %s AND table_name = %s",
                (LEGACY_CLINICAL_VIEW, table),
            )
            if cur.fetchone():
                view_grants = _select_grantees(cur, LEGACY_CLINICAL_VIEW)
                cur.execute(f'DROP VIEW "{LEGACY_CLINICAL_VIEW}"')

        if args.file:
            grantees = _select_grantees(cur, "patient")
            if target:
                cur.execute(f'DROP TABLE "{table}"')
            cur.execute(
                f'CREATE TABLE "{table}" ('
                + ", ".join(
                    f'"{c}" {"text" if c == id_col else _pg_type(df[c].dtype)}' for c in columns
                ) + ")"
            )
            from psycopg2.extras import execute_values  # noqa: PLC0415

            records = [
                tuple(None if (v != v or v is None) else v for v in row)  # NaN -> NULL
                for row in df.astype(object).itertuples(index=False, name=None)
            ]
            column_list = ", ".join(f'"{c}"' for c in columns)
            execute_values(
                cur, f'INSERT INTO "{table}" ({column_list}) VALUES %s', records, page_size=1000
            )
            for grantee in grantees:
                cur.execute(f'GRANT SELECT ON "{table}" TO {grantee}')
        elif not adopting_in_place:
            if target:
                cur.execute(f'DROP TABLE "{table}"')
            cur.execute(f'ALTER TABLE "{args.from_table}" RENAME TO "{table}"')
            if args.from_table == LEGACY_CLINICAL_VIEW:
                view_grants = _select_grantees(cur, table)
        cur.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_{id_col}_uidx" ON "{table}" ("{id_col}")'
        )
        if view_grants is not None:
            cur.execute(f'CREATE VIEW "{LEGACY_CLINICAL_VIEW}" AS SELECT * FROM "{table}"')
            for grantee in view_grants:
                cur.execute(f'GRANT SELECT ON "{LEGACY_CLINICAL_VIEW}" TO {grantee}')
        cur.execute(
            "UPDATE dataset SET clinical_table = %s, clinical_id_column = %s, "
            "clinical_date_column = %s, timepoint_strategy = %s WHERE name = %s",
            (table, id_col, date_col, strategy, args.dataset),
        )
    conn.commit()
    print(f"Committed: {table} registered for {args.dataset!r}"
          + (f"; view {LEGACY_CLINICAL_VIEW} kept for old queries" if view_grants is not None else "")
          + ". The web app reads the registry per request (no restart needed).")
    if strategy or current:
        print("Timepoints follow the clinical anchors: run "
              "scripts/admin/recompute_timepoints.py (dry-run, then --execute).")
    return 0


def _clear_clinical(conn, dataset: str, execute: bool) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dataset SET clinical_table = NULL, clinical_id_column = NULL, "
            "clinical_date_column = NULL, timepoint_strategy = NULL "
            "WHERE name = %s RETURNING clinical_table", (dataset,),
        )
        if cur.fetchone() is None:
            sys.exit(f"Error: no dataset named {dataset!r}.")
    if execute:
        conn.commit()
        print(f"Unregistered the clinical table of {dataset!r} (the table itself is kept).")
    else:
        conn.rollback()
        print(f"Would unregister the clinical table of {dataset!r}. Re-run with --execute.")
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
    imp = sub.add_parser("import-clinical",
                         help="Upload (or adopt) a dataset's clinical table and register it.")
    imp.add_argument("--dataset", required=True, help="Dataset name (offered for creation if new).")
    src = imp.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="CSV or Excel file to upload.")
    src.add_argument("--from-table", help="Adopt an existing table (renamed; see LEGACY view).")
    imp.add_argument("--sheet", default=0, help="Excel sheet name or index (default: first).")
    imp.add_argument("--id-column", required=True, help="Column with the dataset's patient ids.")
    imp.add_argument("--date-column", required=True, help="Column with the episode date.")
    imp.add_argument("--timepoint-strategy", choices=STRATEGIES, default=None,
                     help="Timepoint anchoring for this dataset (default: thrombectomy study only).")
    imp.add_argument("--replace", action="store_true", help="Overwrite an existing clinical table.")
    imp.add_argument("--execute", action="store_true", help="Apply. Default: validate and report.")
    clr = sub.add_parser("clear-clinical", help="Unregister a dataset's clinical table.")
    clr.add_argument("--dataset", required=True)
    clr.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    conn = get_conn()
    try:
        if args.command == "list":
            return _list(conn)
        if args.command == "add":
            return _add(conn, args.slug, args.name)
        if args.command == "import-clinical":
            return _import_clinical(conn, args)
        if args.command == "clear-clinical":
            return _clear_clinical(conn, args.dataset, args.execute)
        return _rename(conn, args.slug, args.new_name, args.execute)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
