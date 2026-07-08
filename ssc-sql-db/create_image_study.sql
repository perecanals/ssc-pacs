-- Create table in the stanford-stroke database.
-- Derived from public.stanford_ctas_soren_dicom_series in stanford_data.
-- This script only defines the table; it does not load any data.

\connect "stanford-stroke"

CREATE TABLE IF NOT EXISTS public.image_study (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    study_type text,
    studydescription text,
    studyinstanceuid text,
    study_path text,
    protocolname text,
    manufacturer text,
    -- Rollups of image_series sizes (decimal MB); stamped only when every
    -- child series has sizes (backfill_storage_sizes.py / ingestion).
    compressed_size_mb double precision,
    decompressed_size_mb double precision,
    CONSTRAINT image_study_pkey PRIMARY KEY (studyinstanceuid)
);

-- Join/filter hot path (mirrors Alembic revision 0011).
CREATE INDEX IF NOT EXISTS idx_image_study_patient_id
    ON public.image_study USING btree (patient_id);
