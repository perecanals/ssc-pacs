"""upstream_size_columns: bootstrap image_series/image_study size columns

Revision ID: 0012_upstream_size_columns
Revises: 0011_image_table_indexes
Create Date: 2026-07-07

The storage-footprint columns (decimal MB; see backfill_storage_sizes.py)
reached production via runtime `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
in the ingestion executor and the backfill script, so a fresh DB built by
`alembic upgrade head` lacked them. This revision makes Alembic the
canonical fresh-install source; the runtime ALTERs remain as idempotent
guards only, and `ssc-sql-db/create_image_{series,study}.sql` mirror it.

No-op on production (all four columns exist there — verified 2026-07-07).
ADD COLUMN IF NOT EXISTS takes an ACCESS EXCLUSIVE lock but returns
instantly when the column exists; safe at app-startup application.

downgrade() is intentionally a no-op: the columns hold upstream-owned,
backfilled data that a schema rollback must not destroy.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0012_upstream_size_columns"
down_revision: str | Sequence[str] | None = "0011_image_table_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.image_series "
        "ADD COLUMN IF NOT EXISTS compressed_size_mb double precision"
    )
    op.execute(
        "ALTER TABLE public.image_series "
        "ADD COLUMN IF NOT EXISTS decompressed_size_mb double precision"
    )
    op.execute(
        "ALTER TABLE public.image_study "
        "ADD COLUMN IF NOT EXISTS compressed_size_mb double precision"
    )
    op.execute(
        "ALTER TABLE public.image_study "
        "ADD COLUMN IF NOT EXISTS decompressed_size_mb double precision"
    )


def downgrade() -> None:
    pass  # intentional no-op — dropping would destroy backfilled size data
