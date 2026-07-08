"""Test bootstrap for the ingestion package.

Puts the package dir on sys.path so tests import the flat script modules
directly (the protocol is a script directory, not an installed package;
`pytest` from either the package dir or the repo root works with this shim),
and supplies dummy secrets so the DB-free tests can import the executor's
web-app dependencies without a real environment.
"""

import os
import sys
from pathlib import Path

# The executor imports web-app modules (config, labelled_table_sync -> common
# -> db, and the cold_storage scripts -> orthanc_client/auth) whose
# module-level require_env() refuses to import without these secrets. These
# tests never open a connection or call Orthanc, so dummy values suffice;
# setdefault respects a real .env when present (local dev) and only fills the
# gaps in a bare environment (CI).
for _key, _dummy in {
    "DB_USER": "ci_user",
    "DB_PASSWORD": "ci_pass",
    "JWT_SECRET": "ci-test-jwt-secret-32bytes!!!!",
    "ORTHANC_ADMIN_USER": "ci_orthanc",
    "ORTHANC_ADMIN_PASSWORD": "ci_orthanc_pass",
}.items():
    os.environ.setdefault(_key, _dummy)

_PKG_DIR = str(Path(__file__).resolve().parents[1])
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
