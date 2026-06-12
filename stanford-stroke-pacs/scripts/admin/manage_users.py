#!/usr/bin/env python3
"""Manage SSC PACS users.

End-user authentication lives in the PostgreSQL ``users`` table (bcrypt).
The web app is the single login point for end users — its reverse proxy serves
OHIF and DICOMweb to anyone with a valid JWT cookie, attaching the Orthanc
service-account credential behind the scenes.

``orthanc_users.json`` is no longer used for routine end users. It holds:

 - the Orthanc service account (the credential the web app uses to proxy to
   Orthanc; rotated via ``rotate-service-account``)
 - admin users (``is_admin=True``), so admins can also reach Orthanc Explorer 2
   on :8042 directly as themselves

Regular ``add``/``passwd``/``remove`` commands touch ``orthanc_users.json``
only when the affected user has ``is_admin=True``.
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

import bcrypt
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ORTHANC_USERS_FILE = REPO_ROOT / "orthanc_users.json"
ENV_FILE = REPO_ROOT / ".env"
load_dotenv(ENV_FILE)

sys.path.insert(0, str(REPO_ROOT / "web-app"))
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


def _orthanc_admin_user() -> str:
    return os.getenv("ORTHANC_ADMIN_USER", "admin")


def ensure_table():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


def prompt_password() -> str:
    """Hidden password prompt with confirmation and minimum-length check."""
    while True:
        p1 = getpass.getpass("Password: ")
        if len(p1) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            continue
        p2 = getpass.getpass("Confirm password: ")
        if p1 == p2:
            return p1
        print("Passwords do not match. Try again.", file=sys.stderr)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _refuse_service_account(username: str) -> None:
    """Bail out if a regular command targets the Orthanc service-account user."""
    if username == _orthanc_admin_user():
        print(
            f"'{username}' is the Orthanc service account. "
            "Use `rotate-service-account` to rotate its password.",
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
    """Parse 'precise,lvo' into a sorted, deduped list; warn on unknown tags.

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


# -- Orthanc users JSON helpers ------------------------------------------------

def load_orthanc_users() -> dict[str, str]:
    if ORTHANC_USERS_FILE.exists():
        data = json.loads(ORTHANC_USERS_FILE.read_text())
        return data.get("RegisteredUsers", {})
    return {}


def save_orthanc_users(users: dict[str, str]) -> None:
    ORTHANC_USERS_FILE.write_text(
        json.dumps({"RegisteredUsers": users}, indent=2) + "\n"
    )
    os.chmod(ORTHANC_USERS_FILE, 0o600)


def upsert_orthanc_user(username: str, password: str) -> None:
    users = load_orthanc_users()
    users[username] = password
    save_orthanc_users(users)


def remove_orthanc_user(username: str) -> None:
    users = load_orthanc_users()
    if users.pop(username, None) is not None:
        save_orthanc_users(users)


# -- .env admin-password sync -------------------------------------------------

def _write_env_admin_password(password: str) -> None:
    """Rewrite ORTHANC_ADMIN_PASSWORD in .env (idempotent; appends if absent)."""
    text = ENV_FILE.read_text()
    new_text, count = re.subn(
        r"^ORTHANC_ADMIN_PASSWORD=.*$",
        f"ORTHANC_ADMIN_PASSWORD='{password}'",
        text,
        flags=re.MULTILINE,
    )
    if count == 0:
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += f"ORTHANC_ADMIN_PASSWORD='{password}'\n"
    ENV_FILE.write_text(new_text)


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


def cmd_rotate_service_account(_args: argparse.Namespace) -> None:
    """Rotate the Orthanc service-account password.

    Rewrites ORTHANC_ADMIN_PASSWORD in .env and the matching entry in
    orthanc_users.json atomically. Does not touch the users DB table.
    """
    username = _orthanc_admin_user()
    print(f"Rotating Orthanc service-account password for user '{username}'.")
    password = prompt_password()

    _write_env_admin_password(password)
    upsert_orthanc_user(username, password)

    print(f"Service-account '{username}' rotated.")
    print("Restart both services to pick up the new password:")
    print("  docker restart ssc-orthanc")
    print("  sudo systemctl restart ssc-web-app")


def cmd_check_service_account(_args: argparse.Namespace) -> None:
    """Verify the service-account password matches across .env and the JSON.

    The web-app proxy authenticates to Orthanc with ORTHANC_ADMIN_PASSWORD from
    .env; Orthanc accepts it because orthanc_users.json holds the same value.
    These two are the one config pair not enforced by code at runtime, so a
    manual edit to either file can silently break OHIF/DICOMweb. This is the
    detector. Exits non-zero on mismatch (usable from a healthcheck).
    """
    username = _orthanc_admin_user()
    env_pw = os.getenv("ORTHANC_ADMIN_PASSWORD")
    json_pw = load_orthanc_users().get(username)

    problems = []
    if not env_pw:
        problems.append(f"ORTHANC_ADMIN_PASSWORD is unset/empty in {ENV_FILE}")
    if json_pw is None:
        problems.append(f"no entry for '{username}' in {ORTHANC_USERS_FILE}")
    if env_pw and json_pw is not None and env_pw != json_pw:
        problems.append(
            "ORTHANC_ADMIN_PASSWORD in .env does not match orthanc_users.json "
            f"for '{username}'"
        )

    if problems:
        print(f"Service-account '{username}': OUT OF SYNC", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Fix with:  python scripts/admin/manage_users.py rotate-service-account",
              file=sys.stderr)
        sys.exit(1)
    print(f"Service-account '{username}': .env and orthanc_users.json are in sync.")


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
            "Comma-separated dataset grants (e.g. 'precise,lvo'). "
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
        help="Comma-separated dataset names (e.g. 'precise,lvo')",
    )
    p_ds.add_argument(
        "--all", action="store_true",
        help="Grant every dataset currently present in patient.dataset",
    )
    p_ds.add_argument(
        "--none", action="store_true",
        help="Revoke all dataset access",
    )

    sub.add_parser(
        "rotate-service-account",
        help="Rotate the Orthanc service-account password (.env + orthanc_users.json)",
    )

    sub.add_parser(
        "check-service-account",
        help="Verify .env and orthanc_users.json agree on the service-account password",
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
        "rotate-service-account": cmd_rotate_service_account,
        "check-service-account": cmd_check_service_account,
    }

    # File-only commands don't need (and shouldn't require) a live DB.
    if args.command not in {"check-service-account"}:
        ensure_table()

    cmds[args.command](args)


if __name__ == "__main__":
    main()
