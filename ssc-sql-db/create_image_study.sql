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
    CONSTRAINT image_study_pkey PRIMARY KEY (studyinstanceuid)
);
