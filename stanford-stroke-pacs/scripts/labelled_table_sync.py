"""Helpers for maintaining per-level labelled mirror tables."""

from __future__ import annotations

import re
from dataclasses import dataclass

from psycopg2 import sql

VALID_LEVELS = ("patient", "study", "series")


@dataclass(frozen=True)
class LevelConfig:
    source_table: str
    labelled_table: str
    source_key: str
    annotation_key: str


LEVEL_CONFIGS = {
    "patient": LevelConfig(
        source_table="lvo_clinical_data",
        labelled_table="lvo_clinical_data_labelled",
        source_key="study_id",
        annotation_key="patient_id",
    ),
    "study": LevelConfig(
        source_table="image_study",
        labelled_table="image_study_labelled",
        source_key="studyinstanceuid",
        annotation_key="studyinstanceuid",
    ),
    "series": LevelConfig(
        source_table="image_series",
        labelled_table="image_series_labelled",
        source_key="seriesinstanceuid",
        annotation_key="seriesinstanceuid",
    ),
}

DATATYPE_SQL = {
    "bool": "BOOLEAN NOT NULL DEFAULT FALSE",
    "int": "INTEGER",
    "text": "TEXT",
    "select": "TEXT",
}


def normalize_levels(levels: list[str] | tuple[str, ...] | None) -> list[str]:
    if levels is None:
        return list(VALID_LEVELS)
    normalized = []
    for level in levels:
        if level in VALID_LEVELS and level not in normalized:
            normalized.append(level)
    return normalized


def get_level_config(level: str) -> LevelConfig:
    if level not in LEVEL_CONFIGS:
        raise ValueError(f"Unsupported level: {level}")
    return LEVEL_CONFIGS[level]


def sanitize_label_column(label_name: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z]+", "_", label_name.strip().lower()).strip("_")
    if not safe:
        safe = "unnamed"
    if safe[0].isdigit():
        safe = f"n_{safe}"
    return f"label_{safe}"


def _get_table_columns(conn, table_name: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                a.attname AS column_name,
                format_type(a.atttypid, a.atttypmod) AS column_type
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s
              AND n.nspname = current_schema()
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (table_name,),
        )
        return [
            {"name": row[0], "type": row[1]}
            for row in cur.fetchall()
        ]


def _get_label_defs(conn, level: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, datatype FROM label_definitions WHERE level = %s ORDER BY name",
            (level,),
        )
        return [
            {"name": row[0], "datatype": row[1]}
            for row in cur.fetchall()
        ]


def find_label_column_conflict(conn, level: str, label_name: str) -> str | None:
    target_column = sanitize_label_column(label_name)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM label_definitions WHERE level = %s ORDER BY name",
            (level,),
        )
        for (existing_name,) in cur.fetchall():
            if existing_name == label_name:
                continue
            if sanitize_label_column(existing_name) == target_column:
                return existing_name
    return None


def ensure_labelled_tables(conn) -> None:
    for level in VALID_LEVELS:
        _ensure_level_table(conn, level)
        sync_labelled_schema(conn, level)


def sync_labelled_schema(conn, level: str) -> list[str]:
    config = get_level_config(level)
    _ensure_level_table(conn, level)
    existing = {col["name"] for col in _get_table_columns(conn, config.labelled_table)}
    added = []
    with conn.cursor() as cur:
        for label_def in _get_label_defs(conn, level):
            column_name = sanitize_label_column(label_def["name"])
            if column_name in existing:
                continue
            cur.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                    sql.Identifier(config.labelled_table),
                    sql.Identifier(column_name),
                    sql.SQL(DATATYPE_SQL[label_def["datatype"]]),
                )
            )
            existing.add(column_name)
            added.append(column_name)
    return added


