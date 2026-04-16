#!/usr/bin/env python3
"""Backfill annotations_history with one synthetic 'I' row per existing annotation.

Usage:
    python scripts/backfill_annotation_history.py --dry-run   # preview only
    python scripts/backfill_annotation_history.py --execute   # apply

Idempotent: uses ON CONFLICT-safe logic (checks for existing history rows
before inserting) so it can be safely re-run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bootstrap imports — repo layout: scripts/ is a sibling of companion/
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPANION_DIR = _REPO_ROOT / "companion"
sys.path.insert(0, str(_COMPANION_DIR))
sys.path.insert(0, str(_REPO_ROOT))

from db import get_conn  # noqa: E402


def _entity_id_expr() -> str:
    return (
        "CASE level "
        "WHEN 'patient' THEN patient_id "
        "WHEN 'study' THEN studyinstanceuid "
        "ELSE seriesinstanceuid END"
    )


def backfill(*, execute: bool) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Find annotations that have no history rows yet.
            cur.execute(
                f"SELECT id, level, {_entity_id_expr()} AS entity_id, "
                "label, value, notes, created_by, created_at "
                "FROM annotations "
                "WHERE id NOT IN (SELECT DISTINCT annotation_id FROM annotations_history) "
                "ORDER BY id"
            )
            rows = cur.fetchall()

            if not rows:
                print("No annotations without history rows found. Nothing to do.")
                return 0

            print(f"Found {len(rows)} annotation(s) without history rows.")

            if not execute:
                for r in rows[:10]:
                    print(f"  [DRY-RUN] id={r[0]} level={r[1]} entity={r[2]} label={r[3]}")
                if len(rows) > 10:
                    print(f"  ... and {len(rows) - 10} more")
                return len(rows)

            # Insert synthetic 'I' rows.
            inserted = 0
            for r in rows:
                ann_id, level, entity_id, label, value, notes, created_by, created_at = r
                cur.execute(
                    "INSERT INTO annotations_history "
                    "(operation, operation_at, operation_by, annotation_id, level, "
                    "entity_id, label, value_after, notes_after, created_by) "
                    "VALUES ('I', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        created_at,  # use original creation time
                        created_by,  # attribute to original creator
                        ann_id, level, entity_id, label,
                        value, notes, created_by,
                    ),
                )
                inserted += 1

            conn.commit()
            print(f"Inserted {inserted} backfill history row(s).")
            return inserted
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without writing")
    group.add_argument("--execute", action="store_true", help="Apply the backfill")
    args = parser.parse_args()

    count = backfill(execute=args.execute)
    if args.dry_run:
        print(f"\nDry run complete. {count} row(s) would be inserted.")
    else:
        print(f"\nBackfill complete. {count} row(s) inserted.")


if __name__ == "__main__":
    main()
