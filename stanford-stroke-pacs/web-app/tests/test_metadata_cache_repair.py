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

import gzip
import importlib.util
import json
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

def _metadata(n):
    return [{"00080018": {"Value": [f"1.2.{i}"], "vr": "UI"}} for i in range(n)]


def _cache_responses(body, indexed=72, payload=None):
    data = Mock(status_code=200)
    data.content = payload if payload is not None else b"2;signature;" + gzip.compress(json.dumps(body).encode())
    series = Mock(status_code=200)
    series.json.return_value = {"Instances": [str(i) for i in range(indexed)]}
    return [_info_response(1506), data, series]


@pytest.mark.parametrize("size", [57, 64, None])
def test_empty_cache_fast_rejection(size):
    with patch.object(orthanc_client.requests, "get", return_value=_info_response(size)) as get:
        assert not orthanc_client.series_metadata_cache_is_healthy("oid")
        assert get.call_count == 1


@pytest.mark.parametrize("cached,indexed,healthy", [(1, 72, False), (72, 72, True), (1, 1, True), (73, 72, False)])
def test_cache_health_checks_instance_count(cached, indexed, healthy):
    with patch.object(orthanc_client.requests, "get", side_effect=_cache_responses(_metadata(cached), indexed)) as get:
        assert orthanc_client.series_metadata_cache_is_healthy("oid") is healthy
    assert all("/dicom-web/" not in call.args[0] for call in get.call_args_list)


@pytest.mark.parametrize("body,payload", [
    (_metadata(1) * 2, None),
    ([{}], None),
    ([{"00080018": {"Value": "1.2.3"}}], None),
    ([{"00080018": {"Value": ["1.2.3", "1.2.4"]}}], None),
    ([], None),
    (None, b"3;signature;unknown"),
    (None, b"2;signature;broken-gzip"),
])
def test_invalid_cache_fails_closed(body, payload):
    with patch.object(orthanc_client.requests, "get", side_effect=_cache_responses(body, payload=payload)):
        assert not orthanc_client.series_metadata_cache_is_healthy("oid")


def test_cleanup_expected_count_rejects_incomplete_index():
    with patch.object(orthanc_client.requests, "get", side_effect=_cache_responses(_metadata(1), 1)):
        assert not orthanc_client.series_metadata_cache_is_healthy("oid", expected_instances=72)


@pytest.mark.parametrize("complete", [True, False])
def test_rebuild_checks_completeness(complete):
    metadata = Mock(status_code=200)
    metadata.json.return_value = _metadata(1)
    with patch.object(orthanc_client.requests, "delete", return_value=Mock(status_code=200)) as delete, \
         patch.object(orthanc_client.requests, "get", return_value=metadata) as get, \
         patch.object(orthanc_client, "series_metadata_cache_is_healthy", return_value=complete):
        assert orthanc_client.rebuild_series_metadata_cache("st", "se", "oid") is complete
    assert "attachments/4301" in delete.call_args.args[0]
    assert "/dicom-web/" in get.call_args.args[0]


@pytest.mark.parametrize("status,body", [(200, []), (400, None)])
def test_rebuild_reports_failure(status, body):
    metadata = Mock(status_code=status)
    metadata.json.return_value = body
    with patch.object(orthanc_client.requests, "delete", return_value=Mock(status_code=200)), \
         patch.object(orthanc_client.requests, "get", return_value=metadata):
        assert not orthanc_client.rebuild_series_metadata_cache("st", "se", "oid")


def test_rebuild_stops_on_failed_delete():
    with patch.object(orthanc_client.requests, "delete", return_value=Mock(status_code=500)), \
         patch.object(orthanc_client.requests, "get") as get:
        assert not orthanc_client.rebuild_series_metadata_cache("st", "se", "oid")
    get.assert_not_called()


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
