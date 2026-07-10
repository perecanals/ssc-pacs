"""users.allowed_datasets: per-user dataset (cohort) access grants

Revision ID: 0008_users_allowed_datasets
Revises: 0007_cache_state_queued_status
Create Date: 2026-06-12

Adds ``allowed_datasets TEXT[] NOT NULL DEFAULT '{}'`` to ``users``: the
cohort tags (values of ``patient.dataset``) a non-admin user is allowed to
see. Semantics are deny-by-default — an empty array means the user sees no
patient data until an admin grants datasets (via the /admin page or
``scripts/admin/manage_users.py set-datasets``). Admins (``is_admin``)
bypass the scope entirely, regardless of this column.

No backfill on purpose: existing non-admin users must be granted datasets
explicitly after deploying this revision.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0008_users_allowed_datasets"
down_revision: str | Sequence[str] | None = "0007_cache_state_queued_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS allowed_datasets TEXT[] NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS allowed_datasets")
