"""Explicit research catalog. Future roles must extend this policy, not UI checks."""

TABLES = {
    "patient_labelled": "Patients with patient-level labels (eventually consistent)",
    "image_study_labelled": "Studies with study-level labels (eventually consistent)",
    "image_series_labelled": "Series with series-level labels (eventually consistent)",
    "series_dicom_tags": "Representative DICOM tags and aggregates",
}

# Fixed internal metadata reads only. These relations must never be passed to
# SQL validation as user-queryable tables or exposed in the browser catalog.
# Patient membership is authoritative for staff dataset authorization; mirrors
# are eventually consistent and must not decide access to patient data.
# study_visibility (a view, Alembic 0028) decides which imaging is in a cohort.
METADATA_TABLES = {"label_definitions", "patient", "study_visibility"}
READER_TABLES = set(TABLES) | METADATA_TABLES

# Prefer the study/series UID relationship when both tables are selected;
# joining them through the patient instead would multiply unrelated studies.
# Imaging joins the patients (enrollments) that see it — its owner, plus any
# member that re-sent the very same UID (Alembic 0028) — so a shared study
# yields one row per in-cohort enrollment that sees it; copies re-exported under
# other UIDs are separate studies. Those two joins are built over
# study_visibility (VISIBILITY_JOINS), not column equality. Inverse paths can
# multiply rows.
RELATIONSHIPS = [
    ("image_study_labelled", "studyinstanceuid", "image_series_labelled", "studyinstanceuid"),
    ("patient_labelled", "patient_key", "image_study_labelled", "studyinstanceuid"),
    ("image_series_labelled", "seriesinstanceuid", "series_dicom_tags", "seriesinstanceuid"),
    ("patient_labelled", "patient_key", "image_series_labelled", "studyinstanceuid"),
]
VISIBILITY_JOINS = {
    frozenset(("patient_labelled", "image_study_labelled")),
    frozenset(("patient_labelled", "image_series_labelled")),
}
# The one non-catalog relation module-generated SQL may reference; never
# user-queryable (validate_sql only admits catalog tables).
VISIBILITY_VIEW = "study_visibility"
