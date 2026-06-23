"""label_value_options: fast lookup of known values per select-type label

Revision ID: 0009_label_value_options
Revises: 0008_users_allowed_datasets
Create Date: 2026-06-23

Introduces ``label_value_options`` — a small, indexed table holding the known
values (the controlled vocabulary) for each select-type label. It replaces the
slow ``SELECT DISTINCT value FROM annotations`` scan that previously fed the
inline-edit dropdown, and becomes the single source of truth that also feeds the
column filter (so values created inline show up there too).

Behaviour:
- Global vocabulary — values are not scoped per dataset; only the value strings
  are shared, never the underlying patient data.
- Persist values — a value stays once created, even if no annotation currently
  uses it. Pruning is an explicit admin action, never automatic.

Backfill (so existing values survive the cutover): seed from both the curated
``label_definitions.options`` and every value already observed in ``annotations``
for select-type labels.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0009_label_value_options"
down_revision: str | Sequence[str] | None = "0008_users_allowed_datasets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.label_value_options (
            label       text NOT NULL,
            value       text NOT NULL,
            created_by  text,
            created_at  timestamptz DEFAULT now(),
            PRIMARY KEY (label, value)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_label_value_options_label "
        "ON public.label_value_options (label)"
    )

    # Seed from curated options on select-type label definitions.
    op.execute(
        """
        INSERT INTO public.label_value_options (label, value, created_by)
        SELECT d.name, opt, d.created_by
        FROM public.label_definitions d
        CROSS JOIN LATERAL json_array_elements_text(d.options::json) AS opt
        WHERE d.datatype = 'select'
          AND d.options IS NOT NULL
          AND d.options <> ''
          AND opt IS NOT NULL
          AND opt <> ''
        ON CONFLICT (label, value) DO NOTHING
        """
    )

    # Seed from values already observed in annotations for select-type labels.
    op.execute(
        """
        INSERT INTO public.label_value_options (label, value)
        SELECT DISTINCT a.label, a.value
        FROM public.annotations a
        JOIN public.label_definitions d
          ON d.name = a.label AND d.datatype = 'select'
        WHERE a.value IS NOT NULL AND a.value <> ''
        ON CONFLICT (label, value) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.label_value_options")
