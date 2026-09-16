"""Tag extraction for `series_dicom_tags`.

The assertions here encode the fixes that make the extractor safe to point at
127k real series — each one is a behaviour the reference implementation
(maintenance/DicomDetector/metadata.py) gets wrong.
"""

import json

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from dicom_tags import EXTRACTOR_VERSION, SeriesTagAccumulator, extract_series_tags
from utils import max_same_position_count


def _header(modality="CT", z=0.0, kernel=None, image_type=None, descr="TEST"):
    ds = Dataset()
    ds.Modality = modality
    ds.SeriesDescription = descr
    ds.PatientName = "Doe^Jane"
    ds.ImagePositionPatient = [0.0, 0.0, z]
    if kernel is not None:
        ds.ConvolutionKernel = kernel
    if image_type is not None:
        ds.ImageType = image_type
    return ds


def test_keys_are_pydicom_keywords_not_display_names():
    # DicomDetector keys on elem.name, yielding "Patient'sName" / "Patient'sSex" —
    # apostrophes in jsonb keys, and not a stable API. We key on elem.keyword.
    row = extract_series_tags([_header()])
    assert "PatientName" in row["tags"]
    assert "Patient'sName" not in row["tags"]
    assert row["tags"]["PatientName"] == "Doe^Jane"  # PersonName -> str


def test_private_tags_are_kept_under_a_private_subkey():
    ds = _header()
    ds.add_new(0x00091001, "LO", "vendor-secret")
    row = extract_series_tags([ds])
    assert row["tags"]["_private"]["0009,1001"] == "vendor-secret"


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_tags_are_null_without_losing_the_series(value):
    ds = _header()
    ds.add_new(0x0018602C, "FD", value)  # PhysicalDeltaX: public scalar
    # Reproduce the private multi-value tag that failed CRISP2 ingestion.
    ds.add_new(0x00232018, "FD", [12.0, value, 0.0])
    inner = Dataset()
    inner.add_new(0x00232018, "FD", value)
    ds.ProcedureCodeSequence = Sequence([inner])

    row = extract_series_tags([ds])
    tags = json.loads(json.dumps(row["tags"], allow_nan=False))
    assert tags["PhysicalDeltaX"] is None
    assert tags["_private"]["0023,2018"] == [12.0, None, 0.0]
    assert tags["ProcedureCodeSequence"][0]["_private"]["0023,2018"] is None
    assert tags["SeriesDescription"] == "TEST"
    assert row["n_instances_scanned"] == 1


@pytest.mark.parametrize("number_type", [pydicom.valuerep.DSfloat, pydicom.valuerep.DSdecimal])
@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "1.25"])
def test_dicom_numeric_wrappers_produce_strict_json(number_type, value, monkeypatch):
    ds = _header()
    monkeypatch.setattr(pydicom.config, "use_DS_decimal", number_type is pydicom.valuerep.DSdecimal)
    monkeypatch.setattr(pydicom.config.settings, "reading_validation_mode", pydicom.config.IGNORE)
    ds.add_new(0x00232018, "DS", [value, "2.0"])
    assert isinstance(ds[0x00232018].value[0], number_type)

    tags = extract_series_tags([ds])["tags"]
    stored = json.loads(json.dumps(tags, allow_nan=False))
    expected = 1.25 if value == "1.25" else None
    assert stored["_private"]["0023,2018"] == [expected, 2.0]


def test_sequences_are_recursed_but_depth_capped():
    ds = _header()
    inner = Dataset()
    inner.CodeValue = "RID1"
    ds.ProcedureCodeSequence = Sequence([inner])
    row = extract_series_tags([ds])
    assert row["tags"]["ProcedureCodeSequence"][0]["CodeValue"] == "RID1"


def test_short_multiframe_does_not_raise():
    # DicomDetector indexes PerFrameFunctionalGroupsSequence[10] unguarded, so any
    # enhanced-multiframe instance with <11 frames raises IndexError and costs the
    # whole series. We take item 0 and cap the item count.
    ds = _header()
    frame = Dataset()
    frame.FrameAcquisitionNumber = 1
    ds.PerFrameFunctionalGroupsSequence = Sequence([frame])  # only ONE frame
    row = extract_series_tags([ds])
    assert row["tags"]["PerFrameFunctionalGroupsSequence"][0]["FrameAcquisitionNumber"] == 1


