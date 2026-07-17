"""drop patient.femoral_sheath_time: clinical variables belong in annotations

Revision ID: 0018_drop_femoral_sheath_time
Revises: 0017_patient_femoral_sheath_time
Create Date: 2026-07-16

Withdraws `public.patient.femoral_sheath_time`, added one revision ago by
`0017`. That design does not generalize: a clinical variable per column means a
new Alembic revision on an **upstream-owned** table, a new COALESCE expression,
a new frontend column, and a new mirror into the out-of-band
`ssc-sql-db/create_patient.sql` — every single time. The annotation label system
already carries arbitrary named variables at patient level with no schema churn,
so the arterial-puncture time now lives as the patient-level `femoral_sheath_time`
label (datatype `text`, instrument `redcap_lvo_clinical`), bulk-loaded from
`lvo_clinical_data` via `scripts/admin/bulk_set_label_values.py`.

Dropping is **lossless**: the column was populated prospectively by ingestion
with no historical backfill, and no re-ingest ran between `0017` and this
revision, so it was 0-of-1854 populated in production when it was dropped. The
values it *would* have held were loaded into the label first (874 patients — the
`lvo_clinical_data` rows that join to a real `patient`).

`0017` is deliberately **superseded, not edited or deleted**: it is released
history (tagged v1.10, v1.11, v1.12) and production is stamped at it, so removing
it would strand any DB at that revision with "Can't locate revision 0017".

Note this also re-aligns Alembic with `ssc-sql-db/create_patient.sql`, which
never actually received the mirrored column that `0017` called for — a DB
provisioned from that DDL would have failed the old `_upsert_patient` INSERT.

Unlike `0017`'s deliberately no-op downgrade, this downgrade is a true inverse.
`0017` refused to drop because the column was upstream-owned and might hold data;
here the column is known-empty and the re-added column is nullable, so the
round-trip destroys nothing. Both directions are metadata-only and instant.

(Revision id is `..._drop_femoral_sheath_time`, not `..._drop_patient_femoral_
sheath_time`: `alembic_version.version_num` is `varchar(32)` and the longer name
is 37 chars. `0017` fit at exactly 32.)
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0018_drop_femoral_sheath_time"
down_revision: str | Sequence[str] | None = "0017_patient_femoral_sheath_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.patient DROP COLUMN IF EXISTS femoral_sheath_time"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.patient "
        "ADD COLUMN IF NOT EXISTS femoral_sheath_time text"
    )
