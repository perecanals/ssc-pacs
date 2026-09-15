"""Rename Data Exports metadata without rewriting reports, jobs or audit rows."""
from alembic import op

revision = "0024_data_exports_naming"
down_revision = "0023_export_names"
branch_labels = None
depends_on = None

TABLES = (
    ("explorer_reports", "data_exports_reports"),
    ("explorer_exports", "data_exports_jobs"),
    ("explorer_downloads", "data_exports_downloads"),
)
CONSTRAINTS = (
    ("data_exports_reports", "explorer_reports_pkey", "data_exports_reports_pkey"),
    ("data_exports_jobs", "explorer_exports_pkey", "data_exports_jobs_pkey"),
    ("data_exports_jobs", "explorer_exports_format_check", "data_exports_jobs_format_check"),
    ("data_exports_jobs", "explorer_exports_status_check", "data_exports_jobs_status_check"),
    ("data_exports_jobs", "explorer_export_name", "data_exports_job_name"),
    ("data_exports_downloads", "explorer_downloads_pkey", "data_exports_downloads_pkey"),
    ("data_exports_downloads", "explorer_downloads_export_id_fkey", "data_exports_downloads_export_id_fkey"),
)


def require_stopped_worker():
    if not op.get_bind().exec_driver_sql("SELECT pg_try_advisory_xact_lock(782341, 21)").scalar():
        raise RuntimeError("Stop the Data Exports worker before renaming its metadata tables")


def upgrade():
    require_stopped_worker()
    for old, new in TABLES:
        op.rename_table(old, new)
    for table, old, new in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")
    op.execute("ALTER INDEX explorer_exports_created RENAME TO data_exports_jobs_created")
    op.execute("ALTER SEQUENCE explorer_downloads_id_seq RENAME TO data_exports_downloads_id_seq")


def downgrade():
    require_stopped_worker()
    op.execute("ALTER SEQUENCE data_exports_downloads_id_seq RENAME TO explorer_downloads_id_seq")
    op.execute("ALTER INDEX data_exports_jobs_created RENAME TO explorer_exports_created")
    for table, old, new in reversed(CONSTRAINTS):
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
    for old, new in reversed(TABLES):
        op.rename_table(new, old)
