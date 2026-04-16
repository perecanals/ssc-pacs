"""Centralized database connection management.

Single source of truth for DB_CONFIG and the optional connection pool.
All modules that need a database connection should import from here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    """Return the value of env var *name*, or abort with a clear error."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(
            f"{name} must be set (missing or empty in environment / .env). "
            "Refusing to start without required secrets."
        )
    return value


DB_CONFIG: dict[str, str] = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=_require_env("DB_USER"),
    password=_require_env("DB_PASSWORD"),
)

POOL_MIN = 2
POOL_MAX = 10

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


class _PooledConnection:
    """Thin wrapper that returns a connection to the pool on `close()`.

    All attribute access is delegated to the underlying psycopg2 connection
    so callers see no difference.  The wrapper exists solely to intercept
    ``close()`` — calling it returns the connection to the pool instead of
    destroying it.
    """

    __slots__ = ("_conn", "_pool", "_closed")

    def __init__(self, conn, pool: psycopg2.pool.ThreadedConnectionPool):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_closed", False)

    # --- delegate everything to the real connection ---
    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    # --- intercept close → putconn ---
    def close(self) -> None:
        if not self._closed:
            object.__setattr__(self, "_closed", True)
            try:
                self._pool.putconn(self._conn)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass

    # --- context-manager support (transaction semantics) ---
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._conn.rollback()
        else:
            self._conn.commit()
        self.close()
        return False


def init_pool() -> None:
    """Create the connection pool.  Call once during app lifespan startup."""
    global _pool
    if _pool is not None:
        return
    _pool = psycopg2.pool.ThreadedConnectionPool(
        POOL_MIN, POOL_MAX, **DB_CONFIG,
    )
    logger.info("DB connection pool created (min=%d, max=%d)", POOL_MIN, POOL_MAX)


def close_pool() -> None:
    """Shut down the connection pool.  Call during app lifespan shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("DB connection pool closed")


def get_conn():
    """Acquire a database connection.

    When the pool is active (FastAPI app), returns a ``_PooledConnection``
    whose ``close()`` returns the connection to the pool.

    When no pool exists (scripts), falls back to a plain
    ``psycopg2.connect()`` — ``close()`` destroys the connection as usual.
    """
    if _pool is not None:
        return _PooledConnection(_pool.getconn(), _pool)
    return psycopg2.connect(**DB_CONFIG)
