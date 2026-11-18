"""Record authenticated SR saves and conservative series ownership summaries."""
from alembic import op

revision = "0030_sr_provenance"
down_revision = "0029_series_storage_source"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE sr_provenance (
        sop_uid text PRIMARY KEY, study_uid text NOT NULL, series_uid text NOT NULL,
        saved_by text NOT NULL, account_created_at timestamptz NOT NULL,
        saved_at timestamptz NOT NULL DEFAULT now(),
        confirmed boolean NOT NULL DEFAULT false
    )""")
    op.execute("CREATE INDEX sr_provenance_series ON sr_provenance(series_uid)")
    for table in ("image_series", "image_series_labelled"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN sr_saved_by text, ADD COLUMN sr_saved_at timestamptz")


def downgrade():
    for table in ("image_series", "image_series_labelled"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN sr_saved_by, DROP COLUMN sr_saved_at")
    op.execute("DROP TABLE sr_provenance")
