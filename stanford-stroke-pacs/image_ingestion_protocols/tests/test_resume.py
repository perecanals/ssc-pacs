"""Tests for success-based resume in the ingestion driver.

determine_resume_skip_set scans every prior run log whose 'Source directory:'
header matches the current src_dir and returns the set of cases proven
complete (union across logs). These tests write synthetic log files (no DB,
no DICOM) and assert the computed skip set.

Adopted from hidden/ingestion_dev/test_resume.py, which covered the previous
position-based resume; scenarios ported to the skip-set semantics.

Run with: pytest tests/test_resume.py
"""

import logging

import pytest

from execute_image_ingestion_protocol import (
    _COMPLETED_MARKER,
    _SRC_DIR_MARKER,
    determine_resume_skip_set,
)

SRC = "/data/batch1"
CASES = ["4-0001", "4-0002", "4-0003", "4-0004", "4-0005"]


def _log(lines):
    """Render a prior-run log body from marker lines (timestamps don't matter)."""
    return "\n".join(f"2026-06-26 10:00:00,000 - INFO - {ln}" for ln in lines) + "\n"


def _write_log(logs_dir, name, lines, prefix="execute_image_ingestion_protocol"):
    p = logs_dir / f"{prefix}_{name}.log"
    p.write_text(_log(lines), encoding="utf-8")
    return str(p)


def _skip_set(logs_dir, src=SRC, cases=CASES, current=None):
    return determine_resume_skip_set(str(logs_dir), current, src, cases)


def test_no_prior_log_skips_nothing(tmp_path):
    assert _skip_set(tmp_path) == set()


def test_only_completed_cases_are_skipped(tmp_path):
    # 4-0003 was reached but never completed -> re-processed.
    _write_log(tmp_path, "20260626_090000", [
        f"Source directory: {SRC}",
        "Processing case 4-0001",
        "Successfully completed processing case 4-0001",
        "Processing case 4-0002",
        "Successfully completed processing case 4-0002",
        "Processing case 4-0003",
    ])
    assert _skip_set(tmp_path) == {"4-0001", "4-0002"}


def test_failed_case_between_successes_is_reprocessed(tmp_path):
    # Position-based resume would have skipped the failed 4-0002; the
    # success-based skip set must not.
    _write_log(tmp_path, "20260626_090000", [
        f"Source directory: {SRC}",
        "Processing case 4-0001",
        "Successfully completed processing case 4-0001",
        "Processing case 4-0002",
        "Failed to process case 4-0002: disk full",
        "Processing case 4-0003",
        "Successfully completed processing case 4-0003",
    ])
    assert _skip_set(tmp_path) == {"4-0001", "4-0003"}


def test_different_src_dir_is_ignored(tmp_path):
    _write_log(tmp_path, "20260626_090000", [
        "Source directory: /data/batch2",
        "Processing case 4-0001",
        "Successfully completed processing case 4-0001",
    ])
    assert _skip_set(tmp_path) == set()


def test_union_across_multiple_matching_logs(tmp_path):
    # Two prior runs each completed different cases; a third (non-matching
    # batch) log contributes nothing even though it is the newest.
    _write_log(tmp_path, "20260626_080000", [
        f"Source directory: {SRC}",
        "Successfully completed processing case 4-0001",
    ])
    _write_log(tmp_path, "20260626_090000", [
        f"Source directory: {SRC}",
        "Successfully completed processing case 4-0004",
    ])
    _write_log(tmp_path, "20260626_093000", [
        "Source directory: /data/batch2",
        "Successfully completed processing case 4-0002",
    ])
    assert _skip_set(tmp_path) == {"4-0001", "4-0004"}


def test_pre_rename_integration_logs_still_match(tmp_path):
    # Logs from before the integration->ingestion rename keep resume continuity.
    _write_log(tmp_path, "20260626_090000", [
        f"Source directory: {SRC}",
        "Successfully completed processing case 4-0002",
    ], prefix="execute_image_integration_protocol")
    assert _skip_set(tmp_path) == {"4-0002"}


def test_unknown_case_names_are_filtered(tmp_path):
    # A completed case no longer present in the source dir must not leak into
    # the skip set.
    _write_log(tmp_path, "20260626_090000", [
        f"Source directory: {SRC}",
        "Successfully completed processing case 4-9999",
        "Successfully completed processing case 4-0005",
    ])
    assert _skip_set(tmp_path) == {"4-0005"}


def test_current_log_is_ignored(tmp_path):
    # The current run's own (matching) log must not be parsed for resume.
    cur = _write_log(tmp_path, "20260626_100000", [
        f"Source directory: {SRC}",
        "Successfully completed processing case 4-0004",
    ])
    assert _skip_set(tmp_path, current=cur) == set()


def test_current_logger_format_round_trips(tmp_path):
    # Guard against marker-string drift: a log written through the actual
    # logging format used by the executor must parse back.
    log_path = tmp_path / "execute_image_ingestion_protocol_20260626_110000.log"
    logger = logging.getLogger("test_resume_roundtrip")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(f"{_SRC_DIR_MARKER}{SRC}")
        logger.info(f"{_COMPLETED_MARKER}4-0003")
    finally:
        handler.close()
        logger.removeHandler(handler)

    assert _skip_set(tmp_path) == {"4-0003"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
