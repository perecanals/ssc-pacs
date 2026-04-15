"""Load general-purpose settings from repo-root `config.toml` (non-secrets).

Secrets and docker-compose variables remain in `.env`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_COMPANION_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _COMPANION_DIR.parent
_CONFIG_PATH = _REPO_ROOT / "config.toml"

_DEFAULT_STORAGE = {
    "mode": "legacy",
    "legacy_dicom_root": "/DATA2/pacs_imaging_data",
    "cold_archive_root": "/DATA2/pacs_imaging_data_compressed",
    "hot_cache_dir": "/DATA2/pacs_hot_cache",
    "eviction_ttl_hours": 24.0,
}
_DEFAULT_COMPANION = {
    "session_timeout_hours": 2.0,
}


def _load_toml() -> tuple[dict, dict]:
    storage = dict(_DEFAULT_STORAGE)
    companion = dict(_DEFAULT_COMPANION)
    if not _CONFIG_PATH.is_file():
        return storage, companion
    with _CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)
    if isinstance(raw.get("storage"), dict):
        storage.update(raw["storage"])
    if isinstance(raw.get("companion"), dict):
        companion.update(raw["companion"])
    return storage, companion


_storage, _companion = _load_toml()

STORAGE_MODE = str(_storage.get("mode", "legacy")).strip().lower()
LEGACY_DICOM_ROOT = Path(str(_storage.get("legacy_dicom_root", _DEFAULT_STORAGE["legacy_dicom_root"]))).resolve()
COLD_ARCHIVE_ROOT = Path(str(_storage.get("cold_archive_root", _DEFAULT_STORAGE["cold_archive_root"]))).resolve()
HOT_CACHE_DIR = Path(str(_storage.get("hot_cache_dir", _DEFAULT_STORAGE["hot_cache_dir"]))).resolve()
EVICTION_TTL_HOURS = float(_storage.get("eviction_ttl_hours", _DEFAULT_STORAGE["eviction_ttl_hours"]))

SESSION_TIMEOUT_HOURS = float(
    _companion.get("session_timeout_hours", _DEFAULT_COMPANION["session_timeout_hours"])
)
