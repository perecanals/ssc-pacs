-- Create table in the stanford-stroke database.
-- Derived from public.stanford_ctas_soren_dicom_series in stanford_data.
-- This script only defines the table; it does not load any data.
--
-- Manual-bootstrap MIRROR only: the canonical fresh-install path is Alembic
-- (revisions 0001 + 0011 + 0012). Keep this file in sync with those.

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
    import_id integer,
    import_label text,
    pixelspacing double precision[],
    slicethickness double precision,
    imageshape integer[],
    number_of_slices integer,
    scanaxialcoverage_mm double precision,
    -- Storage footprint in decimal MB (bytes / 1e6). compressed = tar.zst
    -- archive size; decompressed = sum of DICOM file content bytes.
    compressed_size_mb double precision,
    decompressed_size_mb double precision,
    CONSTRAINT image_series_pkey PRIMARY KEY (seriesinstanceuid)
);

-- Join/filter hot paths (mirrors Alembic revision 0011).
CREATE INDEX IF NOT EXISTS idx_image_series_studyinstanceuid
    ON public.image_series USING btree (studyinstanceuid);
CREATE INDEX IF NOT EXISTS idx_image_series_patient_id
    ON public.image_series USING btree (patient_id);

