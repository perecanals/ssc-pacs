"""Explicit research catalog. Future roles must extend this policy, not UI checks."""
TABLES = {
    "patient": "Imaging-derived patient registry",
    "image_study": "Study metadata",
    "image_series": "Series metadata",
    "patient_labelled": "Patients with patient-level labels (eventually consistent)",
    "image_study_labelled": "Studies with study-level labels (eventually consistent)",
    "image_series_labelled": "Series with series-level labels (eventually consistent)",
    "annotations": "Individual annotation values",
    "label_definitions": "Label definitions and instruments",
    "label_value_options": "Controlled label vocabulary",
    "series_dicom_tags": "Representative DICOM tags and aggregates",
}

# A single outward path from each base preserves explicit row granularity.
# Series -> study -> patient is many-to-one; inverse paths multiply rows.
RELATIONSHIPS = [
    ("patient", "patient_id", "image_study", "patient_id"),
    ("image_study", "studyinstanceuid", "image_series", "studyinstanceuid"),
    ("patient_labelled", "patient_id", "image_study_labelled", "patient_id"),
    ("image_study_labelled", "studyinstanceuid", "image_series_labelled", "studyinstanceuid"),
    ("image_series", "seriesinstanceuid", "series_dicom_tags", "seriesinstanceuid"),
]
