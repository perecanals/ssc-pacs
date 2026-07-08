"""image_table_indexes: relational indexes for the list/join hot paths

Revision ID: 0011_image_table_indexes
Revises: 0010_series_cache_state
Create Date: 2026-07-07

image_series(studyinstanceuid), image_series(patient_id) and
image_study(patient_id) had no indexes, so every list page and
cache-status poll seq-scanned ~103 MB (measured 1.09-1.35 s per
/api/studies|/api/patients page, 65-96 ms per cache-status batch).

Plain CREATE INDEX (not CONCURRENTLY — Alembic runs in a transaction);
takes a SHARE lock for ~1-3 s per index on the production tables, so
apply during a quiet window with no ingestion running.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011_image_table_indexes"
down_revision: str | Sequence[str] | None = "0010_series_cache_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_series_studyinstanceuid "
        "ON public.image_series USING btree (studyinstanceuid)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_series_patient_id "
        "ON public.image_series USING btree (patient_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_study_patient_id "
        "ON public.image_study USING btree (patient_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_image_study_patient_id")
    op.execute("DROP INDEX IF EXISTS public.idx_image_series_patient_id")
    op.execute("DROP INDEX IF EXISTS public.idx_image_series_studyinstanceuid")
