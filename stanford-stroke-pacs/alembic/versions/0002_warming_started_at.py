"""warming_started_at: timestamp for cold-storage warm-watchdog

Revision ID: 0002_warming_started_at
Revises: 0001_baseline
Create Date: 2026-04-15

Adds `cache_state.warming_started_at TIMESTAMPTZ`. The cache manager
sets it whenever a row transitions to `status='warming'`. The watchdog
in `warm_study()` uses it to detect rows stuck in `warming` after a
crash and treats them as cold once `WARMING_TIMEOUT_MINUTES` elapses.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002_warming_started_at"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.cache_state "
        "ADD COLUMN IF NOT EXISTS warming_started_at timestamp with time zone"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.cache_state DROP COLUMN IF EXISTS warming_started_at")
