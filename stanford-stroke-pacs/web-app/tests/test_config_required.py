"""Tests for config.toml loading policy (`config._load_and_validate`).

Installation-specific keys (storage mode + data roots) have no built-in
defaults — missing ones must abort startup with a message naming them all.
Benign tuning knobs fall back to defaults and are merely reported.
"""

import pytest

import config as config_mod

VALID = """
[storage]
mode = "cold_path_cache"
dicom_data_root = "/data/imaging"
cold_archive_root = "/data/compressed"

[web-app]
port = 9000
"""


def _write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


class TestRequiredKeys:
    def test_missing_file_errors(self, tmp_path):
        with pytest.raises(RuntimeError, match="config.example.toml"):
            config_mod._load_and_validate(tmp_path / "config.toml")

    def test_missing_one_required_key_names_it(self, tmp_path):
        path = _write(tmp_path, """
[storage]
mode = "legacy"
cold_archive_root = "/data/compressed"
""")
        with pytest.raises(RuntimeError) as exc:
            config_mod._load_and_validate(path)
        assert "[storage] dicom_data_root" in str(exc.value)
        assert "cold_archive_root" not in str(exc.value)

    def test_missing_several_required_keys_lists_all(self, tmp_path):
        path = _write(tmp_path, "[web-app]\nport = 9000\n")
        with pytest.raises(RuntimeError) as exc:
            config_mod._load_and_validate(path)
        msg = str(exc.value)
        for key in ("[storage] mode", "[storage] dicom_data_root",
                    "[storage] cold_archive_root"):
            assert key in msg

    def test_invalid_storage_mode_rejected(self, tmp_path):
        path = _write(tmp_path, VALID.replace("cold_path_cache", "warm_fuzzy"))
        with pytest.raises(RuntimeError, match="warm_fuzzy"):
            config_mod._load_and_validate(path)


class TestBenignDefaults:
    def test_missing_benign_key_falls_back_without_error(self, tmp_path):
        storage, web_app, fellback = config_mod._load_and_validate(
            _write(tmp_path, VALID)
        )
        assert storage["warm_workers"] == 2  # built-in default
        assert "storage.warm_workers" in fellback
        assert web_app["session_timeout_hours"] == 2.0
        assert "web-app.session_timeout_hours" in fellback

    def test_full_valid_file_resolves(self, tmp_path):
        storage, web_app, _ = config_mod._load_and_validate(_write(tmp_path, VALID))
        assert storage["mode"] == "cold_path_cache"
        assert storage["dicom_data_root"] == "/data/imaging"
        assert storage["cold_archive_root"] == "/data/compressed"
        assert web_app["port"] == 9000

    def test_mode_normalized(self, tmp_path):
        storage, _, _ = config_mod._load_and_validate(
            _write(tmp_path, VALID.replace('"cold_path_cache"', '" Legacy "'))
        )
        assert storage["mode"] == "legacy"