def sync_labelled_rows(conn, level: str, entity_ids: list[str] | tuple[str, ...] | set[str] | None = None) -> int:
    config = get_level_config(level)
    _ensure_level_table(conn, level)

    normalized_ids = None
    if entity_ids is not None:
        normalized_ids = sorted(
            {str(entity_id).strip() for entity_id in entity_ids if entity_id is not None and str(entity_id).strip()}
        )
        if not normalized_ids:
            return 0

    base_columns = _get_table_columns(conn, config.source_table)
    label_defs = _get_label_defs(conn, level)
    insert_columns = [column["name"] for column in base_columns]
    update_columns = [column for column in insert_columns if column != config.source_key]
    lateral_joins = []
    select_columns = [
        sql.SQL("src.{}").format(sql.Identifier(column["name"]))
        for column in base_columns
    ]

    for index, label_def in enumerate(label_defs):
        alias_name = f"ann_{index}"
        column_name = sanitize_label_column(label_def["name"])
        insert_columns.append(column_name)
        update_columns.append(column_name)

        if label_def["datatype"] == "bool":
            select_columns.append(
                sql.SQL("COALESCE({}.value, FALSE) AS {}").format(
                    sql.Identifier(alias_name),
                    sql.Identifier(column_name),
                )
            )
            lateral_joins.append(
                sql.SQL(
                    "LEFT JOIN LATERAL ("
                    "SELECT TRUE AS value "
                    "FROM annotations a "
                    "WHERE a.level = {} "
                    "AND a.{} = src.{} "
                    "AND a.label = {} "
                    "LIMIT 1"
                    ") AS {} ON TRUE"
                ).format(
                    sql.Literal(level),
                    sql.Identifier(config.annotation_key),
                    sql.Identifier(config.source_key),
                    sql.Literal(label_def["name"]),
                    sql.Identifier(alias_name),
                )
            )
            continue

        if label_def["datatype"] == "int":
            value_expr = sql.SQL(
                "CASE "
                "WHEN NULLIF(BTRIM(a.value), '') IS NULL THEN NULL "
                "WHEN BTRIM(a.value) ~ '^-?[0-9]+$' THEN BTRIM(a.value)::INTEGER "
                "ELSE NULL "
                "END AS value"
            )
        else:
            value_expr = sql.SQL("NULLIF(BTRIM(a.value), '') AS value")

        select_columns.append(
            sql.SQL("{}.value AS {}").format(
                sql.Identifier(alias_name),
                sql.Identifier(column_name),
            )
        )
        lateral_joins.append(
            sql.SQL(
                "LEFT JOIN LATERAL ("
                "SELECT {value_expr} "
                "FROM annotations a "
                "WHERE a.level = {level} "
                "AND a.{annotation_key} = src.{source_key} "
                "AND a.label = {label} "
                "ORDER BY a.created_at DESC NULLS LAST, a.id DESC "
                "LIMIT 1"
                ") AS {alias} ON TRUE"
            ).format(
                value_expr=value_expr,
                level=sql.Literal(level),
                annotation_key=sql.Identifier(config.annotation_key),
                source_key=sql.Identifier(config.source_key),
                label=sql.Literal(label_def["name"]),
                alias=sql.Identifier(alias_name),
            )
        )

    insert_stmt = sql.SQL(
        "INSERT INTO {labelled_table} ({insert_columns}) "
        "SELECT {select_columns} "
        "FROM {source_table} src "
        "{lateral_joins} "
        "{where_clause} "
        "ON CONFLICT ({conflict_column}) DO UPDATE "
        "SET {update_assignments}"
    ).format(
        labelled_table=sql.Identifier(config.labelled_table),
        insert_columns=sql.SQL(", ").join(sql.Identifier(name) for name in insert_columns),
        select_columns=sql.SQL(", ").join(select_columns),
        source_table=sql.Identifier(config.source_table),
        lateral_joins=sql.SQL(" ").join(lateral_joins),
        where_clause=sql.SQL("") if normalized_ids is None else sql.SQL("WHERE src.{} = ANY(%s)").format(
            sql.Identifier(config.source_key)
        ),
        conflict_column=sql.Identifier(config.source_key),
        update_assignments=sql.SQL(", ").join(
            sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(name))
            for name in update_columns
        ),
    )

    delete_stmt = sql.SQL(
        "DELETE FROM {labelled_table} dst "
        "WHERE dst.{key} = ANY(%s) "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM {source_table} src WHERE src.{key} = dst.{key}"
        ")"
    ).format(
        labelled_table=sql.Identifier(config.labelled_table),
        source_table=sql.Identifier(config.source_table),
        key=sql.Identifier(config.source_key),
    )

    with conn.cursor() as cur:
        if normalized_ids is None:
            cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(config.labelled_table)))
            cur.execute(insert_stmt)
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(config.labelled_table)))
            return cur.fetchone()[0]

        cur.execute(delete_stmt, (normalized_ids,))
        cur.execute(insert_stmt, (normalized_ids,))
        return cur.rowcount


def rebuild_labelled_tables(conn, levels: list[str] | tuple[str, ...] | None = None) -> dict[str, int]:
    ensure_labelled_tables(conn)
    counts = {}
    for level in normalize_levels(levels):
        sync_labelled_schema(conn, level)
        config = get_level_config(level)
        counts[config.labelled_table] = sync_labelled_rows(conn, level, entity_ids=None)
    return counts


def _ensure_level_table(conn, level: str) -> None:
    config = get_level_config(level)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} AS TABLE {} WITH NO DATA").format(
                sql.Identifier(config.labelled_table),
                sql.Identifier(config.source_table),
            )
        )
        for source_column in _get_table_columns(conn, config.source_table):
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s
                  AND column_name = %s
                """,
                (config.labelled_table, source_column["name"]),
            )
            if cur.fetchone():
                continue
            cur.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                    sql.Identifier(config.labelled_table),
                    sql.Identifier(source_column["name"]),
                    sql.SQL(source_column["type"]),
                )
            )
        cur.execute(
            sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})").format(
                sql.Identifier(f"{config.labelled_table}_{config.source_key}_uidx"),
                sql.Identifier(config.labelled_table),
                sql.Identifier(config.source_key),
            )
        )
