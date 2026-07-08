"""Tests for process_index_job's resume-marker semantics.

The success marker parsed by determine_resume_skip_set is logged by
process_index_job only after index + cleanup + stamp have run. Indexing,
cleanup, and stamping failures are each non-fatal (marker still logged);
only an unexpected error withholds the marker and reports
status="worker_error". These tests monkeypatch the three collaborators —
no DB, no Orthanc.

Run with: pytest tests/test_index_job.py
"""

import pytest

import execute_image_ingestion_protocol as executor
from execute_image_ingestion_protocol import _COMPLETED_MARKER, process_index_job

RESULT_KEYS = {"nhc", "status", "indexing_error", "indexing_traceback",
               "error", "traceback"}


class _CapturingLogger:
    """Minimal logger double; optionally raises when logging the marker."""

    def __init__(self, fail_on_marker=False):
        self.messages = []  # (level, message)
        self.fail_on_marker = fail_on_marker

    def _record(self, level, msg):
        if self.fail_on_marker and str(msg).startswith(_COMPLETED_MARKER):
            raise RuntimeError("log write failed")
        self.messages.append((level, str(msg)))

    def info(self, msg, *a):
        self._record("info", msg)

    def warning(self, msg, *a):
        self._record("warning", msg)

    def error(self, msg, *a):
        self._record("error", msg)

    def marker_logged(self, nhc):
        return any(m == f"{_COMPLETED_MARKER}{nhc}"
                   for _lvl, m in self.messages)


@pytest.fixture
def calls(monkeypatch):
    """Stub the three collaborators; record every call."""
    record = {"index": [], "cleanup": [], "stamp": [], "series_dirs": []}

    def fake_index(engine, logger, series_ids):
        record["index"].append(list(series_ids))
        return list(series_ids)

    def fake_cleanup(engine, logger, indexed):
        record["cleanup"].append(list(indexed))
        return {"cleaned": list(indexed), "kept": []}

    def fake_stamp(engine, logger, hot_rows, cleaned_uids):
        record["stamp"].append({"hot_rows": list(hot_rows),
                                "cleaned_uids": list(cleaned_uids)})

    def fake_series_dirs(engine, series_ids):
        record["series_dirs"].append(list(series_ids))
        return [(uid, f"/data/{uid}") for uid in series_ids]

    monkeypatch.setattr(executor, "index_case_into_orthanc", fake_index)
    monkeypatch.setattr(executor, "cleanup_case_loose_dirs", fake_cleanup)
    monkeypatch.setattr(executor, "_stamp_series_cache_state", fake_stamp)
    monkeypatch.setattr(executor, "_series_dirs_from_db", fake_series_dirs)
    return record


def test_happy_path_logs_marker_and_stamps_cleaned(calls):
    logger = _CapturingLogger()
    res = process_index_job(None, logger, "4-0001", ["s1", "s2"],
                            cleanup_enabled=True)

    assert res["status"] == "ok"
    assert res["indexing_error"] is None
    assert set(res.keys()) == RESULT_KEYS
    assert logger.marker_logged("4-0001")
    assert calls["index"] == [["s1", "s2"]]
    assert calls["cleanup"] == [["s1", "s2"]]
    # Cleaned series get their cache row dropped; nothing is stamped hot on
    # this path ("kept" series failed an archive check — must stay row-less).
    assert calls["stamp"] == [{"hot_rows": [], "cleaned_uids": ["s1", "s2"]}]


def test_cleanup_disabled_stamps_loose_series_hot(calls):
    logger = _CapturingLogger()
    res = process_index_job(None, logger, "4-0001", ["s1"],
                            cleanup_enabled=False)

    assert res["status"] == "ok"
    assert logger.marker_logged("4-0001")
    assert calls["cleanup"] == []
    assert calls["stamp"] == [{"hot_rows": [("s1", "/data/s1")],
                               "cleaned_uids": []}]


def test_indexing_failure_is_nonfatal_and_skips_cleanup(calls, monkeypatch):
    def boom(engine, logger, series_ids):
        raise RuntimeError("orthanc down")

    monkeypatch.setattr(executor, "index_case_into_orthanc", boom)
    logger = _CapturingLogger()
    res = process_index_job(None, logger, "4-0002", ["s1"],
                            cleanup_enabled=True)

    # Marker still logged: the sanity pass / reindex backfill handle the gap.
    assert res["status"] == "ok"
    assert res["indexing_error"] == "orthanc down"
    assert res["indexing_traceback"]
    assert logger.marker_logged("4-0002")
    assert calls["cleanup"] == []
    assert calls["stamp"] == []


def test_cleanup_failure_is_nonfatal(calls, monkeypatch):
    def boom(engine, logger, indexed):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(executor, "cleanup_case_loose_dirs", boom)
    logger = _CapturingLogger()
    res = process_index_job(None, logger, "4-0003", ["s1"],
                            cleanup_enabled=True)

    assert res["status"] == "ok"
    assert res["error"] is None
    assert logger.marker_logged("4-0003")


def test_stamping_failure_is_nonfatal(calls, monkeypatch):
    def boom(engine, logger, hot_rows, cleaned_uids):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(executor, "_stamp_series_cache_state", boom)
    logger = _CapturingLogger()
    res = process_index_job(None, logger, "4-0004", ["s1"],
                            cleanup_enabled=False)

    assert res["status"] == "ok"
    assert logger.marker_logged("4-0004")


def test_unexpected_error_withholds_marker(calls):
    logger = _CapturingLogger(fail_on_marker=True)
    res = process_index_job(None, logger, "4-0005", ["s1"],
                            cleanup_enabled=True)

    assert res["status"] == "worker_error"
    assert res["error"] == "log write failed"
    assert res["traceback"]
    assert set(res.keys()) == RESULT_KEYS
    assert not logger.marker_logged("4-0005")


def test_empty_to_index_still_logs_marker(calls):
    # Every successful ingest enqueues (even with nothing to index) because
    # the worker owns the success-marker line.
    logger = _CapturingLogger()
    res = process_index_job(None, logger, "4-0006", [], cleanup_enabled=True)

    assert res["status"] == "ok"
    assert logger.marker_logged("4-0006")
    assert calls["cleanup"] == []
    assert calls["stamp"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
