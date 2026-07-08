"""Unit tests for the `_orphan_warming_dirs` scan of cold_storage_health.py.

Regression: the probe (timer-wired) used an unbounded ``rglob("*.warming")``
that descended into every extracted DICOM payload dir and hung for minutes.
The scan is now depth-bounded and dirs-only; these tests pin that behavior
with a synthetic warm-cache tree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "cold_storage"
    / "cold_storage_health.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("cold_storage_health", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


csh = _load_module()


def _make_series(root: Path, patient: str, study: str, desc: str, suid: str) -> Path:
    series_dir = root / patient / study / desc / suid
    dicom = series_dir / "DICOM"
    dicom.mkdir(parents=True)
    for i in range(5):
        (dicom / f"IM-{i}.dcm").write_bytes(b"x")
    return series_dir


def test_finds_warming_dir_at_series_depth(tmp_path):
    series_dir = _make_series(tmp_path, "4-0551", "STUDY_A", "Ax CTA", "SER_A")
    warming = series_dir / "DICOM.warming"
    warming.mkdir()
    (warming / "IM-0.dcm").write_bytes(b"x")  # partial extraction contents

    assert csh._orphan_warming_dirs(tmp_path) == [str(warming)]


def test_empty_when_no_warming_dirs(tmp_path):
    _make_series(tmp_path, "4-0551", "STUDY_A", "Ax CTA", "SER_A")
    assert csh._orphan_warming_dirs(tmp_path) == []


def test_missing_root_returns_empty(tmp_path):
    assert csh._orphan_warming_dirs(tmp_path / "nope") == []


def test_ignores_warming_named_files(tmp_path):
    series_dir = _make_series(tmp_path, "4-0551", "STUDY_A", "Ax CTA", "SER_A")
    (series_dir / "note.warming").write_text("not a dir")
    assert csh._orphan_warming_dirs(tmp_path) == []


def test_does_not_descend_into_dicom_payload(tmp_path):
    """A .warming nested deeper than the scan bound is not reported (and,
    critically, the scan never lists DICOM payload file entries)."""
    series_dir = _make_series(tmp_path, "4-0551", "STUDY_A", "Ax CTA", "SER_A")
    too_deep = series_dir / "DICOM" / "a" / "b" / "deep.warming"
    too_deep.mkdir(parents=True)
    assert csh._orphan_warming_dirs(tmp_path) == []


def test_multiple_warming_dirs_sorted(tmp_path):
    s1 = _make_series(tmp_path, "4-0551", "STUDY_A", "Ax CTA", "SER_A")
    s2 = _make_series(tmp_path, "24-012", "STUDY_B", "Ax Brain", "SER_B")
    w1 = s1 / "DICOM.warming"
    w2 = s2 / "DICOM.warming"
    w1.mkdir()
    w2.mkdir()
    assert csh._orphan_warming_dirs(tmp_path) == sorted([str(w1), str(w2)])
