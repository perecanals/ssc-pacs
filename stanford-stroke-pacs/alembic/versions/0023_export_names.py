"""Require a display name for each table export."""
from alembic import op

revision = "0023_export_names"
down_revision = "0022_staff_role"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE explorer_exports ADD COLUMN name text")
    # Keep history on other deployments; the requested local reset is separate.
    op.execute("UPDATE explorer_exports SET name='Export ' || id::text")
    op.execute("ALTER TABLE explorer_exports ALTER COLUMN name SET NOT NULL")
    op.execute("ALTER TABLE explorer_exports ADD CONSTRAINT explorer_export_name "
               "CHECK (length(name) BETWEEN 1 AND 120 AND name ~ '[^[:space:]]')")


def downgrade():
    op.execute("ALTER TABLE explorer_exports DROP COLUMN name")
