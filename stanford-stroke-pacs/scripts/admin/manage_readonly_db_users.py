#!/usr/bin/env python3
"""Manage read-only PostgreSQL roles for direct database access.

For collaborators who query the databases directly (psql, pandas, DBeaver)
without going through the web app. These are PostgreSQL *roles* (cluster
level) — completely separate from web-app UI logins, which are *rows* in the
``users`` table managed by ``manage_users.py``. Each role this script
creates:

 - can ``SELECT`` every current *and future* table in schema ``public`` of
   ``stanford-stroke`` (future tables via ``ALTER DEFAULT PRIVILEGES FOR
   ROLE <DB_USER>`` — the Alembic migrations run as ``DB_USER``, so that
   role owns new tables) and of ``orthanc_db`` (owner ``PG_ORTHANC_USER``);
 - starts every session with ``default_transaction_read_only = on``, so even
   an accidental write statement is rejected;
 - is denied ``SELECT`` on the web-app auth tables (``users`` holds the UI
   logins' bcrypt password hashes, ``user_preferences`` per-user settings) —
   revoked precisely because they are unrelated to this role's purpose.

No ``sudo`` needed: the script authenticates to Postgres over TCP as
``DB_USER`` with ``DB_PASSWORD`` from ``.env`` (that role must be able to
``CREATE ROLE``; on this deployment it is the cluster superuser). The only
password ever prompted is the NEW one being set for the managed role —
prompted hidden (or ``--generate`` mints one and prints it once) and passed
to the server as a bound parameter, never on a command line.

Usage:
    python scripts/admin/manage_readonly_db_users.py add <username> [--generate]
    python scripts/admin/manage_readonly_db_users.py passwd <username> [--generate]
    python scripts/admin/manage_readonly_db_users.py list
    python scripts/admin/manage_readonly_db_users.py remove <username>

The server listens on localhost only; remote collaborators reach it through
an SSH tunnel (see ``scripts/connectivity/``) and then connect to
``localhost:5432`` as themselves.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"
load_dotenv(ENV_FILE)

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from _secret_helpers import generate_password, prompt_password  # noqa: E402
from db import DB_CONFIG  # noqa: E402

# Web-app-owned tables a read-only research role must not see (password
# hashes / per-user settings). Revoked at add time when they exist.
AUTH_TABLES = ("users", "user_preferences")

# Orthanc's index database: read access is granted there too. Its tables are
# created at runtime by the role Orthanc connects as (PG_ORTHANC_USER), which
# on this deployment is the same role as DB_USER.
ORTHANC_DB = os.getenv("PG_ORTHANC_DB", "orthanc_db")
ORTHANC_OWNER = os.getenv("PG_ORTHANC_USER") or DB_CONFIG["user"]


def _connect(dbname: str | None = None):
    cfg = dict(DB_CONFIG)
    if dbname:
        cfg["dbname"] = dbname
    conn = psycopg2.connect(**cfg)
    conn.autocommit = True
    return conn


def _db_exists(cur, dbname: str) -> bool:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return cur.fetchone() is not None


def _refuse_app_role(username: str) -> None:
    if username == DB_CONFIG["user"]:
        print(
            f"'{username}' is the application role (DB_USER). "
            "Use `rotate_db_password.py` for it, not this script.",
            file=sys.stderr,
        )
        sys.exit(2)


def _role_exists(cur, username: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
    return cur.fetchone() is not None


def _get_password(args: argparse.Namespace, username: str) -> str:
    """The NEW password for the managed role — never a sudo/system password."""
    if args.generate:
        return generate_password()
    print(
        f"Setting the database password for role '{username}' "
        "(this is a new secret for that role — not your sudo or login password)."
    )
    return prompt_password(f"New password for DB role '{username}'")


def _print_generated(args: argparse.Namespace, password: str) -> None:
    if args.generate:
        print(f"Generated password: {password}")
        print("Store it now — it will not be shown again.")


def cmd_add(args: argparse.Namespace) -> None:
    username = args.username
    _refuse_app_role(username)
    owner = DB_CONFIG["user"]
    dbname = DB_CONFIG["dbname"]

    conn = _connect()
    try:
        with conn.cursor() as cur:
            if _role_exists(cur, username):
                print(f"Role '{username}' already exists.", file=sys.stderr)
                sys.exit(1)

            password = _get_password(args, username)
            ident = sql.Identifier(username)

            # psycopg2 interpolates %s client-side (safely quoted), so the
            # password works inside this utility statement too.
            cur.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(ident),
                (password,),
            )
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(dbname), ident
                )
            )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
            cur.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident)
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO {}"
                ).format(sql.Identifier(owner), ident)
            )
            cur.execute(
                sql.SQL(
                    "ALTER ROLE {} SET default_transaction_read_only = on"
                ).format(ident)
            )

            for table in AUTH_TABLES:
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        sql.SQL("REVOKE SELECT ON {} FROM {}").format(
                            sql.Identifier(table), ident
                        )
                    )

            orthanc_exists = _db_exists(cur, ORTHANC_DB)
    finally:
        conn.close()

    scope = dbname
    if orthanc_exists:
        _grant_orthanc_read(username)
        scope = f"{dbname} + {ORTHANC_DB}"
    else:
        print(f"Note: database '{ORTHANC_DB}' not found — skipped its grants.")

    print(f"Read-only role '{username}' created on {scope} "
          f"(auth tables excluded: {', '.join(AUTH_TABLES)}).")
    _print_generated(args, password)


def _grant_orthanc_read(username: str) -> None:
    """Grant SELECT on current + future orthanc_db tables (must run connected
    to that database — table-level GRANTs are per-database)."""
    ident = sql.Identifier(username)
    conn = _connect(ORTHANC_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(ORTHANC_DB), ident
                )
            )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
            cur.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident)
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO {}"
                ).format(sql.Identifier(ORTHANC_OWNER), ident)
            )
    finally:
        conn.close()


def cmd_passwd(args: argparse.Namespace) -> None:
    username = args.username
    _refuse_app_role(username)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            if not _role_exists(cur, username):
                print(f"Role '{username}' does not exist.", file=sys.stderr)
                sys.exit(1)
            password = _get_password(args, username)
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(
                    sql.Identifier(username)
                ),
                (password,),
            )
    finally:
        conn.close()

    print(f"Password updated for role '{username}'.")
    _print_generated(args, password)


def cmd_list(_args: argparse.Namespace) -> None:
    """All login roles, flagging the read-only ones this script manages."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                r"""
                SELECT r.rolname,
                       r.rolsuper,
                       COALESCE(
                           'default_transaction_read_only=on' = ANY(s.setconfig),
                           false
                       ) AS read_only
                FROM pg_roles r
                LEFT JOIN pg_db_role_setting s
                       ON s.setrole = r.oid AND s.setdatabase = 0
                WHERE r.rolcanlogin AND r.rolname NOT LIKE 'pg\_%'
                ORDER BY r.rolname
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    app_role = DB_CONFIG["user"]
    for name, superuser, read_only in rows:
        tags = []
        if name == app_role:
            tags.append("app role / DB_USER")
        if superuser:
            tags.append("superuser")
        if read_only:
            tags.append("read-only")
        print(f"  {name:<24} {', '.join(tags) if tags else '-'}")


