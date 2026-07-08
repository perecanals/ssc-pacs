"""Remove an annotation label: definition, annotation rows, and _labelled column.

Prints the affected counts, then asks for confirmation (``--yes`` bypasses).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG  # noqa: E402
from labelled_table_sync import LEVEL_CONFIGS, sanitize_label_column  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Remove an annotation label entirely.")
    parser.add_argument("label_name", help="Exact name of the label to remove")
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt (for scripted use).",
    )
    args = parser.parse_args()
    label_name = args.label_name

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, level FROM label_definitions WHERE name = %s",
                (label_name,),
            )
            row = cur.fetchone()
            if row is None:
                print(f"Label '{label_name}' not found in label_definitions.")
                sys.exit(1)

            label_id, level = row
            config = LEVEL_CONFIGS[level]
            column_name = sanitize_label_column(label_name)

            cur.execute(
                "SELECT COUNT(*) FROM annotations WHERE label = %s",
                (label_name,),
            )
            ann_count = cur.fetchone()[0]

        print(f"Label:           {label_name}")
        print(f"Level:           {level}")
        print(f"Annotation rows: {ann_count}")
        print(f"Labelled table:  {config.labelled_table}")
        print(f"Column to drop:  {column_name}")
        print()
        if not args.yes:
            answer = input("Proceed with deletion? [y/N] ").strip().lower()
            if answer != "y":
                print("Aborted.")
                sys.exit(0)

        with conn.cursor() as cur:
            cur.execute("DELETE FROM annotations WHERE label = %s", (label_name,))
            cur.execute("DELETE FROM label_definitions WHERE id = %s", (label_id,))
            cur.execute(
                sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {}").format(
                    sql.Identifier(config.labelled_table),
                    sql.Identifier(column_name),
                )
            )
        conn.commit()
        print(f"Done. Removed label '{label_name}' and {ann_count} annotation(s).")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
