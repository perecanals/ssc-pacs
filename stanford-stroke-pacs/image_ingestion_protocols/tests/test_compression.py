"""Tests for per-series cold-archive compression (_compress_series_dir).

Filesystem-only: a bare protocol object (no DB engine) compresses synthetic
series dirs into a scratch cold_archive_root.

Run with: pytest tests/test_compression.py
"""

import os
import tarfile

import pytest
import zstandard as zstd

from image_ingestion_protocol import ImageIngestionProtocol


def _bare_protocol(tmp_path, workers=2):
    proto = object.__new__(ImageIngestionProtocol)
    proto.base_dir = str(tmp_path / "dicom_root")
    proto.cold_archive_root = str(tmp_path / "cold_root")
    proto.compress_workers = workers
    return proto


def _make_series_dir(tmp_path, n_files=3, patient="11-001"):
    dicom_dir = (tmp_path / "dicom_root" / patient / "STUDY_UID" / "desc"
                 / "SER_UID" / "DICOM")
    dicom_dir.mkdir(parents=True)
    for i in range(n_files):
        (dicom_dir / f"IM-{i:04d}.dcm").write_bytes(b"DICM" + bytes([i]) * 64)
    return dicom_dir


def _archive_members(archive_path):
    dctx = zstd.ZstdDecompressor()
    with open(archive_path, "rb") as f_in:
        with dctx.stream_reader(f_in) as z_in:
            with tarfile.open(fileobj=z_in, mode="r|") as tf:
                return [(m.name, tf.extractfile(m).read() if m.isfile() else None)
                        for m in tf]


def test_round_trip_matches_source(tmp_path):
    proto = _bare_protocol(tmp_path)
    dicom_dir = _make_series_dir(tmp_path)

    archive = proto._compress_series_dir(str(dicom_dir))

    # Archive lands under cold_root, mirroring the relative layout, with a
    # flat internal format (files at archive root, no DICOM/ wrapper).
    assert archive.startswith(proto.cold_archive_root)
    assert archive.endswith("DICOM.tar.zst")
    members = _archive_members(archive)
    assert sorted(name for name, _ in members) == [
        "IM-0000.dcm", "IM-0001.dcm", "IM-0002.dcm"]
    for name, content in members:
        assert content == (dicom_dir / name).read_bytes()
    ImageIngestionProtocol._verify_archive(archive, 3)


def test_idempotent_rerun_keeps_existing_archive(tmp_path):
    proto = _bare_protocol(tmp_path)
    dicom_dir = _make_series_dir(tmp_path)

    archive = proto._compress_series_dir(str(dicom_dir))
    stat_before = os.stat(archive)

    assert proto._compress_series_dir(str(dicom_dir)) == archive
    stat_after = os.stat(archive)
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns  # not rewritten
    assert stat_after.st_size == stat_before.st_size


def test_corrupt_existing_archive_is_rebuilt(tmp_path):
    proto = _bare_protocol(tmp_path)
    dicom_dir = _make_series_dir(tmp_path)

    archive = proto._compress_series_dir(str(dicom_dir))
    with open(archive, "wb") as f:
        f.write(b"not a zstd stream")

    assert proto._compress_series_dir(str(dicom_dir)) == archive
    ImageIngestionProtocol._verify_archive(archive, 3)


def test_empty_source_dir_raises(tmp_path):
    proto = _bare_protocol(tmp_path)
    dicom_dir = _make_series_dir(tmp_path, n_files=0)

    with pytest.raises(ValueError, match="empty"):
        proto._compress_series_dir(str(dicom_dir))


def test_missing_source_dir_raises(tmp_path):
    proto = _bare_protocol(tmp_path)
    (tmp_path / "dicom_root").mkdir()

    with pytest.raises(FileNotFoundError):
        proto._compress_series_dir(str(tmp_path / "dicom_root" / "nope"))


def test_source_outside_base_dir_raises(tmp_path):
    proto = _bare_protocol(tmp_path)
    outside = tmp_path / "elsewhere" / "DICOM"
    outside.mkdir(parents=True)
    (outside / "IM-0000.dcm").write_bytes(b"DICM")

    with pytest.raises(ValueError, match="not under base_dir"):
        proto._compress_series_dir(str(outside))


def test_failed_build_leaves_no_tmp_or_archive(tmp_path, monkeypatch):
    proto = _bare_protocol(tmp_path)
    dicom_dir = _make_series_dir(tmp_path)

    def failing_verify(archive_path, expected_count):
        raise ValueError("forced verification failure")

    monkeypatch.setattr(ImageIngestionProtocol, "_verify_archive",
                        staticmethod(failing_verify))
    with pytest.raises(ValueError, match="forced verification failure"):
        proto._compress_series_dir(str(dicom_dir))

    cold_root = tmp_path / "cold_root"
    leftovers = [p for p in cold_root.rglob("*") if p.is_file()]
    assert leftovers == []  # neither .tmp nor a published archive


def test_verify_archive_count_mismatch_raises(tmp_path):
    proto = _bare_protocol(tmp_path)
    dicom_dir = _make_series_dir(tmp_path)
    archive = proto._compress_series_dir(str(dicom_dir))

    with pytest.raises(ValueError, match="expected 4"):
        ImageIngestionProtocol._verify_archive(archive, 4)


def test_verify_archive_missing_or_empty_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ImageIngestionProtocol._verify_archive(str(tmp_path / "missing.tar.zst"), 1)
    empty = tmp_path / "empty.tar.zst"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        ImageIngestionProtocol._verify_archive(str(empty), 1)


def test_compress_cold_archives_records_failures_nonfatally(tmp_path, monkeypatch):
    """Batch wrapper: successes stamp dicom_archive_path, failures keep NULL
    and are reported (log write stubbed away from the live logs/ dir)."""
    import pandas as pd

    proto = _bare_protocol(tmp_path)
    proto.case_dir = str(tmp_path / "src" / "11-001")
    dicom_dir = _make_series_dir(tmp_path)
    proto.case_series_table = pd.DataFrame([
        {"seriesinstanceuid": "s1", "studyinstanceuid": "st1",
         "dicom_dir_path": str(dicom_dir)},
        {"seriesinstanceuid": "s2", "studyinstanceuid": "st1",
         "dicom_dir_path": ""},  # -> failure row (real flow inits to "")
    ])

    written = {}

    def fake_failure_log(self, failures):
        written["failures"] = failures
        return "/dev/null"

    monkeypatch.setattr(ImageIngestionProtocol, "_write_compression_failure_log",
                        fake_failure_log)
    proto.compress_cold_archives()

    t = proto.case_series_table.set_index("seriesinstanceuid")
    assert t.loc["s1", "dicom_archive_path"].endswith("DICOM.tar.zst")
    assert t.loc["s2", "dicom_archive_path"] is None
    assert len(written["failures"]) == 1
    assert written["failures"][0]["error"] == "missing dicom_dir_path"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
