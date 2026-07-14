"""Tests for geometry-first series-type detection (CTP / PWI / DWI).

Covers the pure detectors and max_same_position_count in utils.py, plus the
end-to-end wiring through ImageIngestionProtocol.create_series_table.

Run with: pytest tests/test_series_type.py
"""

import os

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from image_ingestion_protocol import ImageIngestionProtocol
from series_classification import RULES_VERSION
from utils import (
    identify_series_type,
    is_ctp_series,
    is_dwi_series,
    is_pwi_series,
    max_same_position_count,
)

STUDY_UID = "1.2.3.999.1"


def _header(modality="MR", series_desc="TEST", z=0.0, instance=1,
            series_uid="1.2.3.10", n_frames=None):
    ds = Dataset()
    ds.PatientID = "11-002"
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = generate_uid()
    ds.Modality = modality
    ds.SeriesDescription = series_desc
    ds.InstanceNumber = instance
    ds.SeriesNumber = 100
    ds.Rows = 4
    ds.Columns = 4
    ds.SliceThickness = 5.0
    ds.ImagePositionPatient = [0.0, 0.0, float(z)]
    if n_frames is not None:
        ds.NumberOfFrames = n_frames
    return ds


def _multiframe_header(z_positions, modality="MR", series_desc="TEST"):
    """Enhanced multiframe dataset with one frame per z in z_positions."""
    ds = _header(modality=modality, series_desc=series_desc, n_frames=len(z_positions))
    del ds.ImagePositionPatient  # per-frame, not at the top level
    frames = Sequence()
    for z in z_positions:
        plane = Dataset()
        plane.ImagePositionPatient = [0.0, 0.0, float(z)]
        frame = Dataset()
        frame.PlanePositionSequence = Sequence([plane])
        frames.append(frame)
    ds.PerFrameFunctionalGroupsSequence = frames
    return ds


# --- pure detector tests -----------------------------------------------------

def test_is_ctp_requires_ct_and_many_frames():
    assert is_ctp_series("CT", 20)
    assert is_ctp_series("CT", 15)
    assert is_ctp_series("CT", 14)            # his floor is 14, not 15
    assert not is_ctp_series("CT", 13)        # below floor
    assert not is_ctp_series("MR", 20)        # wrong modality
    assert not is_ctp_series("CT", None)      # undetermined


def test_is_pwi_requires_mr_and_many_frames_and_not_excluded():
    assert is_pwi_series("MR", 30)
    assert is_pwi_series("MR", 15)
    assert not is_pwi_series("MR", 14)
    assert not is_pwi_series("CT", 30)
    assert not is_pwi_series("MR", 30, "ASL perfusion")   # excluded token
    assert not is_pwi_series("MR", 30, "resting fMRI")


def test_is_dwi_requires_mr_and_few_frames():
    assert is_dwi_series("MR", 2)
    assert is_dwi_series("MR", 14)
    assert not is_dwi_series("MR", 1)         # static
    assert not is_dwi_series("MR", 15)        # that's perfusion territory
    assert not is_dwi_series("CT", 4)
    assert not is_dwi_series("MR", 4, "QSM map")


def test_identify_series_type_dispatch_and_boundaries():
    assert identify_series_type("CT", 25) == "CTP"
    assert identify_series_type("MR", 25) == "PWI"
    assert identify_series_type("MR", 6) == "DWI"
    # boundary: 14 -> DWI, 15 -> PWI (no overlap)
    assert identify_series_type("MR", 14) == "DWI"
    assert identify_series_type("MR", 15) == "PWI"
    # static / unknown -> None
    assert identify_series_type("CT", 1) is None
    assert identify_series_type("MR", 1) is None
    assert identify_series_type("CT", None) is None
    assert identify_series_type("OT", 30) is None


# --- max_same_position_count tests -------------------------------------------

def test_count_classic_single_frame_dynamic():
    # 8 timepoints at each of 3 slice positions -> max count is 8.
    headers = []
    inst = 1
    for z in (0.0, 5.0, 10.0):
        for _ in range(8):
            headers.append(_header(z=z, instance=inst))
            inst += 1
    assert max_same_position_count(headers) == 8


