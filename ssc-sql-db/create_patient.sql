-- Create table in the stanford-stroke database.
-- Patient-level registry: one row per individual patient in the database,
-- independent of whether a clinical row exists in lvo_clinical_data.
-- Populated equivalently to image_study/image_series by the ingest pipeline.
-- This script only defines the table; it does not load any data.

\connect "stanford-stroke"

CREATE TABLE IF NOT EXISTS public.patient (
    patient_id text NOT NULL,
    stroke_date timestamp without time zone,  -- imaging-derived: MIN(image_study.acquisitiondatetime)
    import_id integer,                          -- origin batch (lowest import_id), preserved on conflict
    import_label text,                          -- origin label, preserved on conflict
    dataset text[] NOT NULL DEFAULT '{}',       -- union-accumulated across batches
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT patient_pkey PRIMARY KEY (patient_id)
);
