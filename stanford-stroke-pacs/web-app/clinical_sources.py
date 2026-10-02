"""Per-dataset clinical tables, as registered on the `dataset` row (Alembic 0027).

A dataset may have one clinical table (uploaded with
``scripts/admin/manage_datasets.py import-clinical``): ``clinical_table`` keyed
by ``clinical_id_column`` (the dataset's patient id), with the episode date in
``clinical_date_column``. A clinical row only ever matches enrollments *of that
dataset* — a same-id patient elsewhere is someone else.

``timepoint_strategy = 'crisp2_puncture'`` marks the dataset whose clinical
table carries the CRISP2 puncture columns that anchor timepoints. That rule is
CRISP2-specific on purpose; other datasets anchor on their thrombectomy study.

Table and column names come from the database, so they are CHECK-constrained
there and always quoted here (psycopg2.sql.Identifier). A registered table or
column that has gone missing is skipped with a warning, never an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg2 import sql

logger = logging.getLogger(__name__)

# The CRISP2 REDCap columns the 'crisp2_puncture' strategy reads.
CRISP2_ANCHOR_COLUMNS = ("femoral_sheath_time", "receiving_arrival_time", "time_recognized")

_warned: set[tuple] = set()


@dataclass(frozen=True)
class ClinicalSource:
    dataset: str
    table: str
    id_column: str
    date_column: str
    strategy: str | None
    # A date/timestamp column (an Excel upload) rather than text (REDCap).
    date_is_temporal: bool = False


def _rows(cur):
    return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in cur.fetchall()]


def _warn_once(key, message, *args):
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _column_types(cur, table: str) -> dict[str, str]:
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return dict(_rows(cur))


def table_columns(cur, table: str) -> set[str]:
    return set(_column_types(cur, table))


def clinical_sources(cur) -> list[ClinicalSource]:
    """Registered clinical tables whose table and key columns exist."""
    cur.execute(
        "SELECT name, clinical_table, clinical_id_column, clinical_date_column, "
        "timepoint_strategy FROM dataset WHERE clinical_table IS NOT NULL ORDER BY name"
    )
    sources = []
    for row in _rows(cur):
        source = ClinicalSource(*row)
        types = _column_types(cur, source.table)
        missing = {source.id_column, source.date_column} - set(types)
        if missing:
            _warn_once(
                ("missing", source.table, tuple(sorted(missing))),
                "Clinical table %r registered for %r lacks %s (or does not exist); "
                "it is ignored until fixed (manage_datasets.py import-clinical).",
                source.table, source.dataset, ", ".join(sorted(missing)),
            )
            continue
        temporal = types[source.date_column].startswith(("date", "timestamp"))
        sources.append(ClinicalSource(*row, date_is_temporal=temporal))
    return sources


def _ident(cur, *names) -> str:
    return sql.Identifier(*names).as_string(cur)


def _literal(cur, value) -> str:
    # Inlined into a statement later executed with parameters: double the %.
    return cur.mogrify("%s", (value,)).decode().replace("%", "%%")


def episode_date_sql(cur, patient_alias: str = "p") -> tuple[str, str]:
    """(joins, expression) for an enrollment's episode date.

    One LEFT JOIN per registered clinical table, each restricted to its own
    dataset's enrollments; the expression prefers the clinical date and falls
    back to the imaging-derived ``stroke_date``. Text on both sides (the cast is
    load-bearing: it keeps the lexicographic date sort valid).
    """
    joins, dates = [], []
    for i, source in enumerate(clinical_sources(cur)):
        alias = f"cd{i}"
        joins.append(
            f"LEFT JOIN {_ident(cur, source.table)} {alias} "
            f"ON {alias}.{_ident(cur, source.id_column)}::text = {patient_alias}.patient_id "
            f"AND {patient_alias}.dataset = {_literal(cur, source.dataset)}"
        )
        date = f"{alias}.{_ident(cur, source.date_column)}"
        # A date/timestamp column shows as YYYY-MM-DD, like the imaging fallback.
        dates.append(f"{date}::date::text" if source.date_is_temporal else f"{date}::text")
    dates.append(f"{patient_alias}.stroke_date::date::text")
    return " ".join(joins), f"COALESCE({', '.join(dates)})"


def crisp2_anchor_source(cur) -> ClinicalSource | None:
    """The clinical table that anchors timepoints on the CRISP2 puncture, if any."""
    for source in clinical_sources(cur):
        if source.strategy != "crisp2_puncture":
            continue
        missing = set(CRISP2_ANCHOR_COLUMNS) - table_columns(cur, source.table)
        if missing:
            _warn_once(
                ("anchor", source.table),
                "Dataset %r uses timepoint_strategy 'crisp2_puncture' but %r lacks %s; "
                "timepoints fall back to the thrombectomy study.",
                source.dataset, source.table, ", ".join(sorted(missing)),
            )
            return None
        return source
    return None


def clinical_source_for(cur, dataset: str) -> ClinicalSource | None:
    return next((s for s in clinical_sources(cur) if s.dataset == dataset), None)
