#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="python"
CSV_PATH="/home/perecanals/pacs/ssc-sql-db/tables/clean/lvo_db_feb2026.csv"
TABLE_NAME="public.lvo_clinical_data"

echo "Using importer: ${SCRIPT_DIR}/import_csv_to_postgres.py"
echo "CSV_PATH=${CSV_PATH}"
echo "TABLE_NAME=${TABLE_NAME}"

"${PYTHON_BIN}" "${SCRIPT_DIR}/import_csv_to_postgres.py" "${CSV_PATH}" "${TABLE_NAME}"
