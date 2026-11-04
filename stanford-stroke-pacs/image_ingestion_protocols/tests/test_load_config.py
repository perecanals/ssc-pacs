"""Tests for the executor's YAML config loading/validation (load_config).

Monkeypatches the module-level STORAGE_MODE / COLD_ARCHIVE_ROOT (normally
imported from repo-root config.toml) so the tests are independent of the
host's storage mode. No DB.

Run with: pytest tests/test_load_config.py
"""

import pytest
from archive_volumes import ArchiveVolume

import execute_image_ingestion_protocol as executor
from execute_image_ingestion_protocol import load_config


def _write_yaml(tmp_path, body, dataset="TEST"):
    """Write a run YAML; `dataset` (required by load_config) is appended
    unless None."""
    if dataset is not None:
        body += f"dataset: {dataset}\n"
    p = tmp_path / "run.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


@pytest.fixture
def legacy_mode(monkeypatch):
    monkeypatch.setattr(executor, "STORAGE_MODE", "legacy")


@pytest.fixture
def volumes(monkeypatch, tmp_path):
    """Two scratch volumes; default disk1, dataset GUIDE -> disk2."""
    import config

    vols = {n: ArchiveVolume(n, tmp_path / n) for n in ("disk1", "disk2")}
    monkeypatch.setattr(config, "ARCHIVE_VOLUMES", vols)
    monkeypatch.setattr(config, "DEFAULT_ARCHIVE_VOLUME", "disk1")
    monkeypatch.setattr(config, "DATASET_ARCHIVE_VOLUME", {"GUIDE": "disk2"})
    return vols


@pytest.fixture
def cold_mode(monkeypatch, volumes):
    monkeypatch.setattr(executor, "STORAGE_MODE", "cold_path_cache")


def test_defaults_merge(cold_mode, tmp_path):
    cfg, raw = load_config(_write_yaml(tmp_path, "src_dir: /data/batch1\n"))

    assert cfg["src_dir"] == "/data/batch1"
    assert cfg["database"] == "stanford-stroke"
    assert cfg["overwrite_if_exists"] is False
    assert cfg["resume"] is True
    assert cfg["compress_workers"] == 4
    assert cfg["pipeline_indexing"] is True
    assert cfg["cleanup_loose_after_indexing"] is True
    assert cfg["skip_dir_names"] == []
    # Archive volume resolved from config.toml (module globals here).
    assert cfg["archive_volume"] == "disk1"
    assert cfg["cold_archive_root"] == str(tmp_path / "disk1")
    assert raw.startswith("src_dir:")


def test_skip_dir_names_passthrough_and_validation(cold_mode, tmp_path):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\nskip_dir_names: [NIFTI, nifti]\n"))
    assert cfg["skip_dir_names"] == ["NIFTI", "nifti"]

    cfg, _ = load_config(_write_yaml(tmp_path, "src_dir: /data/b\nskip_dir_names:\n"))
    assert cfg["skip_dir_names"] == []

    for bad in ("skip_dir_names: NIFTI", "skip_dir_names: [a/b]", "skip_dir_names: ['']"):
        with pytest.raises(ValueError, match="skip_dir_names"):
            load_config(_write_yaml(tmp_path, f"src_dir: /data/b\n{bad}\n"))


def test_missing_src_dir_fails_fast(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="src_dir"):
        load_config(_write_yaml(tmp_path, "import_label: x\n"))


def test_missing_dataset_fails_fast(cold_mode, tmp_path):
    for dataset in (None, "''"):
        with pytest.raises(ValueError, match="dataset is required"):
            load_config(_write_yaml(tmp_path, "src_dir: /data/b\n", dataset=dataset))


