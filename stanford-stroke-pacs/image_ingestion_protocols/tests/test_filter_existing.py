"""Tests for the append-only / drift filter (filter_existing_studies).

With overwrite_if_exists=False, series already in image_series are skipped
(exposed via skipped_existing_series_uids for the executor's resume-boundary
re-index), NULL-slice series are skipped as unverifiable, slice-count drift
triggers a wipe-and-re-ingest of just that series, and brand-new series under
an existing study are appended. DB access is monkeypatched — no database.

Run with: pytest tests/test_filter_existing.py
"""

import pandas as pd
import pytest

from image_ingestion_protocol import ImageIngestionProtocol

STUDY = "1.2.3.100"


def _protocol(tmp_path, db_study_rows, db_series_rows):
    proto = object.__new__(ImageIngestionProtocol)
    proto.base_dir = str(tmp_path / "dicom_root")
    proto.cold_archive_root = str(tmp_path / "cold_root")

    def fake_load(study_uids, include_series=False):
        proto.image_study = pd.DataFrame(
            db_study_rows, columns=["studyinstanceuid", "patient_id", "study_path"])
        proto.image_series = pd.DataFrame(
            db_series_rows,
            columns=["studyinstanceuid", "seriesinstanceuid", "dicom_dir_path",
                     "dicom_archive_path", "number_of_slices"])

    proto._load_case_rows_from_db = fake_load
    return proto


def _disk_tables(series):
    """(case_series_table, case_study_table) for [(series_uid, n_slices)]."""
    series_table = pd.DataFrame(
        [{"patient_id": "11-001", "studyinstanceuid": STUDY,
          "seriesinstanceuid": uid, "number_of_slices": n}
         for uid, n in series])
    study_table = pd.DataFrame(
        [{"patient_id": "11-001", "studyinstanceuid": STUDY}])
    return series_table, study_table


def test_matching_series_skipped_and_study_row_dropped(tmp_path):
    proto = _protocol(
        tmp_path,
        db_study_rows=[(STUDY, "11-001", "/x")],
        db_series_rows=[(STUDY, "s1", "/x/s1", None, 3)],
    )
    proto.case_series_table, proto.case_study_table = _disk_tables([("s1", 3)])

    proto.filter_existing_studies(overwrite_if_exists=False)

    assert proto.skipped_existing_series_uids == ["s1"]
    assert proto.case_series_table.empty
    # The existing study row must not be re-upserted (would clobber its
    # persisted import_id / study_path / acquisitiondatetime).
    assert proto.case_study_table.empty


def test_null_slice_count_is_unverifiable_and_skipped(tmp_path, capsys):
    proto = _protocol(
        tmp_path,
        db_study_rows=[(STUDY, "11-001", "/x")],
        db_series_rows=[(STUDY, "s1", "/x/s1", None, None)],
    )
    proto.case_series_table, proto.case_study_table = _disk_tables([("s1", 3)])

    proto.filter_existing_studies(overwrite_if_exists=False)

    assert proto.skipped_existing_series_uids == ["s1"]
    assert "cannot verify drift" in capsys.readouterr().out


def test_drift_series_reingested_with_old_files_wiped(tmp_path):
    old_dir = tmp_path / "dicom_root" / "11-001" / STUDY / "desc" / "s1" / "DICOM"
    old_dir.mkdir(parents=True)
    (old_dir / "IM-0.dcm").write_bytes(b"x")
    old_archive = tmp_path / "cold_root" / "11-001" / STUDY / "desc" / "s1" / "DICOM.tar.zst"
    old_archive.parent.mkdir(parents=True)
    old_archive.write_bytes(b"zzz")

    proto = _protocol(
        tmp_path,
        db_study_rows=[(STUDY, "11-001", "/x")],
        db_series_rows=[(STUDY, "s1", str(old_dir), str(old_archive), 3)],
    )
    # Disk now has 5 slices for s1 -> drift.
    proto.case_series_table, proto.case_study_table = _disk_tables([("s1", 5)])

    proto.filter_existing_studies(overwrite_if_exists=False)

    assert proto.skipped_existing_series_uids == []
    assert list(proto.case_series_table["seriesinstanceuid"]) == ["s1"]  # re-ingested
    assert not old_dir.exists()
    assert not old_archive.exists()


def test_drift_wipe_refuses_out_of_root_paths(tmp_path):
    # DB rows pointing outside base_dir / cold_archive_root must never be
    # deleted, even on the drift path.
    outside_dir = tmp_path / "elsewhere" / "DICOM"
    outside_dir.mkdir(parents=True)
    (outside_dir / "IM-0.dcm").write_bytes(b"x")
    outside_archive = tmp_path / "elsewhere" / "DICOM.tar.zst"
    outside_archive.write_bytes(b"zzz")

    proto = _protocol(
        tmp_path,
        db_study_rows=[(STUDY, "11-001", "/x")],
        db_series_rows=[(STUDY, "s1", str(outside_dir), str(outside_archive), 3)],
    )
    proto.case_series_table, proto.case_study_table = _disk_tables([("s1", 5)])

    proto.filter_existing_studies(overwrite_if_exists=False)

    assert outside_dir.exists()
    assert (outside_dir / "IM-0.dcm").exists()
    assert outside_archive.exists()


def test_new_series_under_existing_study_is_appended(tmp_path):
    proto = _protocol(
        tmp_path,
        db_study_rows=[(STUDY, "11-001", "/x")],
        db_series_rows=[(STUDY, "s1", "/x/s1", None, 3)],
    )
    proto.case_series_table, proto.case_study_table = _disk_tables(
        [("s1", 3), ("s2", 7)])

    proto.filter_existing_studies(overwrite_if_exists=False)

    assert proto.skipped_existing_series_uids == ["s1"]
    assert list(proto.case_series_table["seriesinstanceuid"]) == ["s2"]
    assert proto.case_study_table.empty  # study row still dropped


def test_brand_new_study_passes_through_untouched(tmp_path):
    proto = _protocol(tmp_path, db_study_rows=[], db_series_rows=[])
    proto.case_series_table, proto.case_study_table = _disk_tables(
        [("s1", 3), ("s2", 7)])

    proto.filter_existing_studies(overwrite_if_exists=False)

    assert proto.skipped_existing_series_uids == []
    assert len(proto.case_series_table) == 2
    assert len(proto.case_study_table) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
