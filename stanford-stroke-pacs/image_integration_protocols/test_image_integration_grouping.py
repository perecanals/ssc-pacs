"""Focused tests for SeriesInstanceUID-based grouping in the integration protocol.

The protocol groups DICOM files by their embedded SeriesInstanceUID (not by
directory), so that "mixed" folders are split into their true series and a
series scattered across folders is merged into one row. These tests build tiny
synthetic DICOM trees and exercise create_series_table /
add_paths_and_copy_dicom_files without a database.

Run with: pytest image_integration_protocols/test_image_integration_grouping.py
"""

import os

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from image_integration_protocol import ImageIntegrationProtocol

STUDY_UID = "1.2.3.999.1"


def _write_dcm(path, series_uid, series_number, instance_number,
               study_uid=STUDY_UID, series_desc="TEST", patient_id="11-002"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = Dataset()
    ds.PatientID = patient_id
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"  # MR Image Storage
    ds.SeriesNumber = series_number
    ds.InstanceNumber = instance_number
    ds.SeriesDescription = series_desc
    ds.StudyDescription = "STUDY"
    ds.Modality = "MR"
    ds.Rows = 4
    ds.Columns = 4
    ds.SliceThickness = 5.0
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    pydicom.dcmwrite(path, ds, write_like_original=False)


def _protocol(case_dir):
    return ImageIntegrationProtocol(case_dir=str(case_dir), postgres_engine=None)


def test_mixed_folder_splits_and_split_series_merges(tmp_path):
    case = tmp_path / "11002"
    uid_x, uid_y, uid_z, uid_w = (f"1.2.3.{n}" for n in (10, 20, 30, 40))

    # Series X lives mostly in its own folder (3 instances) ...
    for i in range(1, 4):
        _write_dcm(case / "real_home" / f"x{i}.dcm", uid_x, 100, i, series_desc="SERIES_X")
    # ... with one stray instance mis-filed into a mixed folder that also holds
    # a single instance of series Y. Same-UID files must MERGE; different-UID
    # files in the same folder must SPLIT.
    _write_dcm(case / "mixed" / "x_stray.dcm", uid_x, 100, 4, series_desc="SERIES_X")
    _write_dcm(case / "mixed" / "y1.dcm", uid_y, 200, 1, series_desc="SERIES_Y")
    # A localizer-style folder holding two distinct single-image series.
    _write_dcm(case / "localizers" / "z.dcm", uid_z, 300, 1, series_desc="SERIES_Z")
    _write_dcm(case / "localizers" / "w.dcm", uid_w, 400, 1, series_desc="SERIES_W")

    p = _protocol(case)
    p.create_series_table()
    t = p.case_series_table

    # Four real series, unique keys, nothing dropped.
    assert len(t) == 4
    assert t["seriesinstanceuid"].is_unique
    assert set(t["seriesinstanceuid"]) == {uid_x, uid_y, uid_z, uid_w}

    # Series X merged across folders: 3 + 1 = 4 instances.
    row_x = t[t["seriesinstanceuid"] == uid_x].iloc[0]
    assert row_x["number_of_slices"] == 4
    assert len(row_x["src_file_paths"]) == 4
    # Total instances preserved across all series (X=4, Y=1, Z=1, W=1).
    assert int(t["number_of_slices"].sum()) == 7


def test_collision_on_divergent_series_number_warns_but_keeps_one_row(tmp_path, capsys):
    case = tmp_path / "case"
    uid = "1.2.3.50"
    # Two files share a SeriesInstanceUID but disagree on SeriesNumber — a
    # standard violation. We keep them merged and warn loudly.
    _write_dcm(case / "a" / "f1.dcm", uid, 11, 1)
    _write_dcm(case / "b" / "f2.dcm", uid, 22, 1)

    p = _protocol(case)
    p.create_series_table()
    out = capsys.readouterr().out

    assert len(p.case_series_table) == 1
    assert p.case_series_table.iloc[0]["number_of_slices"] == 2
    assert "suspected true SeriesInstanceUID collision" in out


def test_copy_handles_basename_collision(tmp_path):
    case = tmp_path / "case"
    uid = "1.2.3.60"
    # Two source folders contribute a file with the SAME basename to one series.
    _write_dcm(case / "a" / "image.dcm", uid, 5, 1)
    _write_dcm(case / "b" / "image.dcm", uid, 5, 2)

    p = _protocol(case)
    p.base_dir = str(tmp_path / "dest")
    p.create_series_table()
    p.create_study_table()
    p.add_paths_and_copy_dicom_files()

    row = p.case_series_table.iloc[0]
    pairs = row["copied_pairs"]
    assert len(pairs) == 2
    dests = {os.path.basename(dst) for _src, dst in pairs}
    assert len(dests) == 2  # collision was renamed, nothing overwritten
    assert len(p._visible_files(row["dicom_dir_path"])) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
