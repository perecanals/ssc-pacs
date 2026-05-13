#!/usr/bin/env python3
"""Manage SSC PACS users.

End-user authentication lives in the PostgreSQL ``users`` table (bcrypt).
Companion is the single login point for end users — its reverse proxy serves
OHIF and DICOMweb to anyone with a valid JWT cookie, attaching the Orthanc
service-account credential behind the scenes.

``orthanc_users.json`` is no longer used for routine end users. It holds:

 - the Orthanc service account (the credential Companion uses to proxy to
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

sys.path.insert(0, str(REPO_ROOT / "companion"))
from db import get_conn  # noqa: E402

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT now()
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
                "SELECT username, is_admin, created_at "
                "FROM users ORDER BY username"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No users found.")
        return

    print(f"{'Username':<20} {'Admin':<8} {'Created'}")
    print("-" * 50)
    for username, is_admin, created_at in rows:
        admin_str = "yes" if is_admin else "no"
        date_str = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "?"
        print(f"{username:<20} {admin_str:<8} {date_str}")


def cmd_add(args: argparse.Namespace) -> None:
    _refuse_service_account(args.username)
    password = prompt_password()
    pw_hash = hash_password(password)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (username) DO UPDATE "
                "SET password_hash = EXCLUDED.password_hash, "
                "    is_admin = EXCLUDED.is_admin",
                (args.username, pw_hash, args.admin),
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

    password = prompt_password()
    pw_hash = hash_password(password)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s",
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
    print("  sudo systemctl restart ssc-companion")


# -- Entrypoint ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Manage SSC PACS users. Companion users live in the PostgreSQL "
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

    p_passwd = sub.add_parser("passwd", help="Change a user's password")
    p_passwd.add_argument("username")

    p_rm = sub.add_parser("remove", help="Remove a user")
    p_rm.add_argument("username")

    sub.add_parser(
        "rotate-service-account",
        help="Rotate the Orthanc service-account password (.env + orthanc_users.json)",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    ensure_table()

    cmds = {
        "list": cmd_list,
        "add": cmd_add,
        "passwd": cmd_passwd,
        "remove": cmd_remove,
        "rotate-service-account": cmd_rotate_service_account,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
