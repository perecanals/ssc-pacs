"""Independent Data Explorer reports and export audit/job state."""
from alembic import op

revision = "0021_data_explorer"
down_revision = "0020_rename_clinical_data"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE explorer_reports (
            id uuid PRIMARY KEY, name text NOT NULL,
            configuration jsonb NOT NULL, created_by text NOT NULL, updated_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE explorer_exports (
            id uuid PRIMARY KEY, username text NOT NULL, configuration jsonb NOT NULL,
            sql text NOT NULL, parameters jsonb NOT NULL, format text NOT NULL CHECK (format IN ('csv','xlsx')),
            status text NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled','expired')),
            created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz,
            expires_at timestamptz, row_count bigint NOT NULL DEFAULT 0, file_size bigint,
            error text, cancel_requested boolean NOT NULL DEFAULT false
        );
        CREATE INDEX explorer_exports_created ON explorer_exports(created_at DESC);
        CREATE TABLE explorer_downloads (
            id bigserial PRIMARY KEY, export_id uuid NOT NULL REFERENCES explorer_exports(id),
            username text NOT NULL, requested_at timestamptz NOT NULL DEFAULT now()
        );
        REVOKE ALL ON explorer_reports, explorer_exports, explorer_downloads FROM PUBLIC;
    """)
    # Existing collaborator roles may have default SELECT grants. These new
    # module tables hold query filters and must not inherit those grants.
    op.execute("""
        DO $$ DECLARE item record; BEGIN
          FOR item IN SELECT DISTINCT grantee FROM information_schema.role_table_grants
            WHERE table_schema='public' AND table_name IN ('explorer_reports','explorer_exports','explorer_downloads')
              AND grantee <> current_user AND grantee <> 'PUBLIC'
          LOOP
            EXECUTE format('REVOKE ALL ON explorer_reports, explorer_exports, explorer_downloads FROM %I', item.grantee);
          END LOOP;
        END $$;
    """)


def downgrade():
    op.execute("DROP TABLE explorer_downloads, explorer_exports, explorer_reports")
