"""Separate query credentials; export/report writes use only fixed application SQL."""

from contextlib import contextmanager
from datetime import datetime

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from data_explorer.policy import METADATA_TABLES, READER_TABLES, TABLES
from data_explorer.scope import scoped_table
from db import DB_CONFIG, get_conn, require_env
from labelled_table_sync import LEVEL_CONFIGS, sanitize_label_column


def query_connection(timeout_seconds=15):
    user = require_env("EXPLORER_DB_USER")
    if user == DB_CONFIG["user"]:
        raise RuntimeError("Explorer requires a separate read-only database login")
    conn = psycopg2.connect(
        **{
            **DB_CONFIG,
            "user": user,
            "password": require_env("EXPLORER_DB_PASSWORD"),
            "connect_timeout": 5,
            "application_name": "ssc-data-explorer",
        }
    )
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
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        if any(cur.fetchone()):
            raise RuntimeError("Explorer database role must not have administrative privileges")
        cur.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
        )
        if cur.fetchone()[0]:
            raise RuntimeError("Explorer database role must not inherit other roles")
        cur.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%%' "
            "AND c.relkind IN ('r','p','v','m','f') AND ("
            "has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') OR "
            "(has_any_column_privilege(c.oid, 'SELECT') AND NOT (n.nspname='public' AND c.relname = ANY(%s))))",
            (list(READER_TABLES),),
        )
        if cur.fetchone():
            raise RuntimeError("Explorer database role has grants outside the approved read-only catalog")
        cur.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_temp%%' "
            "AND has_schema_privilege(oid, 'CREATE')"
        )
        if cur.fetchone():
            raise RuntimeError("Explorer database role must not have schema CREATE privileges")

        cur.execute(
            "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace "
            "AND relkind='r' AND relname=ANY(%s) AND has_table_privilege(oid, 'SELECT')",
            (sorted(METADATA_TABLES),),
        )
        if {row[0] for row in cur.fetchall()} != METADATA_TABLES:
            raise RuntimeError("Data Exports reader lacks required label or dataset metadata grants")


def catalog():
    with reader() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
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
        """,
            (list(TABLES),),
        )
        result = {}
        for row in cur:
            table = row.pop("table_name")
            result.setdefault(table, {"name": table, "description": TABLES[table], "columns": []})["columns"].append(
                dict(row)
            )
        columns = {
            (table_name, column["name"]): column for table_name, table in result.items() for column in table["columns"]
        }
        cur.execute("SELECT name, level, datatype, instrument, description FROM ONLY public.label_definitions")
        for label in cur:
            level = LEVEL_CONFIGS.get(label["level"])
            if level is None:
                continue
            # Match PostgreSQL's identifier truncation and the mirror sanitizer.
            name = sanitize_label_column(label["name"])[:63]
            column = columns.get((level.labelled_table, name))
            if column is not None:
                column.update(
                    label_name=label["name"],
                    label_level=label["level"],
                    label_datatype=label["datatype"],
                    label_description=label["description"],
                    instrument=label["instrument"] or None,
                )
                column["description"] = column["description"] or label["description"]
        return list(result.values())


def datasets(scope=None):
    with reader() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT unnest(dataset) AS dataset FROM ONLY public.patient ORDER BY dataset")
        return [row[0] for row in cur.fetchall() if row[0] and (scope is None or row[0] in scope)]


def distinct_values(column, operator, search, timeout_seconds=15, dataset=None, scope=None):
    """Bounded suggestions from the allowed catalog, using only reader credentials."""
    table, separator, name = column.partition(".")
    metadata = next((c for t in catalog() if t["name"] == table for c in t["columns"] if c["name"] == name), None)
    if not separator or metadata is None:
        raise ValueError("Choose an available research column")
    expression = sql.Identifier(name)
    if operator == "contains" and metadata["type"].endswith("[]"):
        expression = sql.SQL("unnest({})").format(expression)
    statement = sql.SQL("""
        SELECT DISTINCT value FROM (
            SELECT ({expression})::text AS value FROM {source}
        ) AS choices
        WHERE value IS NOT NULL AND octet_length(value) <= 4096
          AND strpos(lower(value), lower(%s)) > 0
        ORDER BY value LIMIT 101
    """)
    with reader(timeout_seconds) as conn, conn.cursor() as cur:
        source = sql.SQL("ONLY public.{}").format(sql.Identifier(table))
        if dataset is not None or scope is not None:
            literal = cur.mogrify("%s", (dataset,)).decode("utf-8") if dataset is not None else None
            allowed = cur.mogrify("%s", (scope,)).decode("utf-8") if scope is not None else None
            source = sql.SQL("({}) AS scoped_values").format(sql.SQL(scoped_table(table, literal, allowed)))
        statement = statement.format(expression=expression, source=source)
        cur.execute(statement, (search,))
        values = [row[0] for row in cur.fetchall()]
    # PostgreSQL's timestamptz text ends in +00; the builder accepts ISO +00:00.
    if metadata["type"].startswith("timestamp") and not metadata["type"].endswith("[]"):
        values = [datetime.fromisoformat(value).isoformat() for value in values]
    return {"values": values[:100], "has_more": len(values) > 100}


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
