#!/usr/bin/env python3
"""Manage SSC PACS users.

End-user authentication lives in the PostgreSQL ``users`` table (bcrypt).
The web app is the single login point for end users — its reverse proxy serves
OHIF and DICOMweb to anyone with a valid JWT cookie, attaching the Orthanc
service-account credential behind the scenes.

``orthanc_users.json`` is no longer used for routine end users. It holds:

 - the Orthanc service account (the credential the web app uses to proxy to
   Orthanc; rotated via ``rotate_service_account.py``)
 - admin users (``is_admin=True``), so admins can also reach Orthanc Explorer 2
   on :8042 directly as themselves

Regular ``add``/``passwd``/``remove`` commands touch ``orthanc_users.json``
only when the affected user has ``is_admin=True``.
"""

import argparse
import sys
from pathlib import Path

import bcrypt
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"
load_dotenv(ENV_FILE)

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from _secret_helpers import (  # noqa: E402
    orthanc_admin_user,
    prompt_password,
    remove_orthanc_user,
    upsert_orthanc_user,
)
from db import get_conn  # noqa: E402

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username             TEXT PRIMARY KEY,
    password_hash        TEXT NOT NULL,
    is_admin             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ DEFAULT now(),
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    password_changed_at  TIMESTAMPTZ,
    allowed_datasets     TEXT[] NOT NULL DEFAULT '{}'
);
"""


def ensure_table():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _refuse_service_account(username: str) -> None:
    """Bail out if a regular command targets the Orthanc service-account user."""
    if username == orthanc_admin_user():
        print(
            f"'{username}' is the Orthanc service account. "
            "Use `rotate_service_account.py rotate` to rotate its password.",
            file=sys.stderr,
        )
        sys.exit(2)


def _is_admin(username: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_admin FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


# -- Dataset grant helpers -----------------------------------------------------

def _distinct_patient_datasets() -> list[str]:
    """Distinct cohort tags currently present in `patient.dataset`."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT unnest(dataset) AS ds FROM patient "
                "WHERE dataset <> '{}' ORDER BY 1"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _parse_datasets_csv(raw: str) -> list[str]:
    """Parse 'PRECISE,CRISP2/LVO' into a sorted, deduped list; warn on unknown tags.

    Unknown tags are allowed (grants may precede ingest of a new cohort) but
    flagged so typos don't silently grant nothing.
    """
    datasets = sorted({d.strip() for d in raw.split(",") if d.strip()})
    known = set(_distinct_patient_datasets())
    unknown = [d for d in datasets if d not in known]
    if unknown:
        print(
            f"Warning: dataset(s) not present in patient.dataset yet: "
            f"{', '.join(unknown)} (known: {', '.join(sorted(known)) or 'none'})",
            file=sys.stderr,
        )
    return datasets


# -- CLI commands --------------------------------------------------------------

def cmd_list(_args: argparse.Namespace) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, is_admin, created_at, allowed_datasets "
                "FROM users ORDER BY username"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No users found.")
        return

    print(f"{'Username':<20} {'Admin':<8} {'Created':<18} {'Datasets'}")
    print("-" * 72)
    for username, is_admin, created_at, allowed_datasets in rows:
        admin_str = "yes" if is_admin else "no"
        date_str = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "?"
        if is_admin:
            ds_str = "(admin: all)"
        else:
            ds_str = ", ".join(sorted(allowed_datasets or [])) or "(none)"
        print(f"{username:<20} {admin_str:<8} {date_str:<18} {ds_str}")


def _user_exists(username: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM users WHERE username = %s",
                (username,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def cmd_add(args: argparse.Namespace) -> None:
    _refuse_service_account(args.username)
    if _user_exists(args.username):
        print(
            f"User '{args.username}' is already added. "
            "Use `passwd` to reset their password or `remove` to delete them.",
            file=sys.stderr,
        )
        sys.exit(1)
    datasets = _parse_datasets_csv(args.datasets) if args.datasets else []

    print(
        "Set a temporary password. The user will be required to change it on "
        "first login."
    )
    password = prompt_password()
    pw_hash = hash_password(password)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users "
                "(username, password_hash, is_admin, "
                " must_change_password, password_changed_at, allowed_datasets) "
                "VALUES (%s, %s, %s, TRUE, NULL, %s::text[]) "
                "ON CONFLICT (username) DO UPDATE "
                "SET password_hash = EXCLUDED.password_hash, "
                "    is_admin = EXCLUDED.is_admin, "
                "    must_change_password = TRUE, "
                "    password_changed_at = NULL, "
                "    allowed_datasets = EXCLUDED.allowed_datasets",
                (args.username, pw_hash, args.admin, datasets),
            )
        conn.commit()
    finally:
        conn.close()

    if args.admin:
        upsert_orthanc_user(args.username, password)
        print(f"Admin user '{args.username}' added (DB + orthanc_users.json).")
        print("Restart Orthanc to apply:  docker restart ssc-orthanc")
    else:
        print(f"User '{args.username}' added (DB only).")
        if datasets:
            print(f"Dataset access: {', '.join(datasets)}")
        else:
            print(
                "Dataset access: none — the user will see NO data until granted.\n"
                f"Grant with:  python {Path(__file__).name} set-datasets "
                f"{args.username} <dataset1,dataset2|--all>"
            )
    print("They will be prompted to set a new password on first login.")


def cmd_passwd(args: argparse.Namespace) -> None:
    _refuse_service_account(args.username)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM users WHERE username = %s",
                (args.username,),
            )
            if not cur.fetchone():
                print(f"User '{args.username}' not found.", file=sys.stderr)
                sys.exit(1)
    finally:
        conn.close()

    print(
        "Set a temporary password. The user will be required to change it on "
        "next login."
    )
    password = prompt_password()
    pw_hash = hash_password(password)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users "
                "SET password_hash = %s, "
                "    must_change_password = TRUE, "
                "    password_changed_at = NULL "
                "WHERE username = %s",
                (pw_hash, args.username),
            )
        conn.commit()
    finally:
        conn.close()

    if _is_admin(args.username):
        upsert_orthanc_user(args.username, password)
        print(f"Password updated for admin '{args.username}' (DB + orthanc_users.json).")
        print("Restart Orthanc to apply:  docker restart ssc-orthanc")
    else:
        print(f"Password updated for '{args.username}' (DB only).")
    print("They will be prompted to set a new password on next login.")


