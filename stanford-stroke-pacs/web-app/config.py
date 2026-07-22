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
import re
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

_WEB_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WEB_APP_DIR.parent
_CONFIG_PATH = _REPO_ROOT / "config.toml"

# Installation-specific keys: no built-in value could be correct on another
# host, so a missing key is a hard startup error, never a silent fallback.
_REQUIRED_KEYS = {
    "storage": ("mode", "dicom_data_root", "cold_archive_root"),
}

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
    # Which clinical_data column supplies the patient tab's episode date
    # (COALESCEd over the imaging-derived patient.stroke_date).
    "clinical_episode_date_column": "stroke_date",
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

    mode = str(storage["mode"]).strip().lower()
    if mode not in _VALID_STORAGE_MODES:
        raise RuntimeError(
            f"config.toml [storage] mode = {storage['mode']!r} is not one of "
            f"{_VALID_STORAGE_MODES}. Fix it before starting the app."
        )
    storage["mode"] = mode
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
COLD_ARCHIVE_ROOT = Path(str(_storage["cold_archive_root"])).resolve()

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
    _web_app["clinical_episode_date_column"],
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
