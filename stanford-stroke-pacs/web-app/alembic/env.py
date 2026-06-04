"""Alembic runtime environment for the `stanford-stroke` database.

Builds the SQLAlchemy URL from the same environment variables the
the web app uses (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD), and
configures `include_object` so `--autogenerate` ignores tables owned by
upstream (raw clinical/imaging tables) or by `labelled_table_sync.py`
(labelled / snapshot tables built dynamically from label_definitions).

This file is invoked by the `alembic` CLI and by the in-process call
from `web-app/app.py:init_db`.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import engine_from_config, pool

from alembic import context

# --- Path bootstrap ----------------------------------------------------------
# env.py runs with CWD set wherever the caller invokes alembic from. Make sure
# we can import the repo's `config.py` (one level above `web-app/`) and pick
# up `.env` consistently.
_WEB_APP_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _WEB_APP_DIR.parent
for p in (_WEB_APP_DIR, _REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")


# --- Alembic config ----------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _build_db_url() -> str:
    """Build the SQLAlchemy URL from .env, matching web-app/app.py."""
    user = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASSWORD")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "stanford-stroke")
    if not user or not password:
        raise RuntimeError(
            "DB_USER and DB_PASSWORD must be set in the environment / .env "
            "before running alembic."
        )
    return (
        f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(name)}"
    )


# Allow override via DATABASE_URL for scratch/test runs (see verification
# section of workstreams/04-schema-migrations.md).
db_url = os.environ.get("DATABASE_URL") or _build_db_url()
# NB: don't call set_main_option — `%` in URL-encoded passwords trips
# ConfigParser's interpolation. Pass the URL directly to engine_from_config
# in run_migrations_online() instead.


# --- Autogenerate scope filter ----------------------------------------------
# Tables owned outside the web app's migration scope. Listed by name only —
# they all live in the `public` schema. See workstream 04 §2.
UPSTREAM_TABLES = frozenset({
    "image_series",
    "image_study",
    "lvo_clinical_data",
})

# Tables created/maintained by labelled_table_sync.py at runtime based on
# label_definitions. Their shape changes with annotations, not with code, so
# Alembic must not try to manage them.
LABELLED_TABLES = frozenset({
    "image_series_labelled",
    "image_study_labelled",
    "lvo_clinical_data_labelled",
    "snapshot_patients",
    "snapshot_studys",
    "snapshot_seriess",
})

EXCLUDED_TABLES = UPSTREAM_TABLES | LABELLED_TABLES


def include_object(object_, name, type_, reflected, compare_to):
    """Filter callback for --autogenerate.

    Skip tables we don't own and any indexes/constraints attached to them,
    so autogen drafts only touch web-app-owned tables.
    """
    if type_ == "table" and name in EXCLUDED_TABLES:
        return False
    if type_ in ("index", "unique_constraint", "foreign_key_constraint"):
        table_name = getattr(object_, "table", None)
        table_name = getattr(table_name, "name", None) if table_name else None
        if table_name in EXCLUDED_TABLES:
            return False
    return True


# No SQLAlchemy models — the web app uses raw psycopg2. Set target_metadata to
# None; --autogenerate would only emit "drop everything" without a model
# layer, so we'll write revisions by hand.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the DB and execute."""
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = db_url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            include_schemas=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
