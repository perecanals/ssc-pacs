"""Tests for the DICOMweb series-metadata cache invariant.

Orthanc's DICOMweb plugin builds a series' WADO-RS metadata cache by reading
the DICOM files. In cold_path_cache mode a cache computed while the series is
evicted is stored as an empty array and never expires — every later metadata
request 400s and OHIF hangs. These tests pin the two halves of the fix:

* the cache is only ever rebuilt while a series is warm (cache_manager)
* loose DICOMs are not deleted until Orthanc has built the cache
  (cleanup_loose_dicoms)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

import cache_manager
import orthanc_client

_CLEANUP_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts" / "cold_storage" / "cleanup_loose_dicoms.py"
)


def _load_cleanup():
    spec = importlib.util.spec_from_file_location("cleanup_loose_dicoms", _CLEANUP_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _info_response(size: int | None):
    """Fake GET .../attachments/4301/info — None means the cache is absent."""
    resp = Mock()
    if size is None:
        resp.status_code = 404
        return resp
    resp.status_code = 200
    resp.json.return_value = {"UncompressedSize": size}
    return resp


# --- the "is this cache poisoned?" predicate --------------------------------

@pytest.mark.parametrize(
    "size,healthy",
    [
        (57, False),     # the empty-array payload: '<rev>;<sig>;' + gzip('[]')
        (64, False),     # boundary — still the empty form
        (1506, True),    # a real single-instance cache
        (None, False),   # no cache at all
    ],
)
def test_cache_health_predicate(size, healthy):
    with patch.object(orthanc_client.requests, "get", return_value=_info_response(size)):
        assert orthanc_client.series_metadata_cache_is_healthy("oid") is healthy


# --- rebuild = DELETE + GET, and it must verify the result ------------------

def test_rebuild_deletes_then_rebuilds_and_reports_success():
    metadata = Mock(status_code=200)
    metadata.json.return_value = [{"00080018": {}}]  # one instance
    with patch.object(orthanc_client.requests, "delete") as delete, \
         patch.object(orthanc_client.requests, "get", return_value=metadata) as get:
        ok = orthanc_client.rebuild_series_metadata_cache("1.2.study", "1.2.series", "oid")

    assert ok is True
    # The DELETE must come first: a GET against a poisoned cache just re-serves it.
    assert "attachments/4301" in delete.call_args[0][0]
    assert "/dicom-web/studies/1.2.study/series/1.2.series/metadata" in get.call_args[0][0]


def test_rebuild_reports_failure_when_result_is_still_empty():
    """An empty array back means the files were NOT on disk — the caller must
    not treat that as repaired."""
    metadata = Mock(status_code=200)
    metadata.json.return_value = []
    with patch.object(orthanc_client.requests, "delete"), \
         patch.object(orthanc_client.requests, "get", return_value=metadata):
        assert orthanc_client.rebuild_series_metadata_cache("st", "se", "oid") is False


def test_rebuild_reports_failure_on_http_400():
    with patch.object(orthanc_client.requests, "delete"), \
         patch.object(orthanc_client.requests, "get", return_value=Mock(status_code=400)):
        assert orthanc_client.rebuild_series_metadata_cache("st", "se", "oid") is False


# --- the warm-path safety net ----------------------------------------------

def test_warm_repairs_a_poisoned_cache():
    with patch.object(cache_manager, "orthanc_series_id", return_value="oid"), \
         patch.object(cache_manager, "series_metadata_cache_is_healthy", return_value=False), \
         patch.object(cache_manager, "rebuild_series_metadata_cache",
                      return_value=True) as rebuild:
        cache_manager._repair_metadata_cache("1.2.series", "1.2.study", {})

    rebuild.assert_called_once_with("1.2.study", "1.2.series", "oid")


def test_warm_leaves_a_healthy_cache_alone():
    with patch.object(cache_manager, "orthanc_series_id", return_value="oid"), \
         patch.object(cache_manager, "series_metadata_cache_is_healthy", return_value=True), \
         patch.object(cache_manager, "rebuild_series_metadata_cache") as rebuild:
        cache_manager._repair_metadata_cache("1.2.series", "1.2.study", {})

    rebuild.assert_not_called()


def test_warm_never_fails_because_of_orthanc():
    """The pixels are on disk regardless — a cache repair failure must not
    turn a successful warm into a failed one."""
    with patch.object(cache_manager, "orthanc_series_id",
                      side_effect=requests.RequestException("orthanc down")):
        cache_manager._repair_metadata_cache("1.2.series", "1.2.study", {})  # must not raise


# --- the ingestion guard ----------------------------------------------------

@pytest.fixture()
def loose_series(tmp_path):
    """A loose DICOM dir plus a matching archive, ready to be cleaned."""
    cleanup = _load_cleanup()
    dicom_dir = tmp_path / "DICOM"
    dicom_dir.mkdir()
    (dicom_dir / "instance.dcm").write_bytes(b"x" * 32)
    archive = tmp_path / "DICOM.tar.zst"
    archive.write_bytes(b"archive")
    return cleanup, dicom_dir, archive


def test_cleanup_refuses_to_delete_before_orthanc_cached_the_metadata(loose_series):
    """The regression that stranded 19,658 series: deleting the loose files
    before Orthanc built its metadata cache poisons the series forever."""
    cleanup, dicom_dir, archive = loose_series
    with patch.object(cleanup, "orthanc_series_id", return_value="oid"), \
         patch.object(cleanup, "wait_for_series_metadata_cache", return_value=False):
        status, freed, detail = cleanup.clean_series_loose_dir(
            "1.2.series", dicom_dir, archive,
            series_in_orthanc=True, deep_verify=False, execute=True,
        )

    assert status == "metadata_cache_not_ready"
    assert freed == 0
    assert dicom_dir.exists(), "loose files must survive — deleting them strands the series"


def test_cleanup_deletes_once_the_cache_is_built(loose_series):
    cleanup, dicom_dir, archive = loose_series
    with patch.object(cleanup, "orthanc_series_id", return_value="oid"), \
         patch.object(cleanup, "wait_for_series_metadata_cache", return_value=True):
        status, freed, _ = cleanup.clean_series_loose_dir(
            "1.2.series", dicom_dir, archive,
            series_in_orthanc=True, deep_verify=False, execute=True,
        )

    assert status == "cleaned"
    assert freed > 0
    assert not dicom_dir.exists()


def test_evict_tolerates_a_directory_that_vanished(monkeypatch):
    """A concurrent warm/evict of the same series both rmtree the series dir;
    whoever loses the final rmdir() gets ENOENT. That is eviction's goal state
    (the files are gone), so it must not abort and strand the cache rows."""
    calls = {"rmtree": 0}

    def boom(path):
        calls["rmtree"] += 1
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(cache_manager.shutil, "rmtree", boom)
    monkeypatch.setattr(cache_manager.Path, "exists", lambda self: True)

    rows = [{"seriesinstanceuid": "1.2.series", "dicom_dir_path": "/tmp/gone/DICOM"}]

    class FakeCur:
        def execute(self, *a, **k): pass
        def fetchall(self): return rows
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn:
        def cursor(self, **k): return FakeCur()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(cache_manager, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(cache_manager, "_advisory_lock", lambda *a: None)
    monkeypatch.setattr(cache_manager, "_advisory_unlock", lambda *a: None)

    out = cache_manager.evict_series(["1.2.series"])  # must not raise

    assert out["ok"] is True
    assert calls["rmtree"] == 1


def test_cleanup_refuses_when_series_is_not_in_orthanc(loose_series):
    cleanup, dicom_dir, archive = loose_series
    with patch.object(cleanup, "orthanc_series_id", return_value=None):
        status, _, _ = cleanup.clean_series_loose_dir(
            "1.2.series", dicom_dir, archive,
            series_in_orthanc=True, deep_verify=False, execute=True,
        )

    assert status == "not_in_orthanc"
    assert dicom_dir.exists()
