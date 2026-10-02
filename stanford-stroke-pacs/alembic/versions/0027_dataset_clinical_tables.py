"""Per-dataset clinical tables, registered on the `dataset` row.

Clinical data belongs to one dataset. Instead of one global ``clinical_data``
table plus config.toml keys saying which column is the episode date and which
dataset the table belongs to, each dataset row now names its own clinical table:

  * ``clinical_table`` — ``<slug with - as _>_clinical_data`` by convention (the
    suffix is enforced: Alembic autogenerate skips such tables as upstream);
  * ``clinical_id_column`` — the column holding the dataset's patient id;
  * ``clinical_date_column`` — the episode-date column (patient tab);
  * ``timepoint_strategy`` — how timepoints anchor: NULL = each episode's
    thrombectomy study only; ``crisp2_puncture`` = the CRISP2 puncture columns
    of that dataset's clinical table first. Future per-dataset strategies are
    new values.

Schema only. Which dataset an existing ``clinical_data`` belongs to is not
knowable here; adopt it with ``scripts/admin/manage_datasets.py import-clinical
--from-table clinical_data`` (which also leaves a compatibility view).
"""

from alembic import op

revision = "0027_dataset_clinical_tables"
down_revision = "0026_patient_identity"
branch_labels = None
depends_on = None

_IDENT = "^[a-z_][a-z0-9_]*$"


def upgrade():
    op.execute(
        f"""
        ALTER TABLE public.dataset
            ADD COLUMN clinical_table text
                CHECK (clinical_table ~ '^[a-z][a-z0-9_]*_clinical_data$'),
            ADD COLUMN clinical_id_column text CHECK (clinical_id_column ~ '{_IDENT}'),
            ADD COLUMN clinical_date_column text CHECK (clinical_date_column ~ '{_IDENT}'),
            ADD COLUMN timepoint_strategy text
                CHECK (timepoint_strategy IN ('crisp2_puncture')),
            ADD CONSTRAINT dataset_clinical_all_or_none CHECK (
                (clinical_table IS NULL) = (clinical_id_column IS NULL)
                AND (clinical_table IS NULL) = (clinical_date_column IS NULL)),
            ADD CONSTRAINT dataset_clinical_table_key UNIQUE (clinical_table)
        """
    )


def downgrade():
    op.execute(
        """
        ALTER TABLE public.dataset
            DROP CONSTRAINT dataset_clinical_table_key,
            DROP CONSTRAINT dataset_clinical_all_or_none,
            DROP COLUMN timepoint_strategy,
            DROP COLUMN clinical_date_column,
            DROP COLUMN clinical_id_column,
            DROP COLUMN clinical_table
        """
    )
