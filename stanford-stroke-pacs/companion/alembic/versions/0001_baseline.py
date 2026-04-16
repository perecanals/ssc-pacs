"""baseline: snapshot of production schema at Alembic introduction

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-15

This revision reproduces the entire `stanford-stroke` schema as it stood
at the moment Alembic was introduced (2026-04-15). It is captured from
`pg_dump --schema-only` against production with owner/privilege noise
stripped — see workstream 04 §6 (Verification) for the diff procedure.

Why a single combined baseline rather than one revision per pre-existing
ALTER:

  - Production has been live for months; the `INIT_SQL` + `MIGRATE_SQL`
    blocks that used to run at app startup encode the cumulative state,
    not a series of individually re-runnable migrations (the dedup step
    in particular is data-dependent and one-shot). Splitting them into
    separate Alembic revisions would either (a) re-run them on prod when
    we stamp, or (b) duplicate the same DDL twice — both are worse than
    capturing the terminal state once.
  - The acceptance gate (T7) is `pg_dump --schema-only` parity between
    production and a scratch DB after `alembic upgrade head`. A single
    baseline that mirrors prod satisfies that gate exactly.
  - In production this revision is applied via `alembic stamp 0001_baseline`
    (no DDL runs); on a fresh DB it runs the full block. See
    `documentation/operations/schema_migrations.md`.

Tables created here fall in three groups:

  Companion-owned (managed by future Alembic revisions):
    annotations, label_definitions, users, user_preferences,
    cache_state, orthanc_resource_map

  Upstream raw tables (managed by external ingest, not by us):
    image_series, image_study, lvo_clinical_data

  Dynamic tables (managed at runtime by labelled_table_sync.py based on
  label_definitions):
    image_series_labelled, image_study_labelled,
    lvo_clinical_data_labelled, snapshot_patients, snapshot_studys,
    snapshot_seriess

  The upstream and dynamic groups are excluded from `--autogenerate`
  proposals via `include_object` in `alembic/env.py`. They are still
  created here so scratch-DB schema-diff parity holds.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BASELINE_SQL = r"""
-- =========================================================================
-- Companion-owned tables
-- =========================================================================

CREATE TABLE public.annotations (
    id integer NOT NULL,
    seriesinstanceuid text,
    studyinstanceuid text,
    patient_id text,
    label text NOT NULL,
    value text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    notes text,
    level text DEFAULT 'series'::text NOT NULL,
    CONSTRAINT annotations_level_check CHECK ((level = ANY (ARRAY['patient'::text, 'study'::text, 'series'::text])))
);

CREATE SEQUENCE public.annotations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.annotations_id_seq OWNED BY public.annotations.id;
ALTER TABLE ONLY public.annotations ALTER COLUMN id SET DEFAULT nextval('public.annotations_id_seq'::regclass);
ALTER TABLE ONLY public.annotations ADD CONSTRAINT annotations_pkey PRIMARY KEY (id);

CREATE INDEX idx_annotations_label   ON public.annotations USING btree (label);
CREATE INDEX idx_annotations_level   ON public.annotations USING btree (level);
CREATE INDEX idx_annotations_patient ON public.annotations USING btree (patient_id);
CREATE INDEX idx_annotations_series  ON public.annotations USING btree (seriesinstanceuid);
CREATE INDEX idx_annotations_study   ON public.annotations USING btree (studyinstanceuid);

CREATE UNIQUE INDEX idx_ann_shared_patient ON public.annotations USING btree (patient_id, label)        WHERE (level = 'patient'::text);
CREATE UNIQUE INDEX idx_ann_shared_series  ON public.annotations USING btree (seriesinstanceuid, label) WHERE (level = 'series'::text);
CREATE UNIQUE INDEX idx_ann_shared_study   ON public.annotations USING btree (studyinstanceuid, label)  WHERE (level = 'study'::text);


