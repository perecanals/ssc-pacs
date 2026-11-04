"""Load general-purpose settings from repo-root `config.toml` (non-secrets).

`config.toml` is the single source of truth for non-secret operational settings
(storage mode + paths, cold-cache tuning, session/auth). It is required and
**per-host** (gitignored, like `.env`): a deployment copies the committed
`config.example.toml` to `config.toml` and edits it in place. Installation-
specific values (storage mode, data roots) have **no built-in defaults** — the
app refuses to start until they are configured. Benign tuning knobs fall back
to the defaults below with a warning. Secrets and docker-compose variables
remain in `.env`. See docs/reference/configuration_sources.md.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from archive_volumes import ArchiveVolume, parse_archive_volumes

logger = logging.getLogger(__name__)

_WEB_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WEB_APP_DIR.parent
_CONFIG_PATH = _REPO_ROOT / "config.toml"

# Installation-specific keys: no built-in value could be correct on another
# host, so a missing key is a hard startup error, never a silent fallback.
_REQUIRED_KEYS = {
    "storage": ("mode", "dicom_data_root"),
}
# Plus at least one archive volume: [storage.archive_volumes.<name>] tables, or
# the deprecated single [storage] cold_archive_root (= one volume, "disk1").

_VALID_STORAGE_MODES = ("legacy", "cold_path_cache")

# Benign tuning knobs only — safe on any host. Installation-specific values
# (see _REQUIRED_KEYS) deliberately have no entry here.
_DEFAULT_STORAGE = {
    "eviction_ttl_hours": 24.0,
    "warming_timeout_minutes": 30.0,
    "warming_disk_safety_factor": 3.0,
    "warming_disk_min_free_bytes": 100 * 1024 * 1024,
    "warm_workers": 2,
}
_DEFAULT_WEB_APP = {
    # The port is bound by uvicorn (rendered into the service units at install
    # time), not by app code — exposed here so the startup log shows it.
    "port": 8043,
    "session_timeout_hours": 2.0,
    "session_absolute_timeout_hours": 24.0,
    "cookie_secure": True,
    "login_rate_limit_per_5min": 10,
    # Trackpad slice-scroll damping threshold for the OHIF proxy shim
    # (routes/proxy.py): pixels of trackpad scroll per slice. 0 disables
    # injection.
    "ohif_trackpad_px_per_slice": 50,
    "ohif_annotation_max_upload_bytes": 256 * 1024 * 1024,
}


def _load_and_validate(path: Path) -> tuple[dict, dict, list[str]]:
    """Load config.toml, enforce required keys, overlay benign defaults.

    Returns ``(storage, web_app, fellback_keys)`` where ``fellback_keys``
    names the benign keys that fell back to a built-in default (reported once
    by the caller). Raises ``RuntimeError`` when the file is absent, a
    required key is missing, or ``storage.mode`` is invalid — all of these
    must be fixed in config.toml before the app may start.
    """
    if not path.is_file():
        raise RuntimeError(
            f"Required config file not found: {path}. "
            "config.toml is the source of truth for non-secret settings — "
            "copy config.example.toml to config.toml and set the "
            "installation-specific values for this host. "
            "See docs/reference/configuration_sources.md."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)

    missing = []
    for section, keys in _REQUIRED_KEYS.items():
        sec = raw.get(section)
        for key in keys:
            if not isinstance(sec, dict) or key not in sec:
                missing.append(f"[{section}] {key}")
    sec = raw.get("storage")
    if not isinstance(sec, dict) or ("archive_volumes" not in sec and "cold_archive_root" not in sec):
        missing.append("[storage.archive_volumes.<name>] (or the legacy [storage] cold_archive_root)")
    if missing:
        raise RuntimeError(
            f"config.toml ({path}) is missing required key(s): "
            + ", ".join(missing)
            + ". These values are installation-specific and have no built-in "
            "default — configure them before starting the app (see "
            "config.example.toml and docs/reference/configuration_sources.md)."
        )

    fellback: list[str] = []

    def merge(name: str, defaults: dict) -> dict:
        """Overlay config.toml [name] over `defaults`, recording missing keys."""
        merged = dict(defaults)
        section = raw.get(name)
        if isinstance(section, dict):
            merged.update(section)
            fellback.extend(f"{name}.{k}" for k in defaults if k not in section)
        else:
            fellback.extend(f"{name}.{k}" for k in defaults)
        return merged

    storage = merge("storage", _DEFAULT_STORAGE)
    web_app = merge("web-app", _DEFAULT_WEB_APP)
    upload_limit = web_app["ohif_annotation_max_upload_bytes"]
    if type(upload_limit) is not int or upload_limit <= 0:
        raise RuntimeError("config.toml [web-app].ohif_annotation_max_upload_bytes must be a positive integer")

    mode = str(storage["mode"]).strip().lower()
    if mode not in _VALID_STORAGE_MODES:
        raise RuntimeError(
            f"config.toml [storage] mode = {storage['mode']!r} is not one of "
            f"{_VALID_STORAGE_MODES}. Fix it before starting the app."
        )
    storage["mode"] = mode
    # Parsed volumes ride along in the storage dict (validated here so a bad
    # volume table aborts startup like any other required key).
    (storage["_volumes"], storage["_default_volume"],
     storage["_dataset_volumes"], storage["_legacy_volumes"]) = parse_archive_volumes(
        storage, Path(str(storage["dicom_data_root"])).resolve(),
    )
    return storage, web_app, fellback


_storage, _web_app, _fellback_keys = _load_and_validate(_CONFIG_PATH)

if _fellback_keys:
    logger.warning(
        "config.toml is missing %d key(s); using built-in defaults: %s",
        len(_fellback_keys),
        ", ".join(sorted(_fellback_keys)),
    )

# Required keys — guaranteed present by _load_and_validate, no .get fallbacks.
STORAGE_MODE = _storage["mode"]
DICOM_DATA_ROOT = Path(str(_storage["dicom_data_root"])).resolve()
ARCHIVE_VOLUMES: dict[str, ArchiveVolume] = _storage["_volumes"]
DEFAULT_ARCHIVE_VOLUME: str = _storage["_default_volume"]
DATASET_ARCHIVE_VOLUME: dict[str, str] = _storage["_dataset_volumes"]
# The DEFAULT volume's root, for callers that genuinely mean "where new
# archives go by default" (e.g. archive_all_series). Never use it to decide
# whether a path is an archive — that is archive_volumes.volume_for_path.
COLD_ARCHIVE_ROOT = ARCHIVE_VOLUMES[DEFAULT_ARCHIVE_VOLUME].root
if _storage["_legacy_volumes"]:
    logger.warning(
        "config.toml [storage] cold_archive_root is deprecated: it is read as one "
        "archive volume %r. Replace it with [storage.archive_volumes.%s] root = ... "
        "(docs/cold_storage/archive_volumes.md).", DEFAULT_ARCHIVE_VOLUME, DEFAULT_ARCHIVE_VOLUME,
    )


def _linked_view_root(raw) -> Path | None:
    """Optional symlink view of linked imaging (web-app/linked_view.py).

    Must sit outside every storage root (and they outside it): anything that
    walks a root — Orthanc, the audits, deletion's orphan sweep, backups —
    would otherwise meet every shared study twice.
    """
    if raw in (None, ""):
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        raise RuntimeError(f"config.toml [storage] linked_view_root must be absolute (got {raw!r})")
    path = path.resolve()
    for root in (DICOM_DATA_ROOT, *(v.root for v in ARCHIVE_VOLUMES.values())):
        if path == root or path.is_relative_to(root) or root.is_relative_to(path):
            raise RuntimeError(
                f"config.toml [storage] linked_view_root ({path}) must be outside "
                f"the storage roots (overlaps {root})."
            )
    return path


LINKED_VIEW_ROOT = _linked_view_root(_storage.get("linked_view_root"))

# Benign knobs — present via the defaults overlay.
EVICTION_TTL_HOURS = float(_storage["eviction_ttl_hours"])
WARMING_TIMEOUT_MINUTES = float(_storage["warming_timeout_minutes"])
WARMING_DISK_SAFETY_FACTOR = float(_storage["warming_disk_safety_factor"])
WARMING_DISK_MIN_FREE_BYTES = int(_storage["warming_disk_min_free_bytes"])
WARM_WORKERS = int(_storage["warm_workers"])

WEB_APP_PORT = int(_web_app["port"])

SESSION_TIMEOUT_HOURS = float(_web_app["session_timeout_hours"])
SESSION_ABSOLUTE_TIMEOUT_HOURS = float(_web_app["session_absolute_timeout_hours"])
COOKIE_SECURE = bool(_web_app["cookie_secure"])
LOGIN_RATE_LIMIT_PER_5MIN = int(_web_app["login_rate_limit_per_5min"])

# Moved to the `dataset` table (Alembic 0027: clinical_table /
# clinical_id_column / clinical_date_column / timepoint_strategy, set by
# scripts/admin/manage_datasets.py import-clinical). Still present in an older
# config.toml: ignored, with a hint.
_RETIRED_WEB_APP_KEYS = ("clinical_episode_date_column", "clinical_data_dataset")
for _key in _RETIRED_WEB_APP_KEYS:
    if _key in _web_app:
        logger.warning(
            "config.toml [web-app] %s is ignored: clinical tables are configured "
            "per dataset now (scripts/admin/manage_datasets.py import-clinical). "
            "Remove the key.", _key,
        )

OHIF_ANNOTATION_MAX_UPLOAD_BYTES = _web_app["ohif_annotation_max_upload_bytes"]

OHIF_TRACKPAD_PX_PER_SLICE = int(_web_app["ohif_trackpad_px_per_slice"])
if OHIF_TRACKPAD_PX_PER_SLICE < 0:
    raise RuntimeError(
        "config.toml [web-app].ohif_trackpad_px_per_slice must be >= 0 "
        f"(got {OHIF_TRACKPAD_PX_PER_SLICE}); use 0 to disable the shim."
    )


def effective_config_summary() -> dict:
    """Resolved non-secret settings, for a one-line startup log.

    Lets operators confirm from the journal which config.toml actually took
    effect (catches a stale file, wrong path, or fallback drift).
    """
    return {
        "config_path": str(_CONFIG_PATH),
        "port": WEB_APP_PORT,
        "storage_mode": STORAGE_MODE,
        "dicom_data_root": str(DICOM_DATA_ROOT),
        "archive_volumes": {n: str(v.root) for n, v in ARCHIVE_VOLUMES.items()},
        "default_archive_volume": DEFAULT_ARCHIVE_VOLUME,
        "dataset_archive_volume": DATASET_ARCHIVE_VOLUME,
        "linked_view_root": str(LINKED_VIEW_ROOT) if LINKED_VIEW_ROOT else None,
        "eviction_ttl_hours": EVICTION_TTL_HOURS,
        "warm_workers": WARM_WORKERS,
        "session_timeout_hours": SESSION_TIMEOUT_HOURS,
        "session_absolute_timeout_hours": SESSION_ABSOLUTE_TIMEOUT_HOURS,
        "cookie_secure": COOKIE_SECURE,
        "login_rate_limit_per_5min": LOGIN_RATE_LIMIT_PER_5MIN,
        "ohif_trackpad_px_per_slice": OHIF_TRACKPAD_PX_PER_SLICE,
        "ohif_annotation_max_upload_bytes": OHIF_ANNOTATION_MAX_UPLOAD_BYTES,
    }
