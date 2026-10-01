"""Persist the distinct modalities represented by each study's child series."""

from alembic import op

revision = "0025_study_modalities"
down_revision = "0024_data_exports_naming"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.image_study ADD COLUMN modalities text[]")
    # Frozen here rather than importing application code: historical migrations
    # must retain their behavior when the live rollup helper changes.
    op.execute(
        """
        UPDATE public.image_study st SET modalities = agg.modalities
        FROM (
            SELECT studyinstanceuid,
                   array_agg(DISTINCT UPPER(BTRIM(modality)) ORDER BY UPPER(BTRIM(modality)))
                       FILTER (WHERE NULLIF(BTRIM(modality), '') IS NOT NULL) AS modalities
            FROM public.image_series
            GROUP BY studyinstanceuid
        ) agg
        WHERE st.studyinstanceuid = agg.studyinstanceuid
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF to_regclass('public.image_study_labelled') IS NOT NULL THEN
                ALTER TABLE public.image_study_labelled ADD COLUMN modalities text[];
                UPDATE public.image_study_labelled mirror SET modalities = st.modalities
                FROM public.image_study st
                WHERE mirror.studyinstanceuid = st.studyinstanceuid;
            END IF;
        END $$
        """
    )


def downgrade():
    op.execute("ALTER TABLE IF EXISTS public.image_study_labelled DROP COLUMN IF EXISTS modalities")
    op.execute("ALTER TABLE public.image_study DROP COLUMN modalities")
