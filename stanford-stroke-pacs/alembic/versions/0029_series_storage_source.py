"""Distinguish Orthanc-owned reports from filesystem imaging."""
from alembic import op

revision = "0029_series_storage_source"
down_revision = "0028_study_membership"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE image_series ADD COLUMN storage_source text NOT NULL DEFAULT 'filesystem' CHECK (storage_source IN ('filesystem', 'orthanc'))")
    op.execute("ALTER TABLE image_series_labelled ADD COLUMN storage_source text")
    op.execute("UPDATE image_series_labelled SET storage_source = 'filesystem'")


def downgrade():
    op.execute("ALTER TABLE image_series_labelled DROP COLUMN storage_source")
    op.execute("ALTER TABLE image_series DROP COLUMN storage_source")
