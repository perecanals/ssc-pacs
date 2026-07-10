"""series_cache_state: re-key cold-storage cache state to the series as unit

Revision ID: 0010_series_cache_state
Revises: 0009_label_value_options
Create Date: 2026-06-24

Cold-storage warming was study-level: ``cache_state`` was keyed by
``studyinstanceuid`` and ``warm_study()`` decompressed every series of a study
together. To let a rater decompress a single series on demand (without warming a
whole study), the **series becomes the single source of truth**: ``cache_state``
is replaced by ``series_cache_state`` (PK ``seriesinstanceuid``). Study- and
patient-level status are now derived aggregates over a study's series rows; the
``warm_study``/``warm_patient``/``evict_study`` Python entry points keep their
old behaviour as thin wrappers over the new ``warm_series`` primitive.

Migration ordering note (FK landmine): the legacy ``orthanc_resource_map`` table
carries an ``ON DELETE CASCADE`` foreign key onto ``cache_state(studyinstanceuid)``
(see ``0001_baseline``). It is dead — no code outside the baseline DDL reads or
writes it — so we drop it first; otherwise ``DROP TABLE cache_state`` would fail
on the dependency. The downgrade recreates both tables.

Backfill: each study's ``cache_state`` row is fanned out to one
``series_cache_state`` row per series of that study (inheriting the study's
status/timestamps). Disk-truth probes (``_is_series_dir_warm``) and
``scripts/cold_storage/rebuild_cache_state.py`` self-heal any drift afterward.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0010_series_cache_state"
down_revision: str | Sequence[str] | None = "0009_label_value_options"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop the dead orthanc_resource_map first — its FK references
    #    cache_state(studyinstanceuid), which would otherwise block the drop.
    op.execute("DROP TABLE IF EXISTS public.orthanc_resource_map CASCADE")

    # 2. Create the series-keyed table, mirroring cache_state's columns + states.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.series_cache_state (
            seriesinstanceuid  text PRIMARY KEY,
            status             text NOT NULL DEFAULT 'cold',
            cache_path         text,
            warmed_at          timestamptz,
            last_accessed_at   timestamptz,
            warming_started_at timestamptz,
            error_message      text,
            CONSTRAINT series_cache_state_status_check
                CHECK (status IN ('cold', 'warming', 'hot', 'error', 'queued'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_series_cache_state_status "
        "ON public.series_cache_state (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_series_cache_state_last_accessed "
        "ON public.series_cache_state (last_accessed_at)"
    )

    # 3. Backfill: fan each study's cache_state row out to its series. Guarded so
    #    a fresh install (no cache_state rows) is a no-op.
    op.execute(
        """
        INSERT INTO public.series_cache_state
            (seriesinstanceuid, status, warmed_at, last_accessed_at,
             warming_started_at, cache_path)
        SELECT s.seriesinstanceuid, cs.status, cs.warmed_at, cs.last_accessed_at,
               cs.warming_started_at, s.dicom_dir_path
        FROM public.cache_state cs
        JOIN public.image_series s ON s.studyinstanceuid = cs.studyinstanceuid
        ON CONFLICT (seriesinstanceuid) DO NOTHING
        """
    )

    # 4. Drop the old study-keyed table.
    op.execute("DROP TABLE IF EXISTS public.cache_state")


def downgrade() -> None:
    # Recreate cache_state (study-keyed) + indexes.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.cache_state (
            studyinstanceuid   text PRIMARY KEY,
            status             text NOT NULL DEFAULT 'cold',
            cache_path         text,
            warmed_at          timestamptz,
            last_accessed_at   timestamptz,
            warming_started_at timestamptz,
            error_message      text,
            CONSTRAINT cache_state_status_check
                CHECK (status IN ('cold', 'warming', 'hot', 'error', 'queued'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cache_state_last_accessed "
        "ON public.cache_state (last_accessed_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cache_state_status "
        "ON public.cache_state (status)"
    )

    # Aggregate series rows back to one study row (binary readiness: hot only if
    # all series hot; else warming > queued > error > cold). Best-effort.
    op.execute(
        """
        INSERT INTO public.cache_state
            (studyinstanceuid, status, warmed_at, last_accessed_at,
             warming_started_at, cache_path)
        SELECT st.studyinstanceuid,
               CASE
                 WHEN count(*) FILTER (WHERE scs.status = 'hot') = count(*) THEN 'hot'
                 WHEN count(*) FILTER (WHERE scs.status = 'warming') > 0 THEN 'warming'
                 WHEN count(*) FILTER (WHERE scs.status = 'queued') > 0 THEN 'queued'
                 WHEN count(*) FILTER (WHERE scs.status = 'error') > 0 THEN 'error'
                 ELSE 'cold'
               END,
               max(scs.warmed_at),
               max(scs.last_accessed_at),
               min(scs.warming_started_at),
               st.study_path
        FROM public.image_study st
        JOIN public.image_series s ON s.studyinstanceuid = st.studyinstanceuid
        JOIN public.series_cache_state scs ON scs.seriesinstanceuid = s.seriesinstanceuid
        GROUP BY st.studyinstanceuid, st.study_path
        ON CONFLICT (studyinstanceuid) DO NOTHING
        """
    )

    op.execute("DROP TABLE IF EXISTS public.series_cache_state")

    # Recreate the dead orthanc_resource_map table + FK/index (parity with baseline).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.orthanc_resource_map (
            orthanc_id        text PRIMARY KEY,
            resource_type     text NOT NULL,
            studyinstanceuid  text NOT NULL,
            seriesinstanceuid text,
            created_at        timestamptz DEFAULT now(),
            CONSTRAINT orthanc_resource_map_resource_type_check
                CHECK (resource_type IN ('study', 'series', 'instance')),
            CONSTRAINT orthanc_resource_map_studyinstanceuid_fkey
                FOREIGN KEY (studyinstanceuid)
                REFERENCES public.cache_state(studyinstanceuid) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orm_study "
        "ON public.orthanc_resource_map (studyinstanceuid)"
    )
