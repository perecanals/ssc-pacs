#!/usr/bin/env python3
"""Rotate / verify the PostgreSQL application password (``DB_PASSWORD``).

The Web App and admin scripts connect to the ``stanford-stroke`` database as
``DB_USER`` with ``DB_PASSWORD`` from ``.env``. This rotates that password:

  1. ``ALTER ROLE`` the login role on the live database (a role may always
     change its own password) — the new secret is passed as a bound parameter,
     never interpolated into the SQL text.
  2. Rewrite ``DB_PASSWORD`` in ``.env`` to match.

Restart the Web App afterwards so it reloads ``.env`` (the running pool keeps its
old connections until then).

Usage:
    python scripts/admin/rotate_db_password.py rotate [--generate]
    python scripts/admin/rotate_db_password.py check

The password never appears on the command line: ``rotate`` prompts for it
(hidden), or with ``--generate`` mints a strong random one and prints it once.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"
load_dotenv(ENV_FILE)

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from _secret_helpers import (  # noqa: E402
    generate_password,
    prompt_password,
    rewrite_env_var,
)
from db import DB_CONFIG  # noqa: E402


def cmd_rotate(args: argparse.Namespace) -> None:
    """Change the role password on the DB, then sync .env."""
    role = DB_CONFIG["user"]
    print(f"Rotating PostgreSQL password for role '{role}'.")

    if args.generate:
        password = generate_password()
    else:
        password = prompt_password()

    # 1. Change the password on the live database, authenticating with the
    #    current credentials from .env. The new value is a bound parameter so
    #    it is never rendered into the statement text or any log.
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(
                    sql.Identifier(role)
                ),
                (password,),
            )
    finally:
        conn.close()

    # 2. Sync .env so the next Web App start authenticates with the new value.
    try:
        rewrite_env_var(ENV_FILE, "DB_PASSWORD", password)
    except Exception:
        print(
            f"ERROR: the database password for '{role}' WAS changed, but "
            f"updating DB_PASSWORD in {ENV_FILE} failed. Set DB_PASSWORD in "
            "that file to the value you just entered, or services will fail to "
            "connect.",
            file=sys.stderr,
        )
        raise

    print(f"Role '{role}' password rotated (database + .env).")
    if args.generate:
        print(f"Generated password: {password}")
        print("Store it now — it will not be shown again.")
    print("Restart the Web App to pick up the new password:")
    print("  sudo systemctl restart ssc-web-app")


def cmd_check(_args: argparse.Namespace) -> None:
    """Verify DB_PASSWORD in .env actually authenticates against Postgres.

    Attempts a fresh connection with the current .env credentials. Exits
    non-zero on failure (usable from a healthcheck), parallel to
    ``rotate_service_account.py check``.
    """
    role = DB_CONFIG["user"]
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.close()
    except psycopg2.OperationalError as exc:
        print(
            f"DB password for '{role}': DOES NOT AUTHENTICATE", file=sys.stderr
        )
        print(f"  - {str(exc).strip()}", file=sys.stderr)
        print(
            "Fix with:  python scripts/admin/rotate_db_password.py rotate",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"DB password for '{role}': authenticates against "
          f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_rotate = sub.add_parser(
        "rotate",
        help="Rotate the DB role password (database + .env)",
    )
    p_rotate.add_argument(
        "--generate", action="store_true",
        help="Mint a strong random password and print it once instead of prompting",
    )

    sub.add_parser(
        "check",
        help="Verify DB_PASSWORD in .env authenticates against Postgres",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"rotate": cmd_rotate, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    main()
