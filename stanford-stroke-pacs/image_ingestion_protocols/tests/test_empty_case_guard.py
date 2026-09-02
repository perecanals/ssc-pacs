"""Tests pinning the "files present but no series built" guard.

Regression cover for the 2026-08-15 16:09 batch, where case 2523 was reported
"Successfully completed processing" while contributing zero rows. Its directory
held 242 perfectly valid DICOM files; the source is an sshfs mount and the walk
transiently returned nothing. A genuinely empty directory and a directory we
merely failed to read were indistinguishable, so a whole case vanished from the
batch while the summary counted it as processed.

A truly empty directory must still be a no-op, not an error.

No DB: create_series_table takes postgres_engine=None.

Run with: pytest tests/test_empty_case_guard.py
"""

import os

import pytest

from image_ingestion_protocol import ImageIngestionProtocol

from test_image_ingestion_grouping import _write_dcm


def _protocol(case_dir):
    return ImageIngestionProtocol(case_dir=str(case_dir), postgres_engine=None)


def test_truly_empty_case_is_a_clean_noop(tmp_path):
    """No files at all -> legitimately empty, returns an empty result."""
    case = tmp_path / "emptycase"
    case.mkdir()

    p = _protocol(case)
    result = p.execute_image_ingestion_protocol()

    assert result["studyinstanceuids"] == []
    assert result["seriesinstanceuids"] == []
    assert p.scan_candidate_files == 0


def test_directory_of_unreadable_files_raises(tmp_path):
    """Files present but none parse -> a failure, never a silent success."""
    case = tmp_path / "unreadable"
    series_dir = case / "series"
    series_dir.mkdir(parents=True)
    for i in range(3):
        (series_dir / f"junk{i}.dcm").write_bytes(b"not a dicom at all")

    p = _protocol(case)
    with pytest.raises(RuntimeError) as excinfo:
        p.execute_image_ingestion_protocol()

    message = str(excinfo.value)
    assert "built 0 series" in message
    assert "3 file(s)" in message
    assert p.scan_candidate_files == 3
    assert p.scan_unreadable_files == 3


def test_files_readable_but_missing_uids_also_raises(tmp_path):
    """The other silent-drop path: parses fine, but no PatientID/UIDs."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    case = tmp_path / "nouids"
    case.mkdir()
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    ds.SOPInstanceUID = generate_uid()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    pydicom.dcmwrite(str(case / "x.dcm"), ds, write_like_original=False)

    p = _protocol(case)
    with pytest.raises(RuntimeError, match="built 0 series"):
        p.execute_image_ingestion_protocol()

    # Readable, so it is not counted as unreadable — but still yields no series.
    assert p.scan_candidate_files == 1
    assert p.scan_unreadable_files == 0


def test_hidden_files_do_not_count_as_candidates(tmp_path):
    """.DS_Store alone must not turn an empty case into a hard failure."""
    case = tmp_path / "dsstore"
    case.mkdir()
    (case / ".DS_Store").write_bytes(b"\x00\x01")

    p = _protocol(case)
    result = p.execute_image_ingestion_protocol()

    assert result["seriesinstanceuids"] == []
    assert p.scan_candidate_files == 0


def test_readable_case_is_unaffected(tmp_path):
    """The guard must not disturb a normal case."""
    case = tmp_path / "goodcase"
    _write_dcm(case / "s" / "a.dcm", "1.2.3.77", 1, 1)

    p = _protocol(case)
    p.create_series_table()

    assert len(p.case_series_table) == 1
    assert p.scan_candidate_files == 1
    assert p.scan_unreadable_files == 0
