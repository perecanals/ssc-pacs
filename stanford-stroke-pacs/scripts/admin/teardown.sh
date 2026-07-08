#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Error: this script must be run with sudo." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# stanford-stroke-pacs/ — two levels up from scripts/admin/
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$STACK_DIR/.env"

source "$ENV_FILE"

echo "=== SSC PACS Teardown ==="
echo ""
echo "WARNING: This will permanently destroy:"
echo "  - Docker containers and volumes (all stored DICOM SRs, etc.)"
echo "  - The Orthanc PostgreSQL database and role"
echo "  - Orthanc-related variables in .env"
echo ""

MAX_ATTEMPTS=3
for attempt in $(seq 1 $MAX_ATTEMPTS); do
    read -rp "Are you sure you want to proceed? (yes/no) [$attempt/$MAX_ATTEMPTS]: " answer
    if [[ "${answer,,}" == "yes" ]]; then
        break
    fi
    if [[ $attempt -eq $MAX_ATTEMPTS ]]; then
        echo "Aborted after $MAX_ATTEMPTS attempts."
        exit 0
    fi
    echo "Please type 'yes' to confirm."
done

echo ""
echo "Stopping and removing Orthanc container and volume..."
cd "$STACK_DIR"
docker compose down -v

echo "Dropping Orthanc database and role from PostgreSQL..."
PGPASSWORD="$DB_PASSWORD" psql -U "$DB_USER" -h localhost -d postgres -c "DROP DATABASE IF EXISTS ${PG_ORTHANC_DB};"
PGPASSWORD="$DB_PASSWORD" psql -U "$DB_USER" -h localhost -d postgres -c "DROP ROLE IF EXISTS ${PG_ORTHANC_USER};"

echo "Removing Orthanc variables from .env..."
sed -i '/^# === Orthanc PACS Configuration ===/,$d' "$ENV_FILE"

echo "Done. All Orthanc resources have been removed."
