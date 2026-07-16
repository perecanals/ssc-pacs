"""study episode + acquisition-datetime source provenance

Revision ID: 0016_study_episode
Revises: 0015_series_classification
Create Date: 2026-07-15

Follow-up to 0015. Two additions, both to make the machine timepoint honest for
the whole corpus rather than only the single-episode LVO majority:

  * `image_study.episode` (integer) — a handful of patients (the `11-*` cohort,
    ~15 in all) carry studies from two distinct stroke episodes months apart. A
    single per-patient clinical anchor scored one episode's imaging against the
    OTHER episode's puncture (hours_to_event in the tens of thousands, a whole
    episode mislabelled BL). Studies are now split into episodes by a large
    inter-study gap and each episode anchored on its own puncture, or failing
    that its own thrombectomy study. `episode` is 1-based per patient, NULL for a
    study with no acquisition clock.

  * `acquisitiondatetime_source` (text) on `image_study` and `image_series` —
    which DICOM clock supplied `acquisitiondatetime` (`acquisition` | `study`).
    ~16% of series carry no acquisition tag and fall to StudyDate; recording the
    source makes a timepoint built on the study encounter clock (rather than a
    real acquisition timestamp) distinguishable. Content/Series dates are
    deliberately not used — for derived series they are the post-processing day,
    which mis-dates the study.

These are upstream tables (`alembic/env.py:UPSTREAM_TABLES`), mirrored in Alembic
so fresh installs and the pytest scratch DB match production. All columns are
nullable ADDs — metadata-only, instant even on the populated DB. The values are
(re)computed by ingestion and `scripts/admin/recompute_timepoints.py`; this
migration only creates the columns.

## Boundary

Machine-owned, independent of the human annotation labels (`label_timepoint_*`).
Neither may be derived from the other.

downgrade() drops the columns; every value is re-derivable from series_dicom_tags.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0016_study_episode"
down_revision: str | Sequence[str] | None = "0015_series_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.image_study ADD COLUMN IF NOT EXISTS episode integer"
    )
    op.execute(
        "ALTER TABLE public.image_study "
        "ADD COLUMN IF NOT EXISTS acquisitiondatetime_source text"
    )
    op.execute(
        "ALTER TABLE public.image_series "
        "ADD COLUMN IF NOT EXISTS acquisitiondatetime_source text"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_study_episode "
        "ON public.image_study USING btree (episode)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_image_study_episode")
    op.execute("ALTER TABLE public.image_series DROP COLUMN IF EXISTS acquisitiondatetime_source")
    op.execute("ALTER TABLE public.image_study DROP COLUMN IF EXISTS acquisitiondatetime_source")
    op.execute("ALTER TABLE public.image_study DROP COLUMN IF EXISTS episode")