def cmd_remove(args: argparse.Namespace) -> None:
    username = args.username
    _refuse_app_role(username)

    ident = sql.Identifier(username)

    # DROP OWNED BY only revokes grants/default privileges in the database it
    # runs in, so it must run once per database the role was granted in —
    # otherwise the cluster-wide DROP ROLE fails with a dependency error.
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if not _role_exists(cur, username):
                print(f"Role '{username}' does not exist.", file=sys.stderr)
                sys.exit(1)
            orthanc_exists = _db_exists(cur, ORTHANC_DB)
            cur.execute(sql.SQL("DROP OWNED BY {}").format(ident))
    finally:
        conn.close()

    if orthanc_exists:
        conn = _connect(ORTHANC_DB)
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP OWNED BY {}").format(ident))
        finally:
            conn.close()

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP ROLE {}").format(ident))
    finally:
        conn.close()

    print(f"Role '{username}' removed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    for name, helptext, with_generate in (
        ("add", "Create a read-only role (prompts for its password)", True),
        ("passwd", "Set a new password for an existing role", True),
        ("remove", "Drop a role and revoke its grants", False),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("username")
        if with_generate:
            p.add_argument(
                "--generate", action="store_true",
                help="Mint a strong random password and print it once "
                     "instead of prompting",
            )

    sub.add_parser("list", help="List login roles, flagging read-only ones")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "add": cmd_add,
        "passwd": cmd_passwd,
        "list": cmd_list,
        "remove": cmd_remove,
    }[args.command](args)


if __name__ == "__main__":
    main()
