from datetime import datetime, timedelta

import pytest

from autolabels.crisp2 import classify, classify_v2
from autolabels.model import Study

SCAN = datetime(2025, 1, 10, 12)


def at(offset):
    return (SCAN - timedelta(minutes=offset)).isoformat()


@pytest.mark.parametrize(
    "arrival,referring,receiving,puncture,expected",
    [
        (-1441, None, None, None, "BL"),
        (-1441, 60, None, None, "BLO"),
        (-1440, None, None, None, "BLO"),
        (-31, None, None, None, "BLO"),
        (-30, None, None, None, "BL"),
        (-16, None, 0, None, "BL"),
        (-15, None, -30, None, "BLT"),
        (-1, 60, -30, None, "BL"),
        (0, None, None, -1, "BLT"),
        (0, None, None, 0, "BL"),
        (1440, None, None, None, "BLT"),
        (1441, None, None, None, "BL"),
        (1441, None, None, -1, "BLT"),
        (1, -60, None, None, "BL"),
    ],
)
def test_boundaries(arrival, referring, receiving, puncture, expected):
    fields = dict(
        receiving_arrival_time=arrival,
        imaging_refer_bl_time=referring,
        imaging_receiv_bl_time=receiving,
        femoral_sheath_time=puncture,
        onset_time=3000,
    )
    clinical = {k: at(v) for k, v in fields.items() if v is not None}
    assert classify(Study("BL", SCAN, clinical)).value == expected


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"was_pt_transfer": 0, "time_normal": at(0)}, "BLT"),
        ({"was_pt_transfer": 0}, "BL"),
        ({"was_pt_transfer": 1, "time_normal": at(0)}, "BL"),
        ({"was_pt_transfer": 0, "time_normal": at(0), "imaging_refer_bl_time": at(0)}, "BL"),
        ({"onset_time": at(0), "time_recognized": at(-1)}, "PRE"),
        ({"time_normal": at(10), "onset_time": at(-1)}, "PRE"),
        ({"receiving_arrival_time": at(-1441), "imaging_refer_bl_time": at(0)}, "BL"),
        ({"receiving_arrival_time": at(-1440)}, "BLO"),
    ],
)
def test_incomplete_timeline(fields, expected):
    assert classify(Study("BL", SCAN, fields)).value == expected
    for general in ("FU", "THROMBECTOMY", None):
        assert classify(Study(general, SCAN, fields)).value == general


@pytest.mark.parametrize("minutes,puncture,expected", [
    (719, None, "BLT"), (720, None, "BLT"), (720.01, None, "FU"),
    (1440, None, "FU"), (1441, None, "FU"), (721, -10, "BLT"),
])
def test_v2_post_arrival_cutoff(minutes, puncture, expected):
    fields = {"receiving_arrival_time": at(minutes), "time_recognized": at(3000)}
    if puncture is not None:
        fields["femoral_sheath_time"] = at(puncture)
    assert classify_v2(Study("BL", SCAN, fields)).value == expected
    assert classify_v2(Study("FU", SCAN, fields)).value == "FU"