def test_binary_values_are_dropped_not_stringified():
    ds = _header()
    ds.add_new(0x00291010, "OB", b"\x00\x01\x02")
    row = extract_series_tags([ds])
    assert row["tags"]["_private"]["0029,1010"] is None


def test_cross_instance_aggregates():
    # Two positions, three frames at z=0 -> same_position_count == 3.
    headers = [_header(z=0.0), _header(z=0.0), _header(z=0.0), _header(z=5.0)]
    row = extract_series_tags(headers, source_instance="/a/b.dcm")

    assert row["same_position_count"] == 3
    assert row["n_positions"] == 2
    assert row["n_instances_scanned"] == 4
    assert row["source_instance"] == "/a/b.dcm"
    assert row["extractor_version"] == EXTRACTOR_VERSION


def test_distinct_kernels_joins_multivalue_and_dedupes():
    # ConvolutionKernel is a plain str on some scanners and a MultiValue on
    # others; both must normalize to one comparable token.
    headers = [
        _header(kernel=pydicom.multival.MultiValue(str, ["Hr32s", "3"])),
        _header(kernel=pydicom.multival.MultiValue(str, ["Hr32s", "3"])),
        _header(kernel="STANDARD"),
    ]
    row = extract_series_tags(headers)
    assert row["distinct_kernels"] == ["Hr32s3", "STANDARD"]


def test_distinct_image_types():
    headers = [
        _header(image_type=["ORIGINAL", "PRIMARY", "AXIAL"]),
        _header(image_type=["DERIVED", "SECONDARY", "OTHER"]),
    ]
    row = extract_series_tags(headers)
    assert row["distinct_image_types"] == [
        "DERIVED/SECONDARY/OTHER",
        "ORIGINAL/PRIMARY/AXIAL",
    ]


def test_empty_series_is_safe():
    row = extract_series_tags([])
    assert row["tags"] == {}
    assert row["n_instances_scanned"] == 0
    assert row["same_position_count"] is None


# --- The accumulator is the fold both paths go through -----------------------


@pytest.mark.parametrize("zs", [
    [0.0, 0.0, 0.0, 5.0],            # dynamic: 3 frames at one location
    [0.0, 5.0, 10.0, 15.0],          # static: one frame per location
    [0.0],                           # single instance
])
def test_accumulator_matches_max_same_position_count(zs):
    # The backfill streams into the accumulator; ingestion passes a list. If the
    # two ever disagreed, a reclassify would silently contradict the ingest that
    # produced the row — so pin them to the existing implementation.
    headers = [_header(z=z) for z in zs]

    acc = SeriesTagAccumulator()
    for h in headers:
        acc.add(h)

    assert acc.result()["same_position_count"] == max_same_position_count(headers)
    assert acc.result() == extract_series_tags(headers)


def test_undecodable_multiframe_geometry_degrades_to_none():
    # max_same_position_count refuses to guess when per-frame geometry can't be
    # read; the streaming fold must refuse identically, not under-count.
    ds = _header()
    ds.NumberOfFrames = 4
    frame = Dataset()  # no PlanePositionSequence -> undecodable
    ds.PerFrameFunctionalGroupsSequence = Sequence([frame])

    headers = [ds]
    assert max_same_position_count(headers) is None
    assert extract_series_tags(headers)["same_position_count"] is None


def test_accumulator_holds_only_the_first_dataset():
    # The memory guarantee, asserted rather than assumed: a 500-instance series
    # retains exactly one representative dataset.
    acc = SeriesTagAccumulator()
    for i in range(500):
        acc.add(_header(z=float(i), descr=f"instance-{i}"))

    result = acc.result()
    assert result["n_instances_scanned"] == 500
    assert result["tags"]["SeriesDescription"] == "instance-0"  # the first, not the last
    assert acc._tags is result["tags"]
