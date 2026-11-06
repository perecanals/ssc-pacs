"""Duplicate exports must be separated from repeat scans and reconstructions."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import numpy as np
import pydicom
import pytest
import zstandard
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/data_integrity/duplicate_series_audit.py"
spec = importlib.util.spec_from_file_location("duplicate_series_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def row(uid="1.2.1", **updates):
    result = dict(
        seriesinstanceuid=uid, studyinstanceuid="1.2", subject_id="guide__person",
        patient_key="guide__person", seriesdescription="Head W/O  3.0  J30s  2",
        modality="CT", number_of_slices=2, imageshape=[2, 2, 2],
        acquisitiondatetime="2017-09-04 00:27:42", acquisitiondatetime_source="acquisition",
        pixelspacing=[0.4, 0.4], slicethickness=3.0, n_instances_scanned=2,
        tags={"ImageOrientationPatient": [1, 0, 0, 0, 1, 0], "ConvolutionKernel": ["J30s", "2"]},
    )
    result.update(updates)
    return result


def test_reexport_ids_and_series_numbers_do_not_hide_duplicates():
    a = row(seriesnumber=3)
    b = row("1.2.2", seriesnumber=103, seriesdescription="head W/O 3.0 J30s 2 ")
    groups, summary = audit.find_candidates([a, b])
    assert len(groups) == 1
    assert groups[0]["match"] == "strong_metadata"
    assert summary["candidate_series"] == 2


@pytest.mark.parametrize("change", [
    {"acquisitiondatetime": "2017-09-04 00:28:42"},
    {"pixelspacing": [0.5, 0.5]},
    {"slicethickness": 5.0},
    {"number_of_slices": 3},
    {"imageshape": [3, 2, 2]},
    {"tags": {"ConvolutionKernel": "J70h"}},
    {"tags": {"EchoTime": 90}},
    {"tags": {"DiffusionBValue": 1000}},
    {"n_positions": 1},
])
def test_distinct_acquisitions_and_reconstructions_do_not_match(change):
    assert audit.find_candidates([row(), row("1.2.2", **change)])[0] == []


def test_missing_metadata_is_reported_and_not_called_strong():
    rows = [row(acquisitiondatetime_source="study"), row("1.2.2", acquisitiondatetime_source="study"),
            row("1.2.3", imageshape=None), row("1.2.4", seriesdescription=""),
            row("1.2.5", number_of_slices=0)]
    groups, summary = audit.find_candidates(rows)
    assert groups[0]["match"] == "possible_metadata"
    assert summary["skipped_missing_basic_metadata"] == 3


def test_tag_spacing_is_used_when_legacy_geometry_column_is_empty():
    a = row(pixelspacing=None)
    a["tags"]["PixelSpacing"] = [0.4, 0.4]
    b = dict(a, seriesinstanceuid="1.2.2")
    assert audit.find_candidates([a, b])[0][0]["match"] == "strong_metadata"


def test_subject_scope_requires_explicit_identity_not_patient_id():
    a, b = row(), row("1.3.1", studyinstanceuid="1.3")
    assert not audit.find_candidates([a, b])[0]
    assert len(audit.find_candidates([a, b], "subject")[0]) == 1
    b["subject_id"] = "precise__different_person"
    assert not audit.find_candidates([a, b], "subject")[0]
    a["subject_id"] = b["subject_id"] = None
    assert not audit.find_candidates([a, b], "subject")[0]


def dataset(series="1.2.1", pixel=7, z=0, big_endian=False):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRBigEndian if big_endian else ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = pydicom.uid.CTImageStorage
    meta.MediaStorageSOPInstanceUID = f"{series}.{z + 1}"
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = "1.2"
    ds.SeriesInstanceUID = series
    ds.InstanceNumber = z + 1
    ds.Rows = ds.Columns = 2
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.ImagePositionPatient = [0, 0, z]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.RescaleSlope = 1
    ds.RescaleIntercept = -1024
    ds.PixelData = np.full((2, 2), pixel, dtype=">u2" if big_endian else "<u2").tobytes()
    return ds


def test_pixel_fingerprint_ignores_ids_but_preserves_geometry_intensity_and_pixels():
    assert audit.instance_digest(dataset()) == audit.instance_digest(dataset("1.2.2", big_endian=True))
    assert audit.instance_digest(dataset()) != audit.instance_digest(dataset(z=1))
    assert audit.instance_digest(dataset()) != audit.instance_digest(dataset(pixel=8))
    changed = dataset()
    changed.RescaleIntercept = 0
    assert audit.instance_digest(dataset()) != audit.instance_digest(changed)
    changed = dataset()
    changed.AcquisitionTime = "001234"
    assert audit.instance_digest(dataset()) != audit.instance_digest(changed)


def write_series(tmp_path, uid, archive=False, pixels=(7, 8)):
    directory = tmp_path / uid
    directory.mkdir()
    payloads = []
    for z, pixel in enumerate(pixels):
        buffer = io.BytesIO()
        dataset(uid, pixel=pixel, z=z).save_as(buffer, enforce_file_format=True)
        payloads.append(buffer.getvalue())
        # Deliberately reverse filename order between series.
        (directory / f"{len(pixels) - z}.dcm").write_bytes(payloads[-1])
    result = row(uid, dicom_dir_path=str(directory))
    if archive:
        path = tmp_path / f"{uid}.tar.zst"
        with path.open("wb") as raw, zstandard.ZstdCompressor().stream_writer(raw) as stream:
            with tarfile.open(fileobj=stream, mode="w|") as tar:
                for i, payload in enumerate(reversed(payloads)):
                    member = tarfile.TarInfo(f"DICOM/{i}.dcm")
                    member.size = len(payload)
                    tar.addfile(member, io.BytesIO(payload))
        result["dicom_archive_path"] = str(path)
    return result


def test_full_verification_matches_archives_to_loose_files_and_handles_subgroups(tmp_path):
    a = write_series(tmp_path, "1.2.1")
    b = write_series(tmp_path, "1.2.2", archive=True)
    group = {"series": [a, b]}
    assert audit.verify_group(group)["status"] == "confirmed_pixel_match"
    c = write_series(tmp_path, "1.2.3", pixels=(7, 9))
    result = audit.verify_group({"series": [a, b, c]})
    assert result["status"] == "partial_pixel_match"
    assert result["matching_sets"] == [["1.2.1", "1.2.2"]]
    assert audit.verify_group({"series": [a, c]})["status"] == "pixel_difference"


def test_incomplete_series_and_wrong_uids_cannot_confirm(tmp_path):
    a = write_series(tmp_path, "1.2.1")
    b = write_series(tmp_path, "1.2.2", pixels=(7,))
    assert audit.verify_group({"series": [a, b]})["status"] == "incomplete"
    a["seriesinstanceuid"] = "1.2.99"
    assert "UID differs" in audit.fingerprint_series(a)["error"]


def test_missing_archive_does_not_fall_back_to_warm_files(tmp_path):
    a = write_series(tmp_path, "1.2.1")
    a["dicom_archive_path"] = str(tmp_path / "absent.tar.zst")
    assert audit.fingerprint_series(a)["status"] == "unavailable"


def test_bad_dicom_and_missing_pixels_are_reported(tmp_path):
    a = write_series(tmp_path, "1.2.1")
    path = next(Path(a["dicom_dir_path"]).iterdir())
    path.write_bytes(b"not dicom")
    assert audit.fingerprint_series(a)["status"] == "unavailable"
    ds = dataset()
    del ds.PixelData
    ds.save_as(path, enforce_file_format=True)
    assert audit.fingerprint_series(a)["status"] == "unavailable"


@pytest.fixture
def duplicate_db(test_db):
    import psycopg2

    with psycopg2.connect(**test_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dataset(slug,name) VALUES ('dup-a','DUP A'), ('dup-b','DUP B')")
        cur.execute("""INSERT INTO patient(patient_id,patient_key,subject_id,dataset)
            VALUES ('dup','dup-a__dup','dup-a__dup','DUP A'),
                   ('dup','dup-b__dup','dup-a__dup','DUP B')""")
        cur.execute("""INSERT INTO image_study(studyinstanceuid,patient_key,subject_id)
            VALUES ('dup-study','dup-a__dup','dup-a__dup')""")
        cur.execute("INSERT INTO study_member(studyinstanceuid,patient_key) VALUES ('dup-study','dup-b__dup')")
        for uid in ("dup-1", "dup-2"):
            cur.execute("""INSERT INTO image_series(seriesinstanceuid,studyinstanceuid,patient_key,
                subject_id,seriesdescription,modality,number_of_slices,imageshape)
                VALUES (%s,'dup-study','dup-a__dup','dup-a__dup','HEAD','CT',2,ARRAY[2,2,2])""", (uid,))
            cur.execute("INSERT INTO series_dicom_tags(seriesinstanceuid,tags) VALUES (%s, '{\"EchoTime\":2}')", (uid,))
    try:
        yield test_db
    finally:
        with psycopg2.connect(**test_db) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM series_dicom_tags WHERE seriesinstanceuid IN ('dup-1','dup-2')")
            cur.execute("DELETE FROM image_series WHERE studyinstanceuid='dup-study'")
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid='dup-study'")
            cur.execute("DELETE FROM patient WHERE patient_key IN ('dup-a__dup','dup-b__dup')")
            cur.execute("DELETE FROM dataset WHERE slug IN ('dup-a','dup-b')")


def test_fetch_rows_uses_visibility_without_counting_members_twice(duplicate_db, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_CONFIG", duplicate_db)
    rows = audit.fetch_rows("dup-study", "DUP B")
    assert {r["seriesinstanceuid"] for r in rows} == {"dup-1", "dup-2"}
    assert rows[0]["tags"] == {"EchoTime": 2}
    assert len(audit.find_candidates(rows)[0]) == 1
    assert audit.fetch_rows("dup-study", "nonexistent") == []


def test_cli_writes_review_reports_and_signals_incomplete_verification(tmp_path, monkeypatch):
    rows = [row(), row("1.2.2")]
    monkeypatch.setattr(audit, "fetch_rows", lambda *args: rows)
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--output", str(tmp_path)])
    assert audit.main() == 0
    import csv
    import json

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["summary"]["candidate_series"] == 2
    with (tmp_path / "series.csv").open() as fileobj:
        csv_rows = list(csv.DictReader(fileobj))
    assert len(csv_rows) == 2
    assert csv_rows[0]["group_id"] == csv_rows[1]["group_id"]
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--output", str(tmp_path), "--verify-pixels"])
    monkeypatch.setattr(audit, "fingerprint_series", lambda row: {"status": "unavailable", "error": "offline"})
    assert audit.main() == 2


def test_cli_marks_groups_beyond_verification_limit(tmp_path, monkeypatch):
    rows = [row(), row("1.2.2"), row("1.2.3", seriesdescription="another"),
            row("1.2.4", seriesdescription="another")]
    monkeypatch.setattr(audit, "fetch_rows", lambda *args: rows)
    monkeypatch.setattr(audit, "verify_group", lambda group: {"status": "confirmed_pixel_match"})
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--output", str(tmp_path), "--verify-pixels", "--max-verify-groups", "1"])
    assert audit.main() == 0
    import json

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["summary"]["verification"] == {"confirmed_pixel_match": 1, "not_checked_limit": 1}
