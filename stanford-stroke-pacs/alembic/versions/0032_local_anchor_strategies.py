"""Installation-defined clinical anchor strategy names."""

from alembic import op

revision = "0032_local_anchor_strategies"
down_revision = "0031_dataset_autolabels"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE dataset DROP CONSTRAINT dataset_timepoint_strategy_check")
    op.execute("""ALTER TABLE dataset ADD CONSTRAINT dataset_timepoint_strategy_check
               CHECK (timepoint_strategy ~ '^[a-z][a-z0-9_]*$')""")


def downgrade():
    op.execute("ALTER TABLE dataset DROP CONSTRAINT dataset_timepoint_strategy_check")
    op.execute("""ALTER TABLE dataset ADD CONSTRAINT dataset_timepoint_strategy_check
               CHECK (timepoint_strategy IN ('crisp2_puncture'))""")
