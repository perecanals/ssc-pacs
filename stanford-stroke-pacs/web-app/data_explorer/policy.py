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
METADATA_TABLES = {"label_definitions", "patient"}
READER_TABLES = set(TABLES) | METADATA_TABLES

# Prefer the study/series UID relationship when both tables are selected;
# joining them through the patient instead would multiply unrelated studies.
# Direct series -> patient is many-to-one; inverse paths can multiply rows.
RELATIONSHIPS = [
    ("image_study_labelled", "studyinstanceuid", "image_series_labelled", "studyinstanceuid"),
    ("patient_labelled", "patient_id", "image_study_labelled", "patient_id"),
    ("image_series_labelled", "seriesinstanceuid", "series_dicom_tags", "seriesinstanceuid"),
    ("patient_labelled", "patient_id", "image_series_labelled", "patient_id"),
]
