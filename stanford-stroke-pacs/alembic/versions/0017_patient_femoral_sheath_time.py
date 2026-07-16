"""patient femoral_sheath_time: durable clinical puncture time

Revision ID: 0017_patient_femoral_sheath_time
Revises: 0016_study_episode
Create Date: 2026-07-15

Adds `public.patient.femoral_sheath_time` (text) — the arterial-puncture
(femoral sheath) time, a clinical value that lives on
`lvo_clinical_data.femoral_sheath_time` (keyed by `study_id` = patient_id) and
is present only for the CRISP2/LVO cohort.

`patient` is an **upstream-owned** table (`alembic/env.py:UPSTREAM_TABLES`,
excluded from --autogenerate); its real production DDL lives in
`ssc-sql-db/create_patient.sql`, which must gain the same column. This revision
mirrors the column into Alembic so fresh installs and the pytest scratch DB
match production.

The column is a durable copy populated **prospectively** by ingestion
(`_upsert_patient`, sourced via LEFT JOIN lvo_clinical_data), exactly like the
imaging-derived `stroke_date`. The web app surfaces
`COALESCE(c.femoral_sheath_time, p.femoral_sheath_time)` so existing patients
display the live clinical value immediately; the durable copy fills in as
patients are re-ingested. There is intentionally no historical bulk backfill.

Nullable ADD — metadata-only, instant even on the populated production DB.

downgrade() is intentionally a no-op: the column holds upstream-owned data a
schema rollback must not destroy (same rationale as 0012_upstream_size_columns).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0017_patient_femoral_sheath_time"
down_revision: str | Sequence[str] | None = "0016_study_episode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.patient "
        "ADD COLUMN IF NOT EXISTS femoral_sheath_time text"
    )


def downgrade() -> None:
    pass  # intentional no-op — dropping would destroy upstream-owned data
