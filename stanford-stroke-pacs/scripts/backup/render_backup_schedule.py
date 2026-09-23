#!/usr/bin/env python3
"""Render Linux backup timer policy from config.toml; no host mutations."""

import sys
import tomllib
from pathlib import Path


def render(text: str, unit: str, config: dict, defaults: dict) -> str:
    if "__BACKUP_CALENDAR__" not in text:
        return text
    schedules = config.get("backup", {}).get("schedules", {})
    known = defaults["backup"]["schedules"]
    if unknown := schedules.keys() - known.keys():
        raise ValueError(f"Unknown backup schedule names: {sorted(unknown)}")
    settings = {**known[unit], **schedules.get(unit, {})}
    if settings.keys() != {"calendar", "randomized_delay"}:
        raise ValueError(f"Invalid schedule keys for {unit}")
    for key, token in (("calendar", "__BACKUP_CALENDAR__"), ("randomized_delay", "__BACKUP_JITTER__")):
        value = settings[key]
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"Invalid {key} for {unit}")
        text = text.replace(token, value)
    return text


def main():
    template, config_file, default_file = map(Path, sys.argv[1:])
    config = tomllib.loads(config_file.read_text()) if config_file.exists() else {}
    defaults = tomllib.loads(default_file.read_text())
    sys.stdout.write(render(
        sys.stdin.read(), template.name.removesuffix(".timer.in"), config, defaults,
    ))


if __name__ == "__main__":
    main()
