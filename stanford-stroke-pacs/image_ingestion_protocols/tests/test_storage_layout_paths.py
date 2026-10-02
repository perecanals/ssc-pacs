"""Where ingestion files a study: <base>/<owner's dataset slug>/<patient_id>/<StudyUID>.

The slug is the owner's (resolve_ownership stamps patient_key), not the batch
dataset's; a new series under an already stored study joins its siblings.

Run with: pytest tests/test_storage_layout_paths.py
"""

import os

import pandas as pd
from test_image_ingestion_grouping import _write_dcm

from image_ingestion_protocol import ImageIngestionProtocol


def _protocol(tmp_path, patient_key=None):
    case = tmp_path / "case"
    _write_dcm(case / "s" / "f1.dcm", "1.2.3.80.1", 5, 1, patient_id="11-002")
    p = ImageIngestionProtocol(case_dir=str(case), postgres_engine=None)
    p.base_dir = str(tmp_path / "dest")
    p.dataset_slug = "precise"
    p.create_series_table()
    p.create_study_table()
    if patient_key:
        p.case_series_table["patient_key"] = patient_key
        p.case_study_table["patient_key"] = patient_key
    return p


def _study_uid(p):
    return p.case_series_table.iloc[0]["studyinstanceuid"]


def test_new_study_lands_under_the_batch_slug(tmp_path):
    p = _protocol(tmp_path)
    p.add_paths_and_copy_dicom_files()
    expected = os.path.join(p.base_dir, "precise", "11-002", _study_uid(p))
    assert p.case_study_table.iloc[0]["study_path"] == expected
    assert p.case_series_table.iloc[0]["dicom_dir_path"].startswith(expected + os.sep)
    assert os.path.isdir(expected)


def test_study_owned_by_another_dataset_lands_under_the_owner(tmp_path):
    p = _protocol(tmp_path, patient_key="crisp2-lvo__11-002")
    p.add_paths_and_copy_dicom_files()
    assert p.case_study_table.iloc[0]["study_path"] == os.path.join(
        p.base_dir, "crisp2-lvo", "11-002", _study_uid(p))


def test_new_series_under_a_stored_study_joins_its_siblings(tmp_path):
    p = _protocol(tmp_path, patient_key="crisp2-lvo__11-002")
    stored = os.path.join(p.base_dir, "precise", "11-002", _study_uid(p))
    p.image_study = pd.DataFrame(
        [{"studyinstanceuid": _study_uid(p), "patient_id": "11-002", "study_path": stored}])
    p.add_paths_and_copy_dicom_files()
    assert p.case_series_table.iloc[0]["dicom_dir_path"].startswith(stored + os.sep)