def test_missing_file_and_non_mapping_yaml_raise(cold_mode, tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.yaml"))
    with pytest.raises(ValueError, match="top-level mapping"):
        load_config(_write_yaml(tmp_path, "- just\n- a\n- list\n", dataset=None))


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


def test_archive_volume_resolution_order(cold_mode, tmp_path):
    # Dataset default from config.toml.
    cfg, _ = load_config(_write_yaml(tmp_path, "src_dir: /data/b\n", dataset="GUIDE"))
    assert cfg["archive_volume"] == "disk2"
    assert cfg["cold_archive_root"] == str(tmp_path / "disk2")
    assert "dataset_archive_volume" in cfg["_archive_volume_source"]
    # The YAML wins over the dataset default.
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\narchive_volume: disk1\n", dataset="GUIDE"))
    assert cfg["archive_volume"] == "disk1"
    assert cfg["_archive_volume_source"] == "yaml archive_volume"


def test_unknown_archive_volume_rejected(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="unknown archive volume 'disk9'"):
        load_config(_write_yaml(tmp_path, "src_dir: /data/b\narchive_volume: disk9\n"))


def test_yaml_cold_archive_root_is_retired(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="archive_volume"):
        load_config(_write_yaml(
            tmp_path, "src_dir: /data/b\ncold_archive_root: /elsewhere\n"))


def test_legacy_mode_forces_cleanup_off(legacy_mode, tmp_path, capsys):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\ncleanup_loose_after_indexing: true\n"))
    assert cfg["cleanup_loose_after_indexing"] is False
    assert "legacy" in capsys.readouterr().out


def test_legacy_mode_archive_volume_warns_and_is_ignored(legacy_mode, tmp_path, capsys):
    cfg, _ = load_config(_write_yaml(
        tmp_path, "src_dir: /data/b\narchive_volume: disk2\n"))
    assert cfg["archive_volume"] is None
    assert cfg["cold_archive_root"] is None
    assert "mode is 'legacy'" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def _write_links(tmp_path, body):
    p = tmp_path / "links.json"
    p.write_text(body, encoding="utf-8")
    return p


def test_patient_links_default_off(cold_mode, tmp_path):
    cfg, _ = load_config(_write_yaml(tmp_path, "src_dir: /data/b\n"))
    assert cfg["patient_links"] is None
    assert cfg["_patient_link_rows"] == []


def test_patient_links_relative_to_yaml(cold_mode, tmp_path):
    _write_links(tmp_path, '{"dataset": "TEST", "link_dataset": "CRISP2/LVO", '
                           '"links": {"OL-1": "4-1391", "OL-2": "2-456"}}')
    cfg, _ = load_config(_write_yaml(tmp_path, "src_dir: /data/b\npatient_links: links.json\n"))
    assert cfg["patient_links"] == str(tmp_path / "links.json")
    assert [(r["patient_id"], r["link_dataset"], r["link_patient_id"])
            for r in cfg["_patient_link_rows"]] == [
        ("OL-1", "CRISP2/LVO", "4-1391"), ("OL-2", "CRISP2/LVO", "2-456")]
    assert {r["dataset"] for r in cfg["_patient_link_rows"]} == {"TEST"}


def test_patient_links_dataset_must_match_batch(cold_mode, tmp_path):
    _write_links(tmp_path, '{"dataset": "OTHER", "link_dataset": "CRISP2/LVO", '
                           '"links": {"OL-1": "4-1391"}}')
    with pytest.raises(ValueError, match="is for dataset 'OTHER'"):
        load_config(_write_yaml(tmp_path, "src_dir: /data/b\npatient_links: links.json\n"))


@pytest.mark.parametrize("body, message", [
    ("not json", "cannot read"),
    ('{"dataset": "TEST", "links": {"OL-1": "4-1391"}}', "link_dataset"),
    ('{"dataset": "TEST", "link_dataset": "X", "links": {}}', "non-empty object"),
    ('{"dataset": "TEST", "link_dataset": "X", "links": {"OL-1": 41391}}', "as strings"),
    ('{"dataset": "TEST", "link_dataset": "X", "links": {"OL-1": "a", "OL-1": "b"}}',
     "more than once"),
])
def test_patient_links_malformed(cold_mode, tmp_path, body, message):
    _write_links(tmp_path, body)
    with pytest.raises(ValueError, match=message):
        load_config(_write_yaml(tmp_path, "src_dir: /data/b\npatient_links: links.json\n"))


def test_patient_links_missing_file(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        load_config(_write_yaml(tmp_path, "src_dir: /data/b\npatient_links: nope.json\n"))


def test_on_patient_mismatch_defaults_to_refuse(cold_mode, tmp_path):
    cfg, _ = load_config(_write_yaml(tmp_path, "src_dir: /data/b\n"))
    assert cfg["on_patient_mismatch"] == "refuse"


def test_reassign_requires_overwrite(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="requires overwrite_if_exists"):
        load_config(_write_yaml(
            tmp_path, "src_dir: /data/b\non_patient_mismatch: reassign\n"))
    cfg, _ = load_config(_write_yaml(
        tmp_path,
        "src_dir: /data/b\non_patient_mismatch: reassign\noverwrite_if_exists: true\n"))
    assert cfg["on_patient_mismatch"] == "reassign"


def test_invalid_on_patient_mismatch_raises(cold_mode, tmp_path):
    with pytest.raises(ValueError, match="on_patient_mismatch"):
        load_config(_write_yaml(
            tmp_path, "src_dir: /data/b\non_patient_mismatch: merge\n"))
