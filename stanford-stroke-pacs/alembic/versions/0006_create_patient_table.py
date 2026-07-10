"""create patient table: patient-level registry

Revision ID: 0006_create_patient_table
Revises: 0005_users_must_change_password
Create Date: 2026-06-04

Adds `public.patient`, the comprehensive one-row-per-patient roster. It is an
**upstream-owned** table (like image_study/image_series/lvo_clinical_data):
created out-of-band in production via `ssc-sql-db/create_patient.sql` and
populated by the ingest pipeline, but reproduced here with
`CREATE TABLE IF NOT EXISTS` so fresh installs and the pytest scratch DB (built
purely by `alembic upgrade head`) have it. Idempotent — no-ops where the .sql
script already ran. `patient` is excluded from --autogenerate scope in
alembic/env.py so future revisions never draft a DROP for it.

Also drops the now-obsolete `lvo_clinical_data_labelled`: the patient-level
labelled mirror is rebuilt as `patient_labelled` by labelled_table_sync (its
LEVEL_CONFIGS["patient"] now points at the `patient` table). The baseline
created `lvo_clinical_data_labelled`, so dropping it here removes it on fresh
installs (created by baseline, then dropped) and on production (dropped on the
next `upgrade head`). Downgrade does not recreate it — it is runtime-rebuildable
and only meaningful alongside the old LEVEL_CONFIGS code, which a downgrade does
not revert.

See plan: patient table replaces lvo_clinical_data as the patient-level spine.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0006_create_patient_table"
down_revision: str | Sequence[str] | None = "0005_users_must_change_password"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS public.patient (
    patient_id text NOT NULL,
    stroke_date timestamp without time zone,
    import_id integer,
    import_label text,
    dataset text[] NOT NULL DEFAULT '{}',
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT patient_pkey PRIMARY KEY (patient_id)
);
"""


def upgrade() -> None:
    op.execute(CREATE_SQL)
    op.execute("DROP TABLE IF EXISTS public.lvo_clinical_data_labelled CASCADE")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.patient CASCADE")
