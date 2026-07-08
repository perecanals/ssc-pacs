"""Tests pinning the required-column checkers' behavior and error texts.

The checkers gate ingestion on migrations having run; their error messages
are operator-facing. Monkeypatches SQLAlchemy's inspect — no DB.

Run with: pytest tests/test_required_columns.py
"""

import pytest

import image_ingestion_protocol as iip_mod
from image_ingestion_protocol import ImageIngestionProtocol

ALL_COLUMNS = ["import_id", "import_label", "number_of_slices"]


class _FakeInspector:
    def __init__(self, columns_by_table):
        self._cols = columns_by_table

    def get_columns(self, table):
        return [{"name": c} for c in self._cols.get(table, [])]


def _protocol(monkeypatch, series_cols=ALL_COLUMNS, study_cols=ALL_COLUMNS):
    inspector = _FakeInspector(
        {"image_series": series_cols, "image_study": study_cols})
    monkeypatch.setattr(iip_mod, "inspect", lambda engine: inspector)
    proto = object.__new__(ImageIngestionProtocol)
    proto.postgres_engine = object()
    return proto


def test_all_present_passes(monkeypatch):
    proto = _protocol(monkeypatch)
    proto._require_import_id_columns()
    proto._require_import_label_columns()
    proto._require_number_of_slices_column()


def test_import_id_missing_from_one_table(monkeypatch):
    proto = _protocol(monkeypatch, series_cols=["import_label"])
    with pytest.raises(ValueError) as exc:
        proto._require_import_id_columns()
    assert str(exc.value) == (
        "Missing required import_id column in image_series. "
        "Run the import_id rename migration before executing the protocol."
    )


def test_import_id_missing_from_both_tables(monkeypatch):
    proto = _protocol(monkeypatch, series_cols=[], study_cols=[])
    with pytest.raises(ValueError, match="image_series, image_study"):
        proto._require_import_id_columns()


def test_import_label_missing(monkeypatch):
    proto = _protocol(monkeypatch, study_cols=["import_id"])
    with pytest.raises(ValueError) as exc:
        proto._require_import_label_columns()
    assert str(exc.value) == (
        "Missing required import_label column in image_study. "
        "Run the import_label migration before executing the protocol."
    )


def test_number_of_slices_missing(monkeypatch):
    proto = _protocol(monkeypatch, series_cols=["import_id", "import_label"])
    with pytest.raises(ValueError) as exc:
        proto._require_number_of_slices_column()
    assert str(exc.value) == (
        "Missing required number_of_slices column in image_series. "
        "Run: ALTER TABLE image_series ADD COLUMN IF NOT EXISTS number_of_slices INTEGER;"
    )


def test_get_next_import_id_requires_columns(monkeypatch):
    _protocol(monkeypatch, series_cols=[])  # patches inspect
    with pytest.raises(ValueError, match="Missing required import_id column"):
        ImageIngestionProtocol.get_next_import_id(object())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
