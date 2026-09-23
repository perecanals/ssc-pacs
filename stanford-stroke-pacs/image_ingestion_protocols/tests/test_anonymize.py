"""Tests for header anonymisation (utils.anonymize_dicom_slice) and its wiring
into the ingestion protocol (anonymize_files).

The contract: identity tags are set to the patient id (created when absent),
the blank-list is emptied, identifier sequences are removed, and everything the
pipeline depends on — UIDs, dates, descriptions, geometry — is preserved. When
the protocol runs with anonymize_files=True, the headers it persists to
series_dicom_tags and the files it copies must agree (no identified source
header may leak into the DB).

Run with: pytest tests/test_anonymize.py
"""

import os

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from image_ingestion_protocol import ImageIngestionProtocol
from test_image_ingestion_grouping import _write_dcm
from utils import (
    ANONYMIZE_BLANK_KEYWORDS,
    ANONYMIZE_METHOD,
    anonymize_dicom_slice,
)


def _identified_dataset():
    ds = Dataset()
    ds.PatientID = "12345678"
    ds.PatientName = "Doe^Jane"
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "F"
    ds.PatientAge = "054Y"
    ds.OtherPatientIDs = "OLD-1"
    ds.ReferringPhysicianName = "Ref^Doc"
    ds.OperatorsName = "Op^Tech"
    ds.InstitutionName = "Hospital"
    ds.AccessionNumber = "ACC1"
    ds.ProtocolName = "Head CT"
    ds.StudyID = "S1"
    ds.StudyInstanceUID = "1.2.3.4"
    ds.SeriesInstanceUID = "1.2.3.4.5"
    ds.SOPInstanceUID = "1.2.3.4.5.6"
    ds.FrameOfReferenceUID = "1.2.3.4.7"
    ds.StudyDate = "20240102"
    ds.StudyTime = "101010"
    ds.AcquisitionDateTime = "20240102101010"
    ds.StudyDescription = "Trombectomia"
    ds.SeriesDescription = "CTA 1mm"
    ds.Modality = "CT"
    ds.ConvolutionKernel = "H30f"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    item = Dataset()
    item.RequestedProcedureID = "RP1"
    ds.RequestAttributesSequence = Sequence([item])
    ds.add_new(0x00091001, "LO", "vendor-private")
    return ds


def test_blank_set_identity_and_preserved_tags():
    ds = anonymize_dicom_slice(_identified_dataset(), study_id="1798885")

    for keyword in ANONYMIZE_BLANK_KEYWORDS:
        if keyword in ds:
            assert ds[keyword].value in ("", None), keyword
    assert ds.PatientID == "1798885"
    assert ds.StudyID == "1798885"
    assert str(ds.PatientName) == "Anonymous"
    assert ds.PatientIdentityRemoved == "YES"
    assert ds.DeidentificationMethod == ANONYMIZE_METHOD
    assert "RequestAttributesSequence" not in ds

    # Everything the pipeline reads survives untouched.
    assert ds.StudyInstanceUID == "1.2.3.4"
    assert ds.SeriesInstanceUID == "1.2.3.4.5"
    assert ds.SOPInstanceUID == "1.2.3.4.5.6"
    assert ds.FrameOfReferenceUID == "1.2.3.4.7"
    assert ds.StudyDate == "20240102"
    assert ds.StudyTime == "101010"
    assert ds.AcquisitionDateTime == "20240102101010"
    assert ds.StudyDescription == "Trombectomia"
    assert ds.SeriesDescription == "CTA 1mm"
    assert ds.ConvolutionKernel == "H30f"
    assert list(ds.ImageType) == ["ORIGINAL", "PRIMARY", "AXIAL"]


