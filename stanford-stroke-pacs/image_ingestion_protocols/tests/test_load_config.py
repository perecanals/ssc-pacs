"""Tests for the executor's YAML config loading/validation (load_config).

Monkeypatches the module-level STORAGE_MODE / COLD_ARCHIVE_ROOT (normally
imported from repo-root config.toml) so the tests are independent of the
host's storage mode. No DB.

Run with: pytest tests/test_load_config.py
"""

import pytest

import execute_image_ingestion_protocol as executor
from execute_image_ingestion_protocol import load_config


def _write_yaml(tmp_path, body):
    p = tmp_path / "run.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


@pytest.fixture
def legacy_mode(monkeypatch):
    monkeypatch.setattr(executor, "STORAGE_MODE", "legacy")
    monkeypatch.setattr(executor, "COLD_ARCHIVE_ROOT", None)


@pytest.fixture
def cold_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "STORAGE_MODE", "cold_path_cache")
    monkeypatch.setattr(executor, "COLD_ARCHIVE_ROOT", str(tmp_path / "cold_root"))


def test_defaults_merge(cold_mode, tmp_path):
    cfg, raw = load_config(_write_yaml(tmp_path, "src_dir: /data/batch1\n"))

    assert cfg["src_dir"] == "/data/batch1"
    assert cfg["database"] == "stanford-stroke"
    assert cfg["overwrite_if_exists"] is False
    assert cfg["resume"] is True
    assert cfg["compress_workers"] == 4
    assert cfg["pipeline_indexing"] is True
    assert cfg["cleanup_loose_after_indexing"] is True
    # cold_archive_root resolved from config.toml (module globals here).
    assert cfg["cold_archive_root"] == str(tmp_path / "cold_root")
    assert raw.startswith("src_dir:")


def test_missing_src_dir_fails_fast(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="src_dir"):
        load_config(_write_yaml(tmp_path, "import_label: x\n"))


def test_missing_file_and_non_mapping_yaml_raise(cold_mode, tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.yaml"))
    with pytest.raises(ValueError, match="top-level mapping"):
        load_config(_write_yaml(tmp_path, "- just\n- a\n- list\n"))


def test_compress_workers_coercion(cold_mode, tmp_path):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncompress_workers: 0\n"))
    assert cfg["compress_workers"] == 1
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncompress_workers: '3'\n"))
    assert cfg["compress_workers"] == 3
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncompress_workers: null\n"))
    assert cfg["compress_workers"] == 1


def test_overwrite_disables_pipeline_indexing(cold_mode, tmp_path, capsys):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\noverwrite_if_exists: true\n"))
    assert cfg["pipeline_indexing"] is False
    assert "pipeline_indexing disabled" in capsys.readouterr().out


def test_cold_mode_without_archive_root_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "STORAGE_MODE", "cold_path_cache")
    monkeypatch.setattr(executor, "COLD_ARCHIVE_ROOT", "")
    with pytest.raises(RuntimeError, match="cold_archive_root"):
        load_config(_write_yaml(tmp_path, "src_dir: /data/b\n"))


def test_cold_mode_yaml_override_warns(cold_mode, tmp_path, capsys):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncold_archive_root: /elsewhere\n"))
    assert cfg["cold_archive_root"] == "/elsewhere"
    assert "YAML overrides cold_archive_root" in capsys.readouterr().out


def test_legacy_mode_forces_cleanup_off(legacy_mode, tmp_path, capsys):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncleanup_loose_after_indexing: true\n"))
    assert cfg["cleanup_loose_after_indexing"] is False
    assert "legacy" in capsys.readouterr().out


def test_legacy_mode_archive_root_warns_but_keeps(legacy_mode, tmp_path, capsys):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncold_archive_root: /somewhere\n"))
    assert cfg["cold_archive_root"] == "/somewhere"
    assert "mode is 'legacy'" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
