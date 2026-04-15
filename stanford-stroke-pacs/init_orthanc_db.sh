#!/usr/bin/env bash
# Create the Orthanc PostgreSQL database and role.
# Idempotent — safe to re-run.  Reads credentials from .env.
#
# Connects via TCP using DB_USER/DB_PASSWORD (must have CREATEDB privilege).
#
# Usage:  ./init_orthanc_db.sh
set -euo pipefail

source "/home/perecanals/pacs/stanford-stroke-pacs/.env"

echo "Ensuring Orthanc database role and database exist..."
echo "  Connecting as ${DB_USER} to ${DB_HOST}:${DB_PORT}"

PGPASSWORD="${DB_PASSWORD}" psql -U "${DB_USER}" -h "${DB_HOST}" -p "${DB_PORT}" -d postgres <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${PG_ORTHANC_USER}') THEN
        CREATE ROLE ${PG_ORTHANC_USER} WITH LOGIN PASSWORD '${PG_ORTHANC_PASSWORD}';
        RAISE NOTICE 'Created role ${PG_ORTHANC_USER}';
    ELSE
        RAISE NOTICE 'Role ${PG_ORTHANC_USER} already exists';
    END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${PG_ORTHANC_DB} OWNER ${PG_ORTHANC_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${PG_ORTHANC_DB}')
\gexec

GRANT ALL PRIVILEGES ON DATABASE ${PG_ORTHANC_DB} TO ${PG_ORTHANC_USER};
SQL

echo "Done."