def test_count_static_scan_is_one():
    headers = [_header(z=z, instance=i) for i, z in enumerate((0.0, 5.0, 10.0, 15.0))]
    assert max_same_position_count(headers) == 1


def test_count_none_without_positions():
    h = _header()
    del h.ImagePositionPatient
    assert max_same_position_count([h]) is None
    assert max_same_position_count([]) is None


def test_count_enhanced_multiframe_decoded():
    # single multiframe file: 12 frames, all at one position -> count 12.
    hdr = _multiframe_header([0.0] * 12)
    assert max_same_position_count([hdr]) == 12


def test_count_multiframe_undecodable_degrades_to_none():
    # NumberOfFrames>1 but no PerFrameFunctionalGroupsSequence -> don't guess.
    h = _header(n_frames=20)
    del h.ImagePositionPatient
    assert max_same_position_count([h]) is None


# --- end-to-end through create_series_table ----------------------------------

def _write(path, ds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.SOPClassUID = fm.MediaStorageSOPClassUID
    ds.file_meta = fm
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    pydicom.dcmwrite(path, ds, write_like_original=False)


def test_create_series_table_assigns_geometric_types(tmp_path):
    case = tmp_path / "case"
    ctp_uid, dwi_uid, static_uid = "1.2.3.1", "1.2.3.2", "1.2.3.3"

    # CTP: CT, 20 frames at one position.
    for i in range(20):
        _write(str(case / "ctp" / f"c{i}.dcm"),
               _header(modality="CT", series_desc="PERFUSION", z=0.0,
                       instance=i + 1, series_uid=ctp_uid))
    # DWI: MR, 4 frames at one position.
    for i in range(4):
        _write(str(case / "dwi" / f"d{i}.dcm"),
               _header(modality="MR", series_desc="DIFFUSION", z=0.0,
                       instance=i + 1, series_uid=dwi_uid))
    # Static CT, one frame per position: geometry cannot type it, so the
    # description/kernel stage does. This used to assert None — CTA detection was
    # retired as untuned, and identify_series_type could not emit it. It now
    # resolves via series_classification, and the ingest path must reflect that.
    for i in range(5):
        _write(str(case / "cta" / f"a{i}.dcm"),
               _header(modality="CT", series_desc="CTA", z=float(i * 5),
                       instance=i + 1, series_uid=static_uid))

    p = ImageIngestionProtocol(case_dir=str(case), postgres_engine=None)
    p.create_series_table()
    t = p.case_series_table.set_index("seriesinstanceuid")

    assert t.loc[ctp_uid, "series_type"] == "CTP"
    assert t.loc[dwi_uid, "series_type"] == "DWI"

    # Only 5 instances — below his CTA minimum of 80, so it is excluded, and the
    # rule says exactly why.
    assert t.loc[static_uid, "series_type"] is None
    assert t.loc[static_uid, "series_type_rule"] == "description-cta-below-min-instances"

    assert t.loc[ctp_uid, "series_type_rule"] == "geometry-same-position-count"
    assert t.loc[ctp_uid, "series_type_version"] == RULES_VERSION

    # The tag rows are captured in the same pass, keyed by series UID.
    tags = p.case_series_tags_table.set_index("seriesinstanceuid")
    assert set(tags.index) == {ctp_uid, dwi_uid, static_uid}
    assert tags.loc[ctp_uid, "same_position_count"] == 20
    assert tags.loc[ctp_uid, "tags"]["Modality"] == "CT"


def test_series_geometry_uses_true_z_extent(tmp_path):
    # 4 slices 5mm apart -> coverage = (15 - 0) + slice_thickness(5) = 20.
    # This only holds if _series_geometry reads the (MultiValue) positions.
    headers = [_header(z=float(i * 5), instance=i + 1) for i in range(4)]
    _, slice_thickness, coverage = ImageIngestionProtocol._series_geometry(headers)
    assert slice_thickness == 5.0
    assert coverage == pytest.approx(20.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
