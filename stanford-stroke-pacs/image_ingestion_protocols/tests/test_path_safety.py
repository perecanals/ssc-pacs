"""Path-safety regression tests for the ingestion protocol's delete paths.

Two verified bugs (audit stage 00, item B8):

1. ``_remove_empty_parent_dirs`` used a bare ``startswith`` containment check,
   so a sibling root sharing the prefix (the real pair:
   ``/DATA2/pacs_imaging_data`` vs ``/DATA2/pacs_imaging_data_loose_backup``)
   counted as "inside" base_dir.
2. ``overwrite_existing_study`` rmtree'd DB-supplied paths with no containment
   check at all — an out-of-root ``dicom_dir_path``/``study_path`` row would
   be deleted wholesale.

These exercise the guards without a database.

Run with: pytest tests/test_path_safety.py
"""

from types import SimpleNamespace

import pandas as pd

import image_ingestion_protocol as iip_mod
from image_ingestion_protocol import ImageIngestionProtocol


def _bare_protocol(base_dir):
    """An ImageIngestionProtocol without __init__ (no DB engine needed)."""
    proto = object.__new__(ImageIngestionProtocol)
    proto.base_dir = str(base_dir)
    return proto


# ---------------------------------------------------------------------------
# _is_strictly_inside_base_dir
# ---------------------------------------------------------------------------


def test_containment_rejects_prefix_sibling(tmp_path):
    """The real collision pair: *_loose_backup shares the base_dir prefix."""
    base = tmp_path / "pacs_imaging_data"
    sibling = tmp_path / "pacs_imaging_data_loose_backup"
    base.mkdir()
    sibling.mkdir()
    proto = _bare_protocol(base)
    assert proto._is_strictly_inside_base_dir(str(sibling)) is False
    assert proto._is_strictly_inside_base_dir(str(sibling / "4-0551")) is False


def test_containment_accepts_true_children(tmp_path):
    base = tmp_path / "pacs_imaging_data"
    proto = _bare_protocol(base)
    assert proto._is_strictly_inside_base_dir(str(base / "4-0551")) is True
    assert proto._is_strictly_inside_base_dir(str(base / "a" / "b" / "c")) is True


def test_containment_rejects_base_itself_and_junk(tmp_path):
    base = tmp_path / "pacs_imaging_data"
    proto = _bare_protocol(base)
    assert proto._is_strictly_inside_base_dir(str(base)) is False
    assert proto._is_strictly_inside_base_dir(str(tmp_path)) is False
    assert proto._is_strictly_inside_base_dir("") is False
    assert proto._is_strictly_inside_base_dir(None) is False


# ---------------------------------------------------------------------------
# _remove_empty_parent_dirs
# ---------------------------------------------------------------------------


def test_remove_empty_parents_stops_at_base_dir(tmp_path):
    base = tmp_path / "pacs_imaging_data"
    leaf = base / "4-0551" / "STUDY_UID" / "desc" / "SER_UID"
    leaf.mkdir(parents=True)
    proto = _bare_protocol(base)

    proto._remove_empty_parent_dirs(str(leaf / "DICOM"))

    assert base.exists()  # never removes the root itself
    assert not (base / "4-0551").exists()  # empty chain cleaned up


def test_remove_empty_parents_never_walks_into_prefix_sibling(tmp_path):
    """With the old startswith check, a path under *_loose_backup walked (and
    rmdir'd) the empty backup tree; now it must be left untouched."""
    base = tmp_path / "pacs_imaging_data"
    base.mkdir()
    backup = tmp_path / "pacs_imaging_data_loose_backup"
    backup_leaf = backup / "4-0551" / "STUDY_UID"
    backup_leaf.mkdir(parents=True)
    proto = _bare_protocol(base)

    proto._remove_empty_parent_dirs(str(backup_leaf / "DICOM"))

    assert backup_leaf.exists()


def test_remove_empty_parents_keeps_nonempty_dirs(tmp_path):
    base = tmp_path / "pacs_imaging_data"
    patient = base / "4-0551"
    study = patient / "STUDY_UID"
    study.mkdir(parents=True)
    (patient / "OTHER_STUDY").mkdir()
    proto = _bare_protocol(base)

    proto._remove_empty_parent_dirs(str(study / "SER" / "DICOM"))

    assert not study.exists()
    assert patient.exists()  # still holds OTHER_STUDY


# ---------------------------------------------------------------------------
# overwrite_existing_study containment guard
# ---------------------------------------------------------------------------


class _FakeConn:
    def execute(self, *args, **kwargs):
        return None


class _FakeBegin:
    def __enter__(self):
        return _FakeConn()

    def __exit__(self, *exc):
        return False


def _overwrite_fixture(tmp_path, monkeypatch, study_path, dicom_dir_path):
    base = tmp_path / "pacs_imaging_data"
    proto = _bare_protocol(base)
    proto.cold_archive_root = None
    proto.postgres_engine = SimpleNamespace(begin=lambda: _FakeBegin())
    proto.image_study = pd.DataFrame(
        [{"studyinstanceuid": "STUDY1", "patient_id": "4-0551", "study_path": study_path}]
    )
    proto.image_series = pd.DataFrame(
        [
            {
                "studyinstanceuid": "STUDY1",
                "seriesinstanceuid": "SER1",
                "dicom_dir_path": dicom_dir_path,
                "dicom_archive_path": None,
            }
        ]
    )
    monkeypatch.setattr(
        iip_mod, "inspect", lambda engine: SimpleNamespace(has_table=lambda name: False)
    )
    return proto


def test_overwrite_deletes_in_root_paths(tmp_path, monkeypatch):
    base = tmp_path / "pacs_imaging_data"
    dicom_dir = base / "4-0551" / "STUDY1" / "desc" / "SER1" / "DICOM"
    dicom_dir.mkdir(parents=True)
    (dicom_dir / "IM-0.dcm").write_bytes(b"x")

    proto = _overwrite_fixture(tmp_path, monkeypatch, None, str(dicom_dir))
    proto.overwrite_existing_study("STUDY1")

    assert not dicom_dir.exists()


def test_overwrite_skips_out_of_root_paths(tmp_path, monkeypatch, capsys):
    """Out-of-root DB paths (e.g. under the *_loose_backup sibling) must be
    warned about and left on disk, not rmtree'd."""
    outside = tmp_path / "pacs_imaging_data_loose_backup" / "4-0551" / "STUDY1"
    outside_dicom = outside / "desc" / "SER1" / "DICOM"
    outside_dicom.mkdir(parents=True)
    (outside_dicom / "IM-0.dcm").write_bytes(b"x")

    proto = _overwrite_fixture(tmp_path, monkeypatch, str(outside), str(outside_dicom))
    (tmp_path / "pacs_imaging_data").mkdir()
    proto.overwrite_existing_study("STUDY1")

    assert outside_dicom.exists()
    assert (outside_dicom / "IM-0.dcm").exists()
    out = capsys.readouterr().out
    assert "refusing to remove path outside base_dir" in out
