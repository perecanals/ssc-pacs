#!/usr/bin/env python3
"""Rotate / verify the PostgreSQL application password (``DB_PASSWORD``).

The Web App and admin scripts connect to the ``stanford-stroke`` database as
``DB_USER`` with ``DB_PASSWORD`` from ``.env``. This rotates that password:

  1. ``ALTER ROLE`` the login role on the live database (a role may always
     change its own password) — the new secret is passed as a bound parameter,
     never interpolated into the SQL text.
  2. Rewrite every ``.env`` variable that holds this role's password.

The catch: a Postgres role has exactly ONE password, but this deployment can
reference it under more than one ``.env`` variable. In particular Orthanc's
PostgreSQL index connection uses ``PG_ORTHANC_USER`` / ``PG_ORTHANC_PASSWORD``
(see ``docker-compose.yml``). When ``PG_ORTHANC_USER`` is the same role as
``DB_USER`` — the default here — a single ``ALTER ROLE`` changes the password
for both, so ``PG_ORTHANC_PASSWORD`` must be rewritten too and Orthanc must be
restarted, or its index connection breaks. This script handles that
automatically.

Restart the affected consumers afterwards so they reload ``.env`` (running
connections keep the old password until then).

Usage:
    python scripts/admin/rotate_db_password.py rotate [--generate]
    python scripts/admin/rotate_db_password.py check

The password never appears on the command line: ``rotate`` prompts for it
(hidden), or with ``--generate`` mints a strong random one and prints it once.
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
from _secret_helpers import (  # noqa: E402
    generate_password,
    prompt_password,
    rewrite_env_var,
)
from db import DB_CONFIG  # noqa: E402


def _orthanc_shares_role() -> bool:
    """True if Orthanc's index DB uses the same role as ``DB_USER``.

    When it does, rotating the role's password also invalidates
    ``PG_ORTHANC_PASSWORD`` (same role, separate .env var), so both must be
    rewritten and Orthanc restarted.
    """
    return (
        os.getenv("PG_ORTHANC_USER") == DB_CONFIG["user"]
        and os.getenv("PG_ORTHANC_PASSWORD") is not None
    )


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

    # 2. Sync every .env variable that mirrors this role's password. DB_PASSWORD
    #    always; PG_ORTHANC_PASSWORD too when Orthanc's index DB shares the role.
    env_vars = ["DB_PASSWORD"]
    if _orthanc_shares_role():
        env_vars.append("PG_ORTHANC_PASSWORD")
    try:
        for var in env_vars:
            rewrite_env_var(ENV_FILE, var, password)
    except Exception:
        print(
            f"ERROR: the database password for '{role}' WAS changed, but "
            f"updating {ENV_FILE} failed. Set {' and '.join(env_vars)} in that "
            "file to the value you just entered, or services will fail to "
            "connect.",
            file=sys.stderr,
        )
        raise

    print(
        f"Role '{role}' password rotated (database + .env: "
        f"{', '.join(env_vars)})."
    )
    if args.generate:
        print(f"Generated password: {password}")
        print("Store it now — it will not be shown again.")
    print("Restart the affected consumers to pick up the new password:")
    print("  sudo systemctl restart ssc-web-app")
    if "PG_ORTHANC_PASSWORD" in env_vars:
        print("  docker restart ssc-orthanc   # its PostgreSQL index connection uses this role")


def _try_connect(cfg: dict[str, str], label: str) -> str | None:
    """Return None on success, or an error line on failure."""
    try:
        psycopg2.connect(**cfg).close()
    except psycopg2.OperationalError as exc:
        return f"{label} ({cfg['dbname']}): {str(exc).strip()}"
    return None


def cmd_check(_args: argparse.Namespace) -> None:
    """Verify the .env DB passwords actually authenticate against Postgres.

    Checks ``DB_PASSWORD`` (stanford-stroke) and, when Orthanc's index DB shares
    the role, ``PG_ORTHANC_PASSWORD`` (orthanc_db) — the latter would otherwise
    silently drift after a rotation. Exits non-zero on any failure (usable from
    a healthcheck), parallel to ``rotate_service_account.py check``.
    """
    role = DB_CONFIG["user"]
    failures = [f for f in [_try_connect(DB_CONFIG, "DB_PASSWORD")] if f]

    if _orthanc_shares_role():
        orthanc_cfg = dict(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            dbname=os.getenv("PG_ORTHANC_DB", "orthanc_db"),
            user=os.environ["PG_ORTHANC_USER"],
            password=os.environ["PG_ORTHANC_PASSWORD"],
        )
        f = _try_connect(orthanc_cfg, "PG_ORTHANC_PASSWORD")
        if f:
            failures.append(f)

    if failures:
        print(f"DB password for '{role}': DOES NOT AUTHENTICATE", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "Fix with:  python scripts/admin/rotate_db_password.py rotate",
            file=sys.stderr,
        )
        sys.exit(1)

    scope = "stanford-stroke + orthanc_db" if _orthanc_shares_role() else "stanford-stroke"
    print(f"DB password for '{role}': authenticates ({scope}) against "
          f"{DB_CONFIG['host']}:{DB_CONFIG['port']}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_rotate = sub.add_parser(
        "rotate",
        help="Rotate the DB role password (database + all mirroring .env vars)",
    )
    p_rotate.add_argument(
        "--generate", action="store_true",
        help="Mint a strong random password and print it once instead of prompting",
    )

    sub.add_parser(
        "check",
        help="Verify the .env DB passwords authenticate against Postgres",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"rotate": cmd_rotate, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    main()
