"""label_definitions.instrument: free-text grouping for annotation variables

Revision ID: 0004_label_def_instrument
Revises: 0003_annotations_history
Create Date: 2026-05-13

Adds a nullable ``instrument`` text column to ``label_definitions`` so
variables can be grouped (e.g. "Functional outcome", "Imaging quality")
in the Navigator DataTable's ColumnSelector. Existing labels start with
``instrument = NULL`` ("Unassigned").
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0004_label_def_instrument"
down_revision: str | Sequence[str] | None = "0003_annotations_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.label_definitions "
        "ADD COLUMN IF NOT EXISTS instrument text"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_label_definitions_instrument "
        "ON public.label_definitions (instrument)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_label_definitions_instrument")
    op.execute(
        "ALTER TABLE public.label_definitions DROP COLUMN IF EXISTS instrument"
    )
