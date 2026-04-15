"""Remove an annotation label: definition, annotation rows, and _labelled column."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

from labelled_table_sync import LEVEL_CONFIGS, sanitize_label_column

ENV_PATH = Path(__file__).resolve().parent / "../.env"
load_dotenv(ENV_PATH)

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)


def main():
    if os.geteuid() != 0:
        print("Error: this script must be run with sudo.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Remove an annotation label entirely.")
    parser.add_argument("label_name", help="Exact name of the label to remove")
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
