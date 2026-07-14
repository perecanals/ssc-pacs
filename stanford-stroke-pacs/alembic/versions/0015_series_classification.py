"""series_classification: DICOM tag store, machine classification, timepoint

Revision ID: 0015_series_classification
Revises: 0014_annotation_index_cleanup
Create Date: 2026-07-13

One revision for the whole series-classification feature. It adds:

  * `series_dicom_tags` — one row per series: the full DICOM tag set of a
    representative instance as jsonb, the cross-instance aggregates no single
    header carries, and typed columns projected out of the blob.
  * `image_series` — machine classification (`series_type_rule`,
    `series_type_version`) plus the per-patient preference rank and its combined
    display label (`series_type_rank`, `series_label`).
  * `image_study` — machine `study_type_version` and the temporal axis
    (`timepoint`, `timepoint_anchor_source`, `hours_to_event`, `timepoint_version`).

## Why the tag table

Ingestion reads every instance header to compute geometry, then throws it away.
That made classification unauditable (you cannot ask *why*) and un-iterable
(changing a rule meant re-reading 131k cold archives, ~48 min). Persisting the
tags turns a reclassify into a ~30s table scan.

Tag scope is deliberately everything, including patient identifiers and private
tags (under a `_private` sub-key). This DB already stores identified upstream
data, ingestion's `anonymize_files` defaults to off, and a curated allowlist would
silently lose the vendor tags that distinguish dual-energy series.

## Why jsonb AND columns

The corpus carries 794 distinct tag names; only 37 appear on >95% of series and
622 (78%) on under 5%. A column per tag would be ~794 mostly-NULL columns against
Postgres's 1600 ceiling, growing with every new scanner — and each new vendor tag
would need a migration, so a batch from an unfamiliar scanner could fail on
*schema* rather than simply recording what it found.

So: keep the blob, project the useful few as GENERATED ... STORED columns. They
cannot drift from the jsonb, need no backfill, and need no change to the writer.
Numeric projections are regex-guarded — a bare `::float` cast in a generated
column would raise on INSERT the first time a vendor writes a non-numeric value,
breaking *ingestion*, not just the column.

PHI tags (PatientName, PatientBirthDate, PatientAge, PatientSex, AccessionNumber,
ReferringPhysicianName) are deliberately NOT promoted: they stay in the blob, so
reading them is a choice rather than the default of `SELECT *`.

## No FK to image_series

Matches the `series_cache_state` precedent (0010). `image_series` is upstream-owned
(`alembic/env.py:UPSTREAM_TABLES`) and no Alembic-managed table takes a FK onto it;
orphans are surfaced by reconciliation, not enforced by the DB.

## Timepoint anchors on the puncture, not the onset

`BL` means pre-thrombectomy, NOT post-onset. Anchor precedence, ported verbatim:
`femoral_sheath_time`, else `receiving_arrival_time + 5h`, else
`time_recognized + 10h`. Only 59% of clinical rows carry a recorded puncture, so
`timepoint_anchor_source` records which column supplied the anchor — a BL/FU built
on a `+10h` estimate is materially weaker evidence, and without this column the two
are indistinguishable. This deliberately re-opens `lvo_clinical_data`, previously
retired as a roster, scoped to exactly those three columns.

## Boundary

Every column here is MACHINE-OWNED and independent of the human annotation labels
sharing these names (`label_series_type_*`, `label_study_type_*`,
`label_timepoint_*`). Neither may be derived from the other, in either direction.

## Lock impact

CREATE TABLE and the nullable ADD COLUMNs are metadata-only and instant. The
GENERATED columns on `series_dicom_tags` rewrite that table — instant on a fresh
install (empty), but ~6.5 min on a populated one (131k rows x 21 jsonb
extractions). Migrations run at app startup, so on a populated DB run
`alembic upgrade head` from the stack root BEFORE restarting the web app, or first
boot stalls for that long. Nothing in the web app reads `series_dicom_tags`, so the
exclusive lock blocks no live traffic.

downgrade() drops everything; all of it is re-derivable from the archives.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0015_series_classification"
down_revision: str | Sequence[str] | None = "0014_annotation_index_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tags projected out of `tags` as text columns.
_TEXT_COLUMNS = [
    # classify_series() reads these four.
    ("modality", "Modality"),
    ("series_description", "SeriesDescription"),
    ("study_description", "StudyDescription"),
    ("convolution_kernel", "ConvolutionKernel"),
    ("contrast_bolus_agent", "ContrastBolusAgent"),
    # ImageType is a JSON array; ->> renders it as JSON text ('["ORIGINAL", ...]').
    # Fine for LIKE/regex; use tags->'ImageType' for real array semantics.
    ("image_type", "ImageType"),
    ("manufacturer", "Manufacturer"),
    ("manufacturer_model_name", "ManufacturerModelName"),
    ("body_part_examined", "BodyPartExamined"),
    ("photometric_interpretation", "PhotometricInterpretation"),
    ("sop_class_uid", "SOPClassUID"),
    # Raw DICOM DA/TM strings (image_series.acquisitiondatetime is the parsed clock).
    ("study_date", "StudyDate"),
    ("study_time", "StudyTime"),
    ("series_date", "SeriesDate"),
    ("series_time", "SeriesTime"),
    ("content_date", "ContentDate"),
    ("content_time", "ContentTime"),
]

_NUMERIC_COLUMNS = [
    ("diffusion_bvalue", "DiffusionBValue"),
    ("slice_thickness", "SliceThickness"),
    ("kvp", "KVP"),
    ("image_rows", "Rows"),
    ("image_columns", "Columns"),
]

_NUMERIC_RE = r"^-?[0-9]+\.?[0-9]*$"

_TAG_INDEXES = ["modality", "convolution_kernel", "body_part_examined", "manufacturer"]

_SERIES_COLUMNS = [
    ("series_type_rule", "text"),
    ("series_type_version", "text"),
    ("series_type_rank", "integer"),
    ("series_label", "text"),
]

_STUDY_COLUMNS = [
    ("study_type_version", "text"),
    ("timepoint", "text"),
    ("timepoint_anchor_source", "text"),
    ("hours_to_event", "double precision"),
    ("timepoint_version", "text"),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.series_dicom_tags (
            seriesinstanceuid    text PRIMARY KEY,
            tags                 jsonb NOT NULL DEFAULT '{}'::jsonb,
            same_position_count  integer,
            n_positions          integer,
            n_instances_scanned  integer,
            distinct_kernels     text[],
            distinct_image_types text[],
            source_instance      text,
            extractor_version    text,
            extracted_at         timestamptz DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_series_dicom_tags_gin "
        "ON public.series_dicom_tags USING gin (tags)"
    )

    for column, keyword in _TEXT_COLUMNS:
        op.execute(
            f"ALTER TABLE public.series_dicom_tags ADD COLUMN IF NOT EXISTS {column} text "
            f"GENERATED ALWAYS AS (tags->>'{keyword}') STORED"
        )
    for column, keyword in _NUMERIC_COLUMNS:
        op.execute(
            f"ALTER TABLE public.series_dicom_tags ADD COLUMN IF NOT EXISTS {column} "
            f"double precision GENERATED ALWAYS AS ("
            f"CASE WHEN tags->>'{keyword}' ~ '{_NUMERIC_RE}' "
            f"THEN (tags->>'{keyword}')::double precision END) STORED"
        )
    for column in _TAG_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_series_dicom_tags_{column} "
            f"ON public.series_dicom_tags USING btree ({column})"
        )

    for column, coltype in _SERIES_COLUMNS:
        op.execute(
            f"ALTER TABLE public.image_series ADD COLUMN IF NOT EXISTS {column} {coltype}"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_series_series_label "
        "ON public.image_series USING btree (series_label)"
    )

    for column, coltype in _STUDY_COLUMNS:
        op.execute(
            f"ALTER TABLE public.image_study ADD COLUMN IF NOT EXISTS {column} {coltype}"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_study_timepoint "
        "ON public.image_study USING btree (timepoint)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_image_study_timepoint")
    for column, _ in _STUDY_COLUMNS:
        op.execute(f"ALTER TABLE public.image_study DROP COLUMN IF EXISTS {column}")

    op.execute("DROP INDEX IF EXISTS public.idx_image_series_series_label")
    for column, _ in _SERIES_COLUMNS:
        op.execute(f"ALTER TABLE public.image_series DROP COLUMN IF EXISTS {column}")

    op.execute("DROP TABLE IF EXISTS public.series_dicom_tags")