def test_absent_identity_elements_are_created():
    ds = Dataset()
    ds.SeriesInstanceUID = "1.2.3"
    anonymize_dicom_slice(ds, study_id="42")

    assert ds.PatientID == "42"
    assert ds.StudyID == "42"
    assert str(ds.PatientName) == "Anonymous"
    assert ds.PatientIdentityRemoved == "YES"
    # Blank-list elements are not invented when absent.
    assert "PatientBirthDate" not in ds
    assert "ReferringPhysicianName" not in ds


def test_study_id_defaults_to_own_patient_id():
    ds = _identified_dataset()
    anonymize_dicom_slice(ds)
    assert ds.PatientID == "12345678"
    assert ds.StudyID == "12345678"


def test_private_tags_kept_by_default_and_removable():
    ds = anonymize_dicom_slice(_identified_dataset(), study_id="1")
    assert (0x0009, 0x1001) in ds

    ds = anonymize_dicom_slice(
        _identified_dataset(), study_id="1", remove_private_tags=True
    )
    assert (0x0009, 0x1001) not in ds


def test_idempotent():
    ds = anonymize_dicom_slice(_identified_dataset(), study_id="7")
    first = ds.to_json_dict()
    anonymize_dicom_slice(ds, study_id="7")
    assert ds.to_json_dict() == first


def _identified_case(tmp_path):
    case = tmp_path / "case"
    for i in (1, 2):
        _write_dcm(
            case / "s" / f"f{i}.dcm", "1.2.3.70", 5, i, patient_id="11-002"
        )
    # Add identifiers the fixture does not write, so the leak is observable.
    for path in sorted((case / "s").iterdir()):
        ds = pydicom.dcmread(path)
        ds.PatientName = "Doe^Jane"
        ds.ReferringPhysicianName = "Ref^Doc"
        ds.ProtocolName = "Head CT"
        ds.save_as(path)
    return case


def test_protocol_persists_anonymised_headers_only(tmp_path):
    case = _identified_case(tmp_path)

    p = ImageIngestionProtocol(
        case_dir=str(case), postgres_engine=None, anonymize_files=True
    )
    p.create_series_table()

    tags = p.case_series_tags_table.iloc[0]["tags"]
    assert tags.get("PatientName") in (None, "Anonymous")
    assert not tags.get("ReferringPhysicianName")
    assert tags.get("PatientID") == "11-002"
    # Identity is unchanged — the table still keys on the source PatientID.
    assert p.case_series_table.iloc[0]["patient_id"] == "11-002"
    # protocolname reflects what the copied files will contain.
    assert not p.case_series_table.iloc[0]["protocolname"]


def test_protocol_without_anonymise_keeps_source_headers(tmp_path):
    case = _identified_case(tmp_path)

    p = ImageIngestionProtocol(case_dir=str(case), postgres_engine=None)
    p.create_series_table()

    tags = p.case_series_tags_table.iloc[0]["tags"]
    assert tags.get("PatientName") == "Doe^Jane"
    assert p.case_series_table.iloc[0]["protocolname"] == "Head CT"


def test_copied_files_are_anonymised(tmp_path):
    case = _identified_case(tmp_path)

    p = ImageIngestionProtocol(
        case_dir=str(case), postgres_engine=None, anonymize_files=True
    )
    p.base_dir = str(tmp_path / "dest")
    p.create_series_table()
    p.create_study_table()
    p.add_paths_and_copy_dicom_files()

    row = p.case_series_table.iloc[0]
    copied = [dst for _src, dst in row["copied_pairs"]]
    assert len(copied) == 2
    for dst in copied:
        ds = pydicom.dcmread(dst)
        assert str(ds.PatientName) == "Anonymous"
        assert ds.PatientID == "11-002"
        assert ds.StudyID == "11-002"
        assert ds.ReferringPhysicianName == ""
        assert ds.SeriesInstanceUID == "1.2.3.70"
        assert ds.PatientIdentityRemoved == "YES"
    # Source untouched.
    src = pydicom.dcmread(os.path.join(case, "s", "f1.dcm"))
    assert str(src.PatientName) == "Doe^Jane"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
