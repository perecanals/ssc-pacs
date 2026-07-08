"""annotation_index_cleanup: composite (level,label) index; drop redundant ones

Revision ID: 0014_annotation_index_cleanup
Revises: 0013_drop_snapshot_tables
Create Date: 2026-07-07

Audit decision 03-D5: `build_label_filter_sql`'s inner subquery filters
`WHERE level = '<x>' AND label = %s` — one composite (level, label) index
serves it, and level-only lookups via its prefix. The two single-column
indexes it replaces had 94 / 74 lifetime scans on a 10k-row table —
coherence, not rescue.

`idx_label_value_options_label` is a strict prefix of the PK
`(label, value)` and had 0 scans since creation (PK: 9,193) — dropped.

Kept deliberately: idx_annotations_series (187k scans — level-less
GET /api/series/{uid}/annotations), idx_annotations_patient/_study
(same level-less pattern), and the three partial-unique upsert targets.

Lock impact: trivial (annotations ~10k rows / 5.5 MB; SHARE lock for a
sub-second index build).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014_annotation_index_cleanup"
down_revision: str | Sequence[str] | None = "0013_drop_snapshot_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_annotations_level_label "
        "ON public.annotations USING btree (level, label)"
    )
    op.execute("DROP INDEX IF EXISTS public.idx_annotations_label")
    op.execute("DROP INDEX IF EXISTS public.idx_annotations_level")
    op.execute("DROP INDEX IF EXISTS public.idx_label_value_options_label")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_label_value_options_label "
        "ON public.label_value_options USING btree (label)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_annotations_level "
        "ON public.annotations USING btree (level)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_annotations_label "
        "ON public.annotations USING btree (label)"
    )
    op.execute("DROP INDEX IF EXISTS public.idx_annotations_level_label")
