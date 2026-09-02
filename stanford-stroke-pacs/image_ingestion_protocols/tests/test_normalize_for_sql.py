"""Tests pinning _normalize_for_sql's null-sentinel coercion.

Regression cover for the 2026-08-15 batch, where 11 cases rolled back with
`InvalidDatetimeFormat: invalid input syntax for type timestamp: "NaT"`. Every
one carried a Horos/OsiriX "OsiriX Annotations SR" series, which has no usable
date tags, so _parse_datetime returned pd.NaT by design — and pd.NaT slipped
past the isinstance(pd.Timestamp) guard into psycopg2, which adapted it (NaTType
subclasses datetime) to the literal 'NaT'.

No DB: this is pure value coercion.

Run with: pytest tests/test_normalize_for_sql.py
"""

import datetime

import numpy as np
import pandas as pd

from image_ingestion_protocol import ImageIngestionProtocol

normalize = ImageIngestionProtocol._normalize_for_sql


class TestNullSentinels:
    """Anything null-ish must become None, whatever flavor of null it is."""

    def test_nat_becomes_none(self):
        # The exact value that broke the 2026-08-15 run.
        assert normalize(pd.NaT) is None

    def test_nat_is_not_a_timestamp_instance(self):
        # Pins the language fact the original bug rested on: an
        # isinstance(pd.Timestamp) guard alone can never catch pd.NaT.
        assert not isinstance(pd.NaT, pd.Timestamp)
        assert isinstance(pd.NaT, datetime.datetime)

    def test_numpy_nat_becomes_none(self):
        assert normalize(np.datetime64("NaT")) is None

    def test_none_stays_none(self):
        assert normalize(None) is None

    def test_float_nan_becomes_none(self):
        assert normalize(float("nan")) is None

    def test_numpy_nan_becomes_none(self):
        # np.float64 is an np.generic; .item() alone would hand SQL a bare NaN.
        assert normalize(np.float64("nan")) is None

    def test_pandas_na_becomes_none(self):
        assert normalize(pd.NA) is None

    def test_empty_series_cell_becomes_none(self):
        # How a NaT actually arrives: via DataFrame.to_dict(orient="records").
        frame = pd.DataFrame({"acquisitiondatetime": [pd.NaT]})
        record = frame.to_dict(orient="records")[0]
        assert normalize(record["acquisitiondatetime"]) is None


class TestRealValuesSurvive:
    """Coercion must not damage the values that are actually present."""

    def test_timestamp_becomes_datetime(self):
        value = normalize(pd.Timestamp("2025-11-22 04:31:56"))
        assert value == datetime.datetime(2025, 11, 22, 4, 31, 56)
        assert type(value) is datetime.datetime

    def test_numpy_datetime64_becomes_datetime(self):
        value = normalize(np.datetime64("2025-11-22T04:31:56"))
        assert value == datetime.datetime(2025, 11, 22, 4, 31, 56)
        assert type(value) is datetime.datetime

    def test_numpy_scalars_unwrap_to_python(self):
        assert normalize(np.int64(45)) == 45
        assert type(normalize(np.int64(45))) is int
        assert normalize(np.float64(1.566)) == 1.566
        assert type(normalize(np.float64(1.566))) is float

    def test_strings_and_zero_pass_through(self):
        assert normalize("OsiriX Annotations SR") == "OsiriX Annotations SR"
        assert normalize(0) == 0
        assert normalize(False) is False
        assert normalize("") == ""


class TestSequences:
    """imageshape/pixelspacing bind as arrays; nulls inside them must coerce too."""

    def test_list_elements_are_normalized(self):
        assert normalize([np.int64(1024), np.int64(1024), np.int64(1)]) == [1024, 1024, 1]

    def test_list_with_nan_element(self):
        assert normalize([np.float64(0.5), float("nan")]) == [0.5, None]

    def test_ndarray_becomes_list(self):
        # pd.isna() on an array returns an elementwise array — the guard must
        # not try to take its truth value.
        assert normalize(np.array([1024, 1024, 1])) == [1024, 1024, 1]

    def test_nested_list(self):
        assert normalize([[np.int64(1), pd.NaT]]) == [[1, None]]


class TestSeriesRecordRoundTrip:
    """The failing row shape from the 2026-08-15 log, end to end."""

    def test_osirix_sr_row_binds_clean(self):
        row = {
            "patient_id": "2-430",
            "acquisitiondatetime": pd.NaT,
            "acquisitiondatetime_source": None,
            "seriesdescription": "OsiriX Annotations SR",
            "modality": "SR",
            "manufacturer": "Horos",
            "seriesnumber": np.int64(5004),
            "instancenumber": np.int64(0),
            "number_of_slices": np.int64(1),
            "pixelspacing": None,
            "imageshape": None,
            "slicethickness": None,
            "compressed_size_mb": np.float64(0.001),
            "series_type_rule": "unhandled-modality-sr",
        }
        normalized = {k: normalize(v) for k, v in row.items()}

        assert normalized["acquisitiondatetime"] is None
        assert normalized["seriesnumber"] == 5004
        assert normalized["instancenumber"] == 0
        assert normalized["compressed_size_mb"] == 0.001
        assert normalized["seriesdescription"] == "OsiriX Annotations SR"
        # Nothing pandas- or numpy-flavored may reach psycopg2.
        for key, value in normalized.items():
            assert not isinstance(value, (pd.Timestamp, np.generic)), key
            assert value is not pd.NaT, key
