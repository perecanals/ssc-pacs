#!/usr/bin/env python3
"""Manage SSC PACS users (Orthanc + Companion).

Users are stored in the ``users`` PostgreSQL table with bcrypt-hashed
passwords.  Orthanc requires plaintext passwords in its config, so this script
also maintains ``orthanc_users.json`` which is mounted into the container.
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

import bcrypt
import psycopg2
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
ORTHANC_USERS_FILE = REPO_ROOT / "orthanc_users.json"
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "companion"))
from db import DB_CONFIG, get_conn  # noqa: E402

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT now()
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


# -- .env admin-password sync -------------------------------------------------

def sync_env_admin(username: str, password: str) -> None:
    """If *username* matches ORTHANC_ADMIN_USER, update ORTHANC_ADMIN_PASSWORD
    in .env so the Companion's service-to-service calls stay in sync."""
    admin_user = os.getenv("ORTHANC_ADMIN_USER", "admin")
    if username != admin_user:
        return
    text = ENV_FILE.read_text()
    text = re.sub(
        r"^ORTHANC_ADMIN_PASSWORD=.*$",
        f"ORTHANC_ADMIN_PASSWORD='{password}'",
        text,
        flags=re.MULTILINE,
    )
    ENV_FILE.write_text(text)
    print(f"  Updated ORTHANC_ADMIN_PASSWORD in .env")


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

    users = load_orthanc_users()
    users[args.username] = password
    save_orthanc_users(users)

    sync_env_admin(args.username, password)

    print(f"User '{args.username}' added.")
    print("Restart Orthanc to apply:  docker restart ssc-orthanc")


def cmd_passwd(args: argparse.Namespace) -> None:
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

    users = load_orthanc_users()
    users[args.username] = password
    save_orthanc_users(users)

    sync_env_admin(args.username, password)

    print(f"Password updated for '{args.username}'.")
    print("Restart Orthanc to apply:  docker restart ssc-orthanc")


def cmd_remove(args: argparse.Namespace) -> None:
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

    users = load_orthanc_users()
    users.pop(args.username, None)
    save_orthanc_users(users)

    print(f"User '{args.username}' removed.")
    print("Restart Orthanc to apply:  docker restart ssc-orthanc")


# -- Entrypoint ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage SSC PACS users (Orthanc + Companion)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all users")

    p_add = sub.add_parser("add", help="Add a new user")
    p_add.add_argument("username")
    p_add.add_argument(
        "--admin", action="store_true", help="Grant admin privileges"
    )

    p_passwd = sub.add_parser("passwd", help="Change a user's password")
    p_passwd.add_argument("username")

    p_rm = sub.add_parser("remove", help="Remove a user")
    p_rm.add_argument("username")

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
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
