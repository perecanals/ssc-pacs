"""cache_state: allow 'queued' status for cold-storage warm queue

Revision ID: 0007_cache_state_queued_status
Revises: 0006_create_patient_table
Create Date: 2026-06-09

Widens the `cache_state_status_check` CHECK constraint to permit
`status='queued'`. A warm is now persisted as 'queued' the moment it is
submitted to the in-process warm executor (before any worker starts), so
the "Queued" badge survives page reloads and is visible to other users —
the executor queue itself is not observable. `warm_study()` flips
'queued'->'warming' when a worker actually begins extraction, and
`reap_stale_warming()` ages out orphaned 'queued' rows (e.g. if the app
restarts and drops its in-memory queue) back to 'cold'.

See workstream `maintenance/workstreams/05-cold-storage-robustness.md`.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0007_cache_state_queued_status"
down_revision: str | Sequence[str] | None = "0006_create_patient_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "ARRAY['cold'::text, 'warming'::text, 'hot'::text, 'error'::text]"
_NEW = "ARRAY['cold'::text, 'queued'::text, 'warming'::text, 'hot'::text, 'error'::text]"


def upgrade() -> None:
    op.execute("ALTER TABLE public.cache_state DROP CONSTRAINT IF EXISTS cache_state_status_check")
    op.execute(
        "ALTER TABLE public.cache_state ADD CONSTRAINT cache_state_status_check "
        f"CHECK ((status = ANY ({_NEW})))"
    )


def downgrade() -> None:
    # Collapse any lingering 'queued' rows before narrowing the constraint.
    op.execute("UPDATE public.cache_state SET status = 'cold' WHERE status = 'queued'")
    op.execute("ALTER TABLE public.cache_state DROP CONSTRAINT IF EXISTS cache_state_status_check")
    op.execute(
        "ALTER TABLE public.cache_state ADD CONSTRAINT cache_state_status_check "
        f"CHECK ((status = ANY ({_OLD})))"
    )
