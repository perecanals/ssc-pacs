import csv
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

def load_dataframe(csv_path, delimiter, has_header):
    if has_header:
        return pd.read_csv(csv_path, sep=delimiter)

    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        first_row = next(csv.reader(handle, delimiter=delimiter), None)

    if first_row is None:
        return pd.DataFrame()

    column_names = [f"column_{idx + 1}" for idx in range(len(first_row))]
    return pd.read_csv(csv_path, sep=delimiter, header=None, names=column_names)


def main(csv_path: str, table_name: str):
    db = os.getenv("DB_NAME")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    delimiter = os.getenv("IMPORT_DELIMITER", ",")
    has_header = os.getenv("IMPORT_HAS_HEADER", "true").lower() != "false"
    if_exists = os.getenv("IMPORT_IF_EXISTS", "fail")

    csv_path = os.path.abspath(csv_path)
    if not os.path.exists(csv_path):
        print(f"CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    if not db:
        print("Missing database name. Set DB_NAME in .env.", file=sys.stderr)
        sys.exit(1)

    if not user:
        print("Missing PostgreSQL user. Set DB_USER in .env.", file=sys.stderr)
        sys.exit(1)

    if "." in table_name:
        schema, table = table_name.split(".", 1)
    else:
        schema, table = "public", table_name

    db_url = URL.create(
        "postgresql+psycopg2",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=db,
    )
    engine = create_engine(db_url)

    print(f"Loading .env from: {ENV_PATH}")
    print(f"Connecting to database: {db}")
    print(f"Importing CSV into: {table_name}")

    df = load_dataframe(csv_path, delimiter, has_header)
    if df.empty:
        print("CSV file is empty. Nothing to import.")
        return

    print(f"CSV columns: {', '.join(df.columns.astype(str))}")
    print(f"Rows to import: {len(df)}")

    df.to_sql(
        name=table,
        con=engine,
        schema=schema,
        if_exists=if_exists,
        index=False,
        method="multi",
        chunksize=1000,
    )

    print(f"Imported {len(df)} rows.")


if __name__ == "__main__":
    csv_path = sys.argv[1]
    table_name = sys.argv[2]

    print(f"CSV file: {csv_path}")
    print(f"Importing CSV into: {table_name}")

    if not csv_path or not table_name:
        print("Usage: python import_csv_to_postgres.py <csv_path> <table_name>")
        sys.exit(1)

    main(csv_path, table_name)
