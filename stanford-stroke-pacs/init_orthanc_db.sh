#!/usr/bin/env bash
# Create the Orthanc PostgreSQL database and role.
# Idempotent — safe to re-run.  Reads credentials from .env.
#
# Connects via TCP using DB_USER/DB_PASSWORD (must have CREATEDB privilege).
#
# Usage:  ./init_orthanc_db.sh
set -euo pipefail

# Resolve .env relative to this script so the bootstrap is portable across
# hosts/checkouts (no hardcoded absolute path). Override with ENV_FILE=... .
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
source "$ENV_FILE"

: "${DB_USER:?DB_USER not set in $ENV_FILE}"
: "${DB_PASSWORD:?DB_PASSWORD not set in $ENV_FILE}"
: "${PG_ORTHANC_USER:?PG_ORTHANC_USER not set in $ENV_FILE}"
: "${PG_ORTHANC_DB:?PG_ORTHANC_DB not set in $ENV_FILE}"
: "${PG_ORTHANC_PASSWORD:?PG_ORTHANC_PASSWORD not set in $ENV_FILE}"

echo "Ensuring Orthanc database role and database exist..."
echo "  Connecting as ${DB_USER} to ${DB_HOST}:${DB_PORT}"

# Credentials reach psql through the *environment*, never through the SQL text
# or the command line. The heredoc is quoted ('SQL') so the shell interpolates
# nothing; \getenv loads the values into psql variables and format()'s %I/%L
# quote them server-side. A password containing ', $, @ or ! therefore cannot
# break the statement (or inject into it), and never appears in `ps` output.
export PGPASSWORD="$DB_PASSWORD"
export PG_ORTHANC_USER PG_ORTHANC_DB PG_ORTHANC_PASSWORD

# ON_ERROR_STOP: without it psql runs on past a failed statement and still
# exits 0, so the script would report "Done." after a half-applied bootstrap.
psql -v ON_ERROR_STOP=1 \
     -U "${DB_USER}" -h "${DB_HOST}" -p "${DB_PORT}" -d postgres <<'SQL'
\getenv orthanc_user PG_ORTHANC_USER
\getenv orthanc_db PG_ORTHANC_DB
\getenv orthanc_password PG_ORTHANC_PASSWORD

SELECT format('CREATE ROLE %I WITH LOGIN PASSWORD %L', :'orthanc_user', :'orthanc_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'orthanc_user')
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'orthanc_db', :'orthanc_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'orthanc_db')
\gexec

SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'orthanc_db', :'orthanc_user')
\gexec
SQL

echo "Done."
