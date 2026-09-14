"""Staff may export data within their existing dataset grants."""
from alembic import op

revision = "0022_staff_role"
down_revision = "0021_data_explorer"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN is_staff boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_distinct_roles CHECK (NOT (is_admin AND is_staff))")


def downgrade():
    op.execute("ALTER TABLE users DROP CONSTRAINT users_distinct_roles")
    op.execute("ALTER TABLE users DROP COLUMN is_staff")
