"""drop_snapshot_tables: retire the snapshot_* label-snapshot tables

Revision ID: 0013_drop_snapshot_tables
Revises: 0012_upstream_size_columns
Create Date: 2026-07-07

Audit decision 02-D1: the snapshot feature is unused, superseded by the
labelled mirror tables. The backend consumer (`_rebuild_snapshots` +
`POST /api/snapshots/refresh`) was deleted by stage 02; before this drop,
pg_stat_user_tables showed 0 rows / no inserts / no targeted reads on all
three tables (only the nightly pg_dump's simultaneous seq scans).

The 0001 baseline still creates the tables on a fresh DB and this revision
drops them again — accepted; baseline revisions are immutable.

downgrade() is intentionally a no-op: the tables were derivable from
`annotations`, and the code that rebuilt them no longer exists.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0013_drop_snapshot_tables"
down_revision: str | Sequence[str] | None = "0012_upstream_size_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS public.snapshot_patients, "
        "public.snapshot_studys, public.snapshot_seriess"
    )


def downgrade() -> None:
    pass  # intentional no-op — rebuild code removed in stage 02 (02-D1)
