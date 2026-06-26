"""Unit tests for the pure detection logic of the stale-index pruner.

These exercise ``classify`` and ``host_dir_to_container`` from
``scripts/cold_storage/prune_stale_index_paths.py`` with no DB/Orthanc/docker
dependency — the script's runtime imports are deferred into ``main()``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "cold_storage"
    / "prune_stale_index_paths.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("prune_stale_index_paths", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prune = _load_module()

# Canonical container dirs for two series.
CANON_A = "/dicom-data/24-012/STUDYA/AX CTA  With/SER_A/DICOM"
CANON_B = "/dicom-data/24-012/STUDYB/Ax Brain/SER_B/DICOM"
SUID2DIR = {"SUID_A": CANON_A, "SUID_B": CANON_B}


def test_valid_only_instance_has_no_stale():
    rows = [(f"{CANON_A}/IM-1.dcm", "iidA"), (f"{CANON_A}/IM-2.dcm", "iidA")]
    res = prune.classify(rows, {"iidA": "SUID_A"}, SUID2DIR)
    assert res["stale_paths"] == []
    assert res["orphan_instances"] == []
    assert res["valid"] == 2


def test_partial_stale_keeps_valid_deletes_wrong_dir():
    # Same instance registered under its canonical dir AND under series B's dir.
    rows = [
        (f"{CANON_A}/IM-1.dcm", "iidA"),  # valid
        (f"{CANON_B}/IM-1.dcm", "iidA"),  # stale (wrong dir for SUID_A)
    ]
    res = prune.classify(rows, {"iidA": "SUID_A"}, SUID2DIR)
    assert res["stale_paths"] == [f"{CANON_B}/IM-1.dcm"]
    assert res["orphan_instances"] == []
    assert res["valid"] == 1


def test_fully_stale_instance_is_orphan_not_raw_deleted():
    # Instance's only row is in the wrong dir -> raw-deleting it would orphan it.
    rows = [(f"{CANON_B}/IM-9.dcm", "iidA")]
    res = prune.classify(rows, {"iidA": "SUID_A"}, SUID2DIR)
    assert res["stale_paths"] == []  # never raw-delete the last row
    assert res["orphan_instances"] == ["iidA"]
    assert res["orphan_paths"] == [f"{CANON_B}/IM-9.dcm"]


def test_orphan_series_uid_absent_from_db_is_skipped():
    rows = [(f"{CANON_A}/IM-1.dcm", "iidZ")]
    res = prune.classify(rows, {"iidZ": "SUID_UNKNOWN"}, SUID2DIR)
    assert res["stale_paths"] == []
    assert res["orphan_instances"] == []
    assert res["orphan_series"] == 1


def test_unknown_instance_not_in_orthanc_is_skipped():
    rows = [(f"{CANON_A}/IM-1.dcm", "iidGhost")]
    res = prune.classify(rows, {}, SUID2DIR)  # instance not in Orthanc map
    assert res["stale_paths"] == []
    assert res["unknown_inst"] == 1


def test_double_space_directory_matches_exactly():
    # The real data has a "AX CTA  With" (double space) dir; exact compare must hold.
    rows = [(f"{CANON_A}/IM-1.dcm", "iidA")]
    res = prune.classify(rows, {"iidA": "SUID_A"}, SUID2DIR)
    assert res["valid"] == 1
    assert res["stale_paths"] == []


def test_host_dir_to_container_rewrites_prefix():
    host_root = "/Volumes/Expansion/ssc-pacs-data/imaging_data"
    host_dir = f"{host_root}/24-012/STUDYA/AX CTA  With/SER_A/DICOM"
    assert (
        prune.host_dir_to_container(host_dir, host_root, "/dicom-data")
        == "/dicom-data/24-012/STUDYA/AX CTA  With/SER_A/DICOM"
    )


def test_host_dir_to_container_leaves_unrelated_path():
    out = prune.host_dir_to_container("/other/root/x", "/Volumes/Expansion", "/dicom-data")
    assert out == "/other/root/x"