CREATE TABLE public.label_definitions (
    id integer NOT NULL,
    name text NOT NULL,
    description text,
    datatype text DEFAULT 'bool'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    options text,
    level text DEFAULT 'series'::text NOT NULL,
    CONSTRAINT label_definitions_datatype_check CHECK ((datatype = ANY (ARRAY['bool'::text, 'int'::text, 'text'::text, 'select'::text]))),
    CONSTRAINT label_definitions_level_check    CHECK ((level    = ANY (ARRAY['patient'::text, 'study'::text, 'series'::text])))
);

CREATE SEQUENCE public.label_definitions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.label_definitions_id_seq OWNED BY public.label_definitions.id;
ALTER TABLE ONLY public.label_definitions ALTER COLUMN id SET DEFAULT nextval('public.label_definitions_id_seq'::regclass);
ALTER TABLE ONLY public.label_definitions ADD CONSTRAINT label_definitions_pkey     PRIMARY KEY (id);
ALTER TABLE ONLY public.label_definitions ADD CONSTRAINT label_definitions_name_key UNIQUE (name);


CREATE TABLE public.users (
    username text NOT NULL,
    password_hash text NOT NULL,
    is_admin boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

ALTER TABLE ONLY public.users ADD CONSTRAINT users_pkey PRIMARY KEY (username);


CREATE TABLE public.user_preferences (
    username text NOT NULL,
    level text NOT NULL,
    prefs jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT user_preferences_level_check CHECK ((level = ANY (ARRAY['patient'::text, 'study'::text, 'series'::text, '_global'::text])))
);

ALTER TABLE ONLY public.user_preferences ADD CONSTRAINT user_preferences_pkey PRIMARY KEY (username, level);
ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_username_fkey FOREIGN KEY (username) REFERENCES public.users(username) ON DELETE CASCADE;


CREATE TABLE public.cache_state (
    studyinstanceuid text NOT NULL,
    status text DEFAULT 'cold'::text NOT NULL,
    cache_path text,
    warmed_at timestamp with time zone,
    last_accessed_at timestamp with time zone,
    error_message text,
    CONSTRAINT cache_state_status_check CHECK ((status = ANY (ARRAY['cold'::text, 'warming'::text, 'hot'::text, 'error'::text])))
);

ALTER TABLE ONLY public.cache_state ADD CONSTRAINT cache_state_pkey PRIMARY KEY (studyinstanceuid);

CREATE INDEX idx_cache_state_last_accessed ON public.cache_state USING btree (last_accessed_at);
CREATE INDEX idx_cache_state_status        ON public.cache_state USING btree (status);


CREATE TABLE public.orthanc_resource_map (
    orthanc_id text NOT NULL,
    resource_type text NOT NULL,
    studyinstanceuid text NOT NULL,
    seriesinstanceuid text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT orthanc_resource_map_resource_type_check CHECK ((resource_type = ANY (ARRAY['study'::text, 'series'::text, 'instance'::text])))
);

ALTER TABLE ONLY public.orthanc_resource_map ADD CONSTRAINT orthanc_resource_map_pkey PRIMARY KEY (orthanc_id);
ALTER TABLE ONLY public.orthanc_resource_map
    ADD CONSTRAINT orthanc_resource_map_studyinstanceuid_fkey FOREIGN KEY (studyinstanceuid) REFERENCES public.cache_state(studyinstanceuid) ON DELETE CASCADE;

CREATE INDEX idx_orm_study ON public.orthanc_resource_map USING btree (studyinstanceuid);


-- =========================================================================
-- Upstream raw tables (owned by external ingest; Alembic does not manage
-- their evolution — see env.py include_object filter).
-- =========================================================================

CREATE TABLE public.image_series (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    studydescription text,
    seriesdescription text,
    series_type text,
    studyinstanceuid text,
    seriesinstanceuid text NOT NULL,
    dicom_dir_path text,
    nifti_path text,
    protocolname text,
    seriesnumber integer,
    instancenumber integer,
    manufacturer text,
    pixelspacing double precision[],
    slicethickness double precision,
    imageshape integer[],
    scanaxialcoverage_mm double precision,
    modality text,
    import_id integer,
    import_label text,
    number_of_slices integer,
    dicom_archive_path text
);

ALTER TABLE ONLY public.image_series ADD CONSTRAINT image_series_pkey PRIMARY KEY (seriesinstanceuid);
CREATE INDEX idx_image_series_number_of_slices ON public.image_series USING btree (number_of_slices);


CREATE TABLE public.image_study (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    study_type text,
    studydescription text,
    studyinstanceuid text NOT NULL,
    study_path text,
    protocolname text,
    manufacturer text,
    import_id integer,
    import_label text
);

ALTER TABLE ONLY public.image_study ADD CONSTRAINT image_study_pkey PRIMARY KEY (studyinstanceuid);


CREATE TABLE public.lvo_clinical_data (
    mrn double precision,
    study_id text,
    sir_database_id double precision,
    stroke_date text,
    enroll_date text,
    redcap_repeat_instrument double precision,
    redcap_repeat_instance double precision,
    site_id double precision,
    enroll_nr text,
    is_retro double precision,
    inc_ica_mca_occl double precision,
    inc_ct_ctp_outside double precision,
    inc_transfer_evt double precision,
    inc_ct_ctp_studysite double precision,
    inc_meet_criteria double precision,
    inc_not_eligible_comment text,
    is_female double precision,
    dob text,
    age double precision,
    is_hispanic double precision,
    race text,
    demographics_complete double precision,
    research_patient double precision,
    research_studies text,
    research_study_id text,
    was_pt_transfer double precision,
    osh double precision,
    other_osh text,
    hx_mi_cad double precision,
    hypertension double precision,
    atrial_fibrillation double precision,
    hyperlipidemia double precision,
    dm double precision,
    hx_stroke double precision,
    smoking double precision,
    hx_antiplatelet double precision,
    hx_anticoagulant double precision,
    initial_glucose double precision,
    referring_first_bp_time text,
    referring_first_sbp double precision,
    referring_last_bp_time text,
    referring_last_sbp double precision,
    referring_last_dbp double precision,
    receiving_first_bp_time text,
    receiving_first_sbp double precision,
    receiving_first_dbp double precision,
    onset_witnessed double precision,
    onset_time text,
    time_normal text,
    time_recognized text,
    in_patient double precision,
    referring_er_present_time text,
    receiving_arrival_time text,
    iv_thrombolytic double precision,
    ivt_where double precision,
    iv_thrombolysis_time text,
    iv_thrombolysis_no_reason text,
    iv_thrombolysis_no_otherreasons text,
    iv_thrombolysis_comment text,
    baseline_data_complete double precision,
    mrs_prestroke_time text,
    mrs_prestroke double precision,
    mrs_dc_time text,
    mrs_dc double precision,
    mrs_90d_time text,
    mrs_90d double precision,
    mrs_90d_method double precision,
    mrs_notes text,
    mrs_summary_complete double precision,
    arterial_punc_yes_no double precision,
    reason_no_cath text,
    other_no_cath text,
    second_evt_yn double precision,
    femoral_sheath_time text,
    undergo_treatment double precision,
    no_intervention_why text,
    revasc_date_time text,
    femoral_closure_time text,
    cath_intubated double precision,
    primary_aol double precision,
    primary_aol_other text,
    ia_final_tici text,
    number_of_passes double precision,
    first_pass_recanal double precision,
    endo_technique text,
    angioplasty_type double precision,
    stent_location double precision,
    cath_complications double precision,
    type_cath_complication text,
    notes_cath_comp text,
    angiogram_report text,
    endovascularl_procedure_complete double precision,
    place_discharge double precision,
    other_dc_loc text,
    discharge_complete double precision,
    imaging_refer_bl_time text,
    imaging_type_refer_bl text,
    osh_scan_inpacsyn double precision,
    imaging_receiv_bl_time text,
    imaging_type_receiv_bl text,
    imaging_ia_time text,
    imaging_fu1_time text,
    imaging_type_fu1 text,
    imaging_fu2_time text,
    imaging_type_fu2 double precision,
    imaging_fu3_time text,
    imaging_type_fu3 double precision,
    imaging_fu4_time text,
    imaging_type_fu4 double precision,
    brain_imaging_summary_complete double precision,
    vo_occl_side double precision,
    nihss_total_calc double precision,
    nihss_total_manual double precision,
    nihss_baseline double precision,
    occlusion_location text
);


-- =========================================================================
-- Dynamic labelled / snapshot tables (managed by labelled_table_sync.py
-- at runtime; recreated here to satisfy schema-diff parity for scratch DBs).
-- =========================================================================

CREATE TABLE public.image_series_labelled (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    studydescription text,
    seriesdescription text,
    series_type text,
    studyinstanceuid text,
    seriesinstanceuid text,
    dicom_dir_path text,
    nifti_path text,
    protocolname text,
    seriesnumber integer,
    instancenumber integer,
    manufacturer text,
    pixelspacing double precision[],
    slicethickness double precision,
    imageshape integer[],
    scanaxialcoverage_mm double precision,
    modality text,
    integration_id integer,
    label_baseline_cta_pere boolean DEFAULT false NOT NULL,
    label_baseline_mra boolean DEFAULT false NOT NULL,
    import_label text,
    import_id integer,
    label_series_type_praneeta text,
    number_of_slices integer,
    dicom_archive_path text
);

CREATE UNIQUE INDEX image_series_labelled_seriesinstanceuid_uidx ON public.image_series_labelled USING btree (seriesinstanceuid);


CREATE TABLE public.image_study_labelled (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    study_type text,
    studydescription text,
    studyinstanceuid text,
    study_path text,
    protocolname text,
    manufacturer text,
    integration_id integer,
    label_study_type_pere text,
    import_label text,
    import_id integer,
    label_study_timepoint text
);

CREATE UNIQUE INDEX image_study_labelled_studyinstanceuid_uidx ON public.image_study_labelled USING btree (studyinstanceuid);


CREATE TABLE public.lvo_clinical_data_labelled (
    mrn double precision,
    study_id text,
    sir_database_id double precision,
    stroke_date text,
    enroll_date text,
    redcap_repeat_instrument double precision,
    redcap_repeat_instance double precision,
    site_id double precision,
    enroll_nr text,
    is_retro double precision,
    inc_ica_mca_occl double precision,
    inc_ct_ctp_outside double precision,
    inc_transfer_evt double precision,
    inc_ct_ctp_studysite double precision,
    inc_meet_criteria double precision,
    inc_not_eligible_comment text,
    is_female double precision,
    dob text,
    age double precision,
    is_hispanic double precision,
    race text,
    demographics_complete double precision,
    research_patient double precision,
    research_studies text,
    research_study_id text,
    was_pt_transfer double precision,
    osh double precision,
    other_osh text,
    hx_mi_cad double precision,
    hypertension double precision,
    atrial_fibrillation double precision,
    hyperlipidemia double precision,
    dm double precision,
    hx_stroke double precision,
    smoking double precision,
    hx_antiplatelet double precision,
    hx_anticoagulant double precision,
    initial_glucose double precision,
    referring_first_bp_time text,
    referring_first_sbp double precision,
    referring_last_bp_time text,
    referring_last_sbp double precision,
    referring_last_dbp double precision,
    receiving_first_bp_time text,
    receiving_first_sbp double precision,
    receiving_first_dbp double precision,
    onset_witnessed double precision,
    onset_time text,
    time_normal text,
    time_recognized text,
    in_patient double precision,
    referring_er_present_time text,
    receiving_arrival_time text,
    iv_thrombolytic double precision,
    ivt_where double precision,
    iv_thrombolysis_time text,
    iv_thrombolysis_no_reason text,
    iv_thrombolysis_no_otherreasons text,
    iv_thrombolysis_comment text,
    baseline_data_complete double precision,
    mrs_prestroke_time text,
    mrs_prestroke double precision,
    mrs_dc_time text,
    mrs_dc double precision,
    mrs_90d_time text,
    mrs_90d double precision,
    mrs_90d_method double precision,
    mrs_notes text,
    mrs_summary_complete double precision,
    arterial_punc_yes_no double precision,
    reason_no_cath text,
    other_no_cath text,
    second_evt_yn double precision,
    femoral_sheath_time text,
    undergo_treatment double precision,
    no_intervention_why text,
    revasc_date_time text,
    femoral_closure_time text,
    cath_intubated double precision,
    primary_aol double precision,
    primary_aol_other text,
    ia_final_tici text,
    number_of_passes double precision,
    first_pass_recanal double precision,
    endo_technique text,
    angioplasty_type double precision,
    stent_location double precision,
    cath_complications double precision,
    type_cath_complication text,
    notes_cath_comp text,
    angiogram_report text,
    endovascularl_procedure_complete double precision,
    place_discharge double precision,
    other_dc_loc text,
    discharge_complete double precision,
    imaging_refer_bl_time text,
    imaging_type_refer_bl text,
    osh_scan_inpacsyn double precision,
    imaging_receiv_bl_time text,
    imaging_type_receiv_bl text,
    imaging_ia_time text,
    imaging_fu1_time text,
    imaging_type_fu1 text,
    imaging_fu2_time text,
    imaging_type_fu2 double precision,
    imaging_fu3_time text,
    imaging_type_fu3 double precision,
    imaging_fu4_time text,
    imaging_type_fu4 double precision,
    brain_imaging_summary_complete double precision,
    vo_occl_side double precision,
    nihss_total_calc double precision,
    nihss_total_manual double precision,
    nihss_baseline double precision,
    occlusion_location text,
    label_laterality_stroke text,
    label_occlusion_location_cta text,
    label_tandem_cta boolean DEFAULT false NOT NULL,
    label_m2_distal boolean DEFAULT false NOT NULL,
    label_mevo_revision_comment text,
    label_m2_dominance text,
    label_mevo_revision_exclusion_reason text
);

CREATE UNIQUE INDEX lvo_clinical_data_labelled_study_id_uidx ON public.lvo_clinical_data_labelled USING btree (study_id);


CREATE TABLE public.snapshot_patients (
    patient_id text,
    stroke_date text,
    label_laterality_stroke text,
    label_m2_distal text,
    label_m2_dominance text,
    label_mevo_revision_comment text,
    label_mevo_revision_exclusion_reason text,
    label_occlusion_location_cta text,
    label_tandem_cta text
);


CREATE TABLE public.snapshot_seriess (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    modality text,
    seriesdescription text,
    seriesinstanceuid text,
    label_baseline_cta_pere text,
    label_baseline_mra text,
    label_series_type_praneeta text
);


CREATE TABLE public.snapshot_studys (
    patient_id text,
    acquisitiondatetime timestamp without time zone,
    study_type text,
    studyinstanceuid text,
    label_study_timepoint text,
    label_study_type_pere text
);
"""


# Best-effort downgrade. Drops every object created by upgrade(). Not used
# in production (we never roll the baseline back) but provided so
# `alembic downgrade base` works on a scratch DB.
DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS public.snapshot_studys                CASCADE;
DROP TABLE IF EXISTS public.snapshot_seriess               CASCADE;
DROP TABLE IF EXISTS public.snapshot_patients              CASCADE;
DROP TABLE IF EXISTS public.lvo_clinical_data_labelled     CASCADE;
DROP TABLE IF EXISTS public.image_study_labelled           CASCADE;
DROP TABLE IF EXISTS public.image_series_labelled          CASCADE;
DROP TABLE IF EXISTS public.lvo_clinical_data              CASCADE;
DROP TABLE IF EXISTS public.image_study                    CASCADE;
DROP TABLE IF EXISTS public.image_series                   CASCADE;
DROP TABLE IF EXISTS public.orthanc_resource_map           CASCADE;
DROP TABLE IF EXISTS public.cache_state                    CASCADE;
DROP TABLE IF EXISTS public.user_preferences               CASCADE;
DROP TABLE IF EXISTS public.users                          CASCADE;
DROP TABLE IF EXISTS public.label_definitions              CASCADE;
DROP TABLE IF EXISTS public.annotations                    CASCADE;
"""


def upgrade() -> None:
    op.execute(BASELINE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
