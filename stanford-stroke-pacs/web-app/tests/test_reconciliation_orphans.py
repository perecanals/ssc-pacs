"""Orphaned-annotation detection in reconciliation.py.

Annotations have no FK to the upstream spine tables, so entity rows deleted or
renamed out from under them leave orphans. `_get_orphaned_annotations` flags
them per level; the report/summary carry an `orphaned_annotations` category.
"""

from __future__ import annotations

from reconciliation import (
    _get_orphaned_annotations,
    snapshot_summary_from_mismatches,
)


def _insert_annotation(conn, level, entity_col, entity_id, label="test_label"):
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO annotations (level, {entity_col}, label, value, created_by) "
            "VALUES (%s, %s, %s, 'x', 'pytest')",
            (level, entity_id, label),
        )


def test_orphans_flagged_per_level(db_conn):
    # Valid annotation on a seeded patient — must NOT be flagged.
    _insert_annotation(db_conn, "patient", "patient_id", "P-0001", "valid_label")
    # Orphans: entities that exist in no spine table.
    _insert_annotation(db_conn, "patient", "patient_id", "P-GONE")
    _insert_annotation(db_conn, "study", "studyinstanceuid", "9.9.9.9")
    _insert_annotation(db_conn, "series", "seriesinstanceuid", "9.9.9.9.9")

    orphans = _get_orphaned_annotations(db_conn)
    found = {(o["level"], o["entity_id"]) for o in orphans}
    assert ("patient", "P-GONE") in found
    assert ("study", "9.9.9.9") in found
    assert ("series", "9.9.9.9.9") in found
    assert ("patient", "P-0001") not in found
    for o in orphans:
        assert set(o) == {"level", "entity_id", "label", "created_by"}


def test_no_orphans_on_clean_db(db_conn):
    _insert_annotation(db_conn, "patient", "patient_id", "P-0001")
    _insert_annotation(db_conn, "series", "seriesinstanceuid", "1.2.3.4.5.6")
    assert _get_orphaned_annotations(db_conn) == []


def test_summary_includes_orphaned_annotations_count():
    mismatches = {
        "in_db_not_in_orthanc": [],
        "in_orthanc_not_in_db": [{"seriesinstanceuid": "x"}],
        "dicom_archive_missing": [],
        "orphaned_annotations": [
            {"level": "patient", "entity_id": "P-GONE", "label": "l", "created_by": "u"},
            {"level": "series", "entity_id": "9.9", "label": "l", "created_by": "u"},
        ],
    }
    summary = snapshot_summary_from_mismatches(mismatches, db_count=10, orthanc_count=11)
    assert summary["orphaned_annotations"] == 2
    assert summary["total_mismatches"] == 3  # 1 orthanc-only + 2 orphans
    # Coverage/matched stay defined by the DB-vs-Orthanc axis only.
    assert summary["matched"] == 10