def cmd_remove(args: argparse.Namespace) -> None:
    _refuse_service_account(args.username)

    # Capture admin status before delete so we know whether to touch the JSON.
    was_admin = _is_admin(args.username)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE username = %s",
                (args.username,),
            )
            if cur.rowcount == 0:
                print(f"User '{args.username}' not found.", file=sys.stderr)
                sys.exit(1)
        conn.commit()
    finally:
        conn.close()

    if was_admin:
        remove_orthanc_user(args.username)
        print(f"Admin user '{args.username}' removed (DB + orthanc_users.json).")
        print("Restart Orthanc to apply:  docker restart ssc-orthanc")
    else:
        print(f"User '{args.username}' removed (DB only).")


def cmd_set_datasets(args: argparse.Namespace) -> None:
    """Replace a user's dataset grants (deny-by-default access control)."""
    _refuse_service_account(args.username)

    if args.all:
        datasets = _distinct_patient_datasets()
        if not datasets:
            print("No datasets found in patient.dataset; nothing to grant.",
                  file=sys.stderr)
            sys.exit(1)
        print(
            "Note: --all grants the datasets that exist right now "
            f"({', '.join(datasets)}); cohorts ingested later require re-running."
        )
    elif args.none:
        datasets = []
    else:
        datasets = _parse_datasets_csv(args.datasets)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET allowed_datasets = %s::text[] "
                "WHERE username = %s RETURNING is_admin",
                (datasets, args.username),
            )
            row = cur.fetchone()
            if row is None:
                print(f"User '{args.username}' not found.", file=sys.stderr)
                sys.exit(1)
        conn.commit()
    finally:
        conn.close()

    ds_str = ", ".join(datasets) if datasets else "(none)"
    print(f"Dataset access for '{args.username}': {ds_str}")
    if row[0]:
        print(
            "Note: this user is an admin — admins see all datasets regardless "
            "of grants (the stored grants only take effect if admin is revoked)."
        )
    elif not datasets:
        print("The user now sees NO data in the web app.")


# -- Entrypoint ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Manage SSC PACS users. Web-app users live in the PostgreSQL "
            "`users` table; admin users (and the Orthanc service account) are "
            "also mirrored into orthanc_users.json so admins can reach Orthanc "
            "directly on :8042."
        )
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all users")

    p_add = sub.add_parser("add", help="Add a new user")
    p_add.add_argument("username")
    p_add.add_argument(
        "--admin", action="store_true",
        help="Grant admin privileges (also adds to orthanc_users.json)",
    )
    p_add.add_argument(
        "--datasets", metavar="CSV",
        help=(
            "Comma-separated dataset grants (e.g. 'PRECISE,CRISP2/LVO'). "
            "Omitted = no access until granted (deny-by-default)."
        ),
    )

    p_passwd = sub.add_parser("passwd", help="Change a user's password")
    p_passwd.add_argument("username")

    p_rm = sub.add_parser("remove", help="Remove a user")
    p_rm.add_argument("username")

    p_ds = sub.add_parser(
        "set-datasets",
        help="Replace a user's dataset grants (web-app data visibility)",
    )
    p_ds.add_argument("username")
    p_ds.add_argument(
        "datasets", nargs="?", metavar="CSV",
        help="Comma-separated dataset names (e.g. 'PRECISE,CRISP2/LVO')",
    )
    p_ds.add_argument(
        "--all", action="store_true",
        help="Grant every dataset currently present in patient.dataset",
    )
    p_ds.add_argument(
        "--none", action="store_true",
        help="Revoke all dataset access",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "set-datasets":
        chosen = [bool(args.datasets), args.all, args.none]
        if sum(chosen) != 1:
            parser.error(
                "set-datasets requires exactly one of: a CSV list, --all, or --none"
            )

    cmds = {
        "list": cmd_list,
        "add": cmd_add,
        "passwd": cmd_passwd,
        "remove": cmd_remove,
        "set-datasets": cmd_set_datasets,
    }

    ensure_table()
    cmds[args.command](args)


if __name__ == "__main__":
    main()
