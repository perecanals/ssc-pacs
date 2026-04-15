-- Create table in the stanford-stroke database.
-- Derived from public.stanford_ctas_soren_dicom_series in stanford_data.
-- This script only defines the table; it does not load any data.

\connect "stanford-stroke"

CREATE TABLE IF NOT EXISTS public.image_series (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    studydescription text,
    seriesdescription text,
    series_type text,
    modality text,
    studyinstanceuid text,
    seriesinstanceuid text NOT NULL,
    dicom_dir_path text,
    dicom_archive_path text,
    nifti_path text,
    protocolname text,
    seriesnumber integer,
    instancenumber integer,
    manufacturer text,
    pixelspacing double precision[],
    slicethickness double precision,
    imageshape integer[],
    number_of_slices integer,
    scanaxialcoverage_mm double precision,
    CONSTRAINT image_series_pkey PRIMARY KEY (seriesinstanceuid)
);

