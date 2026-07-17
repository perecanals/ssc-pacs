"""Load general-purpose settings from repo-root `config.toml` (non-secrets).

`config.toml` is the single source of truth for non-secret operational settings
(storage mode + paths, cold-cache tuning, session/auth). It is required and ships
in the repo — a fresh deployment edits it in place rather than relying on these
built-in fallbacks. Secrets and docker-compose variables remain in `.env`.
See docs/reference/configuration_sources.md.
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

_WEB_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WEB_APP_DIR.parent
_CONFIG_PATH = _REPO_ROOT / "config.toml"

_DEFAULT_STORAGE = {
    "mode": "legacy",
    "dicom_data_root": "/DATA2/pacs_imaging_data",
    "cold_archive_root": "/DATA2/pacs_imaging_data_compressed",
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
    # Which clinical_data column supplies the patient tab's episode date
    # (COALESCEd over the imaging-derived patient.stroke_date).
    "clinical_episode_date_column": "stroke_date",
}


# Keys that fell back to a built-in default because they were absent from a
# present config.toml (populated by _merge, reported once below).
_fellback_keys: list[str] = []


def _merge(name: str, defaults: dict, raw: dict) -> dict:
    """Overlay config.toml [name] over `defaults`, recording missing keys."""
    merged = dict(defaults)
    section = raw.get(name)
    if isinstance(section, dict):
        merged.update(section)
        _fellback_keys.extend(f"{name}.{k}" for k in defaults if k not in section)
    else:
        _fellback_keys.extend(f"{name}.{k}" for k in defaults)
    return merged


def _load_toml() -> tuple[dict, dict]:
    if not _CONFIG_PATH.is_file():
        raise RuntimeError(
            f"Required config file not found: {_CONFIG_PATH}. "
            "config.toml is the source of truth for non-secret settings and ships "
            "in the repo — copy/edit it for this host. "
            "See docs/reference/configuration_sources.md."
        )
    with _CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)
    return _merge("storage", _DEFAULT_STORAGE, raw), _merge("web-app", _DEFAULT_WEB_APP, raw)


_storage, _web_app = _load_toml()

if _fellback_keys:
    logger.warning(
        "config.toml is missing %d key(s); using built-in defaults: %s",
        len(_fellback_keys),
        ", ".join(sorted(_fellback_keys)),
    )

STORAGE_MODE = str(_storage.get("mode", "legacy")).strip().lower()
DICOM_DATA_ROOT = Path(str(_storage.get("dicom_data_root", _DEFAULT_STORAGE["dicom_data_root"]))).resolve()
COLD_ARCHIVE_ROOT = Path(str(_storage.get("cold_archive_root", _DEFAULT_STORAGE["cold_archive_root"]))).resolve()
EVICTION_TTL_HOURS = float(_storage.get("eviction_ttl_hours", _DEFAULT_STORAGE["eviction_ttl_hours"]))
WARMING_TIMEOUT_MINUTES = float(
    _storage.get("warming_timeout_minutes", _DEFAULT_STORAGE["warming_timeout_minutes"])
)
WARMING_DISK_SAFETY_FACTOR = float(
    _storage.get("warming_disk_safety_factor", _DEFAULT_STORAGE["warming_disk_safety_factor"])
)
WARMING_DISK_MIN_FREE_BYTES = int(
    _storage.get("warming_disk_min_free_bytes", _DEFAULT_STORAGE["warming_disk_min_free_bytes"])
)
WARM_WORKERS = int(_storage.get("warm_workers", _DEFAULT_STORAGE["warm_workers"]))

WEB_APP_PORT = int(_web_app.get("port", _DEFAULT_WEB_APP["port"]))

SESSION_TIMEOUT_HOURS = float(
    _web_app.get("session_timeout_hours", _DEFAULT_WEB_APP["session_timeout_hours"])
)
SESSION_ABSOLUTE_TIMEOUT_HOURS = float(
    _web_app.get(
        "session_absolute_timeout_hours",
        _DEFAULT_WEB_APP["session_absolute_timeout_hours"],
    )
)
COOKIE_SECURE = bool(
    _web_app.get("cookie_secure", _DEFAULT_WEB_APP["cookie_secure"])
)
LOGIN_RATE_LIMIT_PER_5MIN = int(
    _web_app.get(
        "login_rate_limit_per_5min",
        _DEFAULT_WEB_APP["login_rate_limit_per_5min"],
    )
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _require_sql_identifier(value: object, key: str) -> str:
    """Reject anything that is not a bare SQL identifier.

    The value is interpolated into SQL unquoted, so this is the injection
    gate: fail fast at import rather than 500 on every request. Lowercased to
    match Postgres's folding of unquoted identifiers, so the schema check in
    routes/studies.py agrees with what the query planner sees.
    """
    ident = str(value).strip().lower()
    if not _IDENTIFIER_RE.fullmatch(ident):
        raise RuntimeError(
            f"config.toml [web-app] {key} = {value!r} is not a valid SQL "
            "identifier (must match ^[A-Za-z_][A-Za-z0-9_]*$). Refusing to start."
        )
    return ident


CLINICAL_EPISODE_DATE_COLUMN = _require_sql_identifier(
    _web_app.get(
        "clinical_episode_date_column",
        _DEFAULT_WEB_APP["clinical_episode_date_column"],
    ),
    "clinical_episode_date_column",
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
        "cold_archive_root": str(COLD_ARCHIVE_ROOT),
        "eviction_ttl_hours": EVICTION_TTL_HOURS,
        "warm_workers": WARM_WORKERS,
        "session_timeout_hours": SESSION_TIMEOUT_HOURS,
        "session_absolute_timeout_hours": SESSION_ABSOLUTE_TIMEOUT_HOURS,
        "cookie_secure": COOKIE_SECURE,
        "login_rate_limit_per_5min": LOGIN_RATE_LIMIT_PER_5MIN,
        "clinical_episode_date_column": CLINICAL_EPISODE_DATE_COLUMN,
    }
