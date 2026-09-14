#!/usr/bin/env python3
"""Provision/check the dedicated Data Exports role; never print credentials.

Run from the stack root: python scripts/admin/manage_explorer_db.py provision
Updates only EXPLORER_DB_USER/PASSWORD in .env. Existing role must carry this
script's ownership marker. Use provision again to rotate its password and sync
SELECT grants after adding an approved research table. Use sync to update
grants without rotating the credential.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

import psycopg2
from dotenv import set_key
from psycopg2 import sql

STACK = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STACK / "web-app"))
from data_explorer.database import check_role
from data_explorer.policy import READER_TABLES
from db import DB_CONFIG

MARKER = "ssc-data-explorer dedicated reader"


def provision(*, rotate=True):
    name = os.getenv("EXPLORER_DB_USER", "sscpacs-readonly")
    if name == DB_CONFIG["user"]:
        raise RuntimeError("Explorer role must differ from DB_USER")
    password = secrets.token_urlsafe(48) if rotate else None
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,))
            existing = cur.fetchone()
            if existing and existing[0] != MARKER:
                raise RuntimeError("Existing role is not owned by this script; choose a new EXPLORER_DB_USER")
            if not existing and not rotate:
                raise RuntimeError("Role does not exist; run provision first")
            ident = sql.Identifier(name)
            if not existing:
                cur.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                    ).format(ident)
                )
                cur.execute(sql.SQL("COMMENT ON ROLE {} IS %s").format(ident), (MARKER,))
            if rotate:
                cur.execute(sql.SQL("ALTER ROLE {} PASSWORD %s").format(ident), (password,))
            cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(ident))
            cur.execute(sql.SQL("ALTER ROLE {} SET search_path=pg_catalog").format(ident))
            cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout='30min'").format(ident))
            cur.execute(sql.SQL("ALTER ROLE {} SET temp_file_limit='1GB'").format(ident))
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DB_CONFIG["dbname"]), ident)
            )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
            cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident))
            cur.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(%s)",
                (list(READER_TABLES),),
            )
            for (table,) in cur.fetchall():
                cur.execute(sql.SQL("GRANT SELECT ON public.{} TO {}").format(sql.Identifier(table), ident))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if rotate:
        env = STACK / ".env"
        set_key(str(env), "EXPLORER_DB_USER", name)
        set_key(str(env), "EXPLORER_DB_PASSWORD", password)
        env.chmod(0o600)
        os.environ["EXPLORER_DB_USER"] = name
        os.environ["EXPLORER_DB_PASSWORD"] = password
    check_role()
    print(
        "Explorer reader provisioned; credentials saved in .env (not displayed)."
        if rotate
        else "Explorer reader grants synchronized; credentials unchanged."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["provision", "sync", "check"])
    args = parser.parse_args()
    try:
        if args.action == "provision":
            provision()
        elif args.action == "sync":
            provision(rotate=False)
        else:
            check_role()
            print("Explorer role passes the read-only catalog checks.")
    except Exception:
        # DB exceptions can include credential-bearing SQL. Never print them.
        print(
            "Explorer role operation failed. Check database privileges, role ownership and .env write access.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
