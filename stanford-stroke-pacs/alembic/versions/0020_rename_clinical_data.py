"""Generalize the clinical side-table: lvo_clinical_data -> clinical_data

Revision ID: 0020_rename_clinical_data
Revises: 0019_label_edit_policy
Create Date: 2026-07-17

The table was never LVO-specific in function, only in origin (the SSC REDCap
export that first populated it). The rename makes the name match the role: an
optional per-patient clinical side-table, joined only to prefer its clinical
episode date over the imaging-derived ``patient.stroke_date``.

The table is **optional** — a deployment may not have it at all — so both
directions use ``ALTER TABLE IF EXISTS`` and no-op cleanly when it is absent
(available on every supported PostgreSQL; the floor is 16). Fresh installs
still create the old name in ``0001_baseline`` and rename here: shipped
revisions are never edited.

Deliberately untouched by the rename:

  - ``RENAME TO`` carries data, indexes, and constraints along, but their
    *names* keep the historical ``lvo_clinical_data_*`` prefixes — cosmetic,
    and cheaper than chasing every index rename forever.
  - The patient-id column keeps its historical name ``study_id``.

If a table named ``clinical_data`` already exists (hand-created before this
revision ran), the rename fails loudly with "relation already exists" — the
right outcome: an operator must reconcile the two by hand; silently preferring
either one would hide data.

Revision id is 24 chars: ``alembic_version.version_num`` is ``varchar(32)``
and a longer id fails at runtime, not at import.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0020_rename_clinical_data"
down_revision: str | Sequence[str] | None = "0019_label_edit_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE IF EXISTS public.lvo_clinical_data RENAME TO clinical_data"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE IF EXISTS public.clinical_data RENAME TO lvo_clinical_data"
    )
