"""Separate query credentials; export/report writes use only fixed application SQL."""
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

from data_explorer.policy import TABLES
from db import DB_CONFIG, get_conn, require_env


def query_connection(timeout_seconds=15):
    user = require_env("EXPLORER_DB_USER")
    if user == DB_CONFIG["user"]:
        raise RuntimeError("Explorer requires a separate read-only database login")
    conn = psycopg2.connect(**{
        **DB_CONFIG, "user": user, "password": require_env("EXPLORER_DB_PASSWORD"),
        "connect_timeout": 5, "application_name": "ssc-data-explorer",
    })
    try:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path = pg_catalog")
            cur.execute("SET LOCAL statement_timeout = %s", (timeout_seconds * 1000,))
            cur.execute("SET LOCAL lock_timeout = '2s'")
            cur.execute("SET LOCAL idle_in_transaction_session_timeout = '60s'")
            cur.execute("SET LOCAL work_mem = '16MB'")
            cur.execute("SET LOCAL timezone = 'UTC'")
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def reader(timeout_seconds=15):
    conn = query_connection(timeout_seconds)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def check_role():
    """Fail closed on privileged/misprovisioned roles, including inherited grants."""
    with reader() as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user")
        if any(cur.fetchone()):
            raise RuntimeError("Explorer database role must not have administrative privileges")
        cur.execute("SELECT count(*) FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)")
        if cur.fetchone()[0]:
            raise RuntimeError("Explorer database role must not inherit other roles")
        cur.execute("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%%' "
                    "AND c.relkind IN ('r','p','v','m','f') AND ("
                    "has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') OR "
                    "(has_any_column_privilege(c.oid, 'SELECT') AND NOT (n.nspname='public' AND c.relname = ANY(%s))))",
                    (list(TABLES),))
        if cur.fetchone():
            raise RuntimeError("Explorer database role has grants outside the approved read-only catalog")
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_temp%%' "
                    "AND has_schema_privilege(oid, 'CREATE')")
        if cur.fetchone():
            raise RuntimeError("Explorer database role must not have schema CREATE privileges")


def catalog():
    with reader() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT c.relname AS table_name, a.attname AS name,
                   format_type(a.atttypid, a.atttypmod) AS type,
                   col_description(c.oid,a.attnum) AS description
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid
            JOIN pg_type t ON t.oid=a.atttypid
            WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(%s)
              AND a.attnum>0 AND NOT a.attisdropped AND has_table_privilege(c.oid,'SELECT')
              AND t.typnamespace = 'pg_catalog'::regnamespace
            ORDER BY c.relname,a.attnum
        """, (list(TABLES),))
        result = {}
        for row in cur:
            table = row.pop("table_name")
            result.setdefault(table, {"name": table, "description": TABLES[table], "columns": []})["columns"].append(dict(row))
        return list(result.values())


def records(statement, params=(), *, one=False):
    """Only call with module-owned fixed SQL, never submitted SQL."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(statement, params)
            result = (cur.fetchone() if one else cur.fetchall()) if cur.description else None
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
