"""Module settings; credentials stay in .env, operational knobs in config.toml."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from config import _CONFIG_PATH


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    spool_dir: str = ""
    queue_limit: int = 10
    timeout_seconds: int = 1800
    preview_timeout_seconds: int = 15
    artifact_bytes: int = 5 * 1024**3
    spool_bytes: int = 10 * 1024**3
    reserve_bytes: int = 20 * 1024**3
    retention_hours: int = 24


def load_settings():
    with _CONFIG_PATH.open("rb") as handle:
        section = tomllib.load(handle).get("data-explorer", {})
    settings = Settings(**section)
    if not isinstance(settings.enabled, bool):
        raise RuntimeError("[data-explorer] enabled must be a boolean")
    if settings.enabled and (not settings.spool_dir or not Path(settings.spool_dir).is_absolute()):
        raise RuntimeError("[data-explorer] spool_dir must be an absolute private directory")
    for key in (
        "queue_limit",
        "timeout_seconds",
        "preview_timeout_seconds",
        "artifact_bytes",
        "spool_bytes",
        "reserve_bytes",
        "retention_hours",
    ):
        if type(getattr(settings, key)) is not int or getattr(settings, key) <= 0:
            raise RuntimeError(f"[data-explorer] {key} must be a positive integer")
    return settings
