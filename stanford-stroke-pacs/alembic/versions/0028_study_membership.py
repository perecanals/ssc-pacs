"""Per-enrollment imaging visibility: linking is identity, not shared imaging.

Until now the enrollments of one person (same ``subject_id``) shared *all* of
that person's imaging. Datasets re-exported with re-generated UIDs then showed
every study twice under each id. From here on:

  * ``subject_id`` only records that enrollments are the same person;
  * an enrollment sees the studies it owns (``image_study.patient_key``) plus
    those it is a **member** of — a ``study_member`` row, written when a linked
    enrollment ingests a StudyInstanceUID another enrollment of the same person
    already owns (the very same object);
  * ``study_visibility`` (owners UNION ALL members) is the one definition of
    which enrollments see a study; every reader goes through it;
  * series ranks and ``patient.stroke_date`` follow the enrollment: ranks are a
    window over the owner's series, stroke_date the earliest visible study.
    (Timepoints are recomputed per owner by scripts/admin/recompute_timepoints.py.)

No backfill: earlier same-UID re-sends were not recorded, and the only linked
people at this revision carry distinct UIDs.
"""

from alembic import op

revision = "0028_study_membership"
down_revision = "0027_dataset_clinical_tables"
branch_labels = None
depends_on = None

# Same order as series_classification.ASSIGN_RANKS_SQL, partitioned by owner.
# strpos(upper()) == ILIKE '%X%' without '%' (kept out of migration SQL).
_RANKS_BY_OWNER = """
WITH ranked AS (
    SELECT s.seriesinstanceuid,
           row_number() OVER (
               PARTITION BY s.patient_key, s.series_type
               ORDER BY
                   CASE WHEN s.series_type = 'CTA'  THEN t.slice_thickness END ASC  NULLS LAST,
                   CASE WHEN s.series_type = 'NCCT' THEN t.slice_thickness END DESC NULLS LAST,
                   CASE
                       WHEN strpos(upper(t.image_type), 'SECOND') > 0 THEN 2
                       WHEN strpos(upper(t.image_type), 'DERIV')  > 0 THEN 3
                       WHEN strpos(upper(t.image_type), 'PRIMA')  > 0
                         OR strpos(upper(t.image_type), 'ORIG')   > 0 THEN 1
                       ELSE 4
                   END,
                   s.acquisitiondatetime ASC NULLS LAST,
                   s.seriesinstanceuid
           ) AS rnk
    FROM image_series s
    JOIN series_dicom_tags t USING (seriesinstanceuid)
    WHERE s.series_type IS NOT NULL
)
UPDATE image_series s
   SET series_type_rank = r.rnk,
       series_label     = s.series_type || '_' || r.rnk
  FROM ranked r
 WHERE r.seriesinstanceuid = s.seriesinstanceuid
   AND s.series_type_rank IS DISTINCT FROM r.rnk
"""


def upgrade():
    op.execute(
        """
        CREATE TABLE public.study_member (
            studyinstanceuid text NOT NULL
                REFERENCES public.image_study (studyinstanceuid) ON DELETE CASCADE,
            patient_key text NOT NULL
                REFERENCES public.patient (patient_key)
                ON DELETE CASCADE ON UPDATE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (studyinstanceuid, patient_key)
        );
        CREATE INDEX idx_study_member_patient_key ON public.study_member (patient_key);

        CREATE VIEW public.study_visibility AS
            SELECT st.studyinstanceuid, st.patient_key, true AS is_owner
              FROM public.image_study st
             WHERE st.patient_key IS NOT NULL
            UNION ALL
            SELECT m.studyinstanceuid, m.patient_key, false
              FROM public.study_member m;
        """
    )
    # Whoever may read image_study may read who else sees it.
    op.execute(
        """
        DO $$ DECLARE r record; BEGIN
            FOR r IN
                SELECT DISTINCT a.grantee::regrole::text AS grantee
                FROM pg_class c, aclexplode(c.relacl) a
                WHERE c.oid = 'public.image_study'::regclass
                  AND a.privilege_type = 'SELECT'
                  AND a.grantee <> 0 AND a.grantee <> c.relowner
            LOOP
                EXECUTE format('GRANT SELECT ON public.study_member, public.study_visibility TO %s',
                               r.grantee);
            END LOOP;
        END $$
        """
    )
    op.execute(_RANKS_BY_OWNER)
    op.execute(
        """
        UPDATE public.patient p
           SET stroke_date = v.first_study, updated_at = now()
          FROM (SELECT q.patient_key,
                       (SELECT MIN(st.acquisitiondatetime)
                          FROM public.study_visibility sv
                          JOIN public.image_study st USING (studyinstanceuid)
                         WHERE sv.patient_key = q.patient_key) AS first_study
                  FROM public.patient q) v
         WHERE p.patient_key = v.patient_key
           AND p.stroke_date IS DISTINCT FROM v.first_study
        """
    )


def downgrade():
    # Ranks/stroke_date stay per enrollment; the next ingestion or
    # refresh re-derives them under the old (per-subject) code.
    op.execute(
        """
        DROP VIEW public.study_visibility;
        DROP TABLE public.study_member;
        """
    )
