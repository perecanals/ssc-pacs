"""Backup schedule overrides and scheduled database selection stay centralized."""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("backup_schedule", STACK / "scripts/backup/render_backup_schedule.py")
schedule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedule)
DEFAULTS = tomllib.loads((STACK / "config.example.toml").read_text())


def test_timer_overrides_and_defaults_share_one_config_source():
    templates = STACK / "deploy/systemd"
    for name, settings in DEFAULTS["backup"]["schedules"].items():
        text = (templates / f"{name}.timer.in").read_text()
        rendered = schedule.render(text, name, {}, DEFAULTS)
        assert f"OnCalendar={settings['calendar']}" in rendered
        assert "__BACKUP_" not in rendered
        config = {"backup": {"schedules": {name: {"calendar": "daily", "randomized_delay": "1m"}}}}
        rendered = schedule.render(text, name, config, DEFAULTS)
        assert "OnCalendar=daily" in rendered
        assert "RandomizedDelaySec=1m" in rendered


def test_bad_schedule_fails_instead_of_silently_using_default():
    config = {"backup": {"schedules": {"typo-job": {"calendar": "daily"}}}}
    with pytest.raises(ValueError, match="Unknown"):
        schedule.render("__BACKUP_CALENDAR__", "pg-backup-orthanc", config, DEFAULTS)
    config = {"backup": {"schedules": {"pg-backup-orthanc": {"calendar": "daily\nBad=1"}}}}
    with pytest.raises(ValueError, match="Invalid"):
        schedule.render("__BACKUP_CALENDAR__", "pg-backup-orthanc", config, DEFAULTS)


@pytest.mark.parametrize("flag,expected", [("--web-app", "custom_research"), ("--orthanc", "custom_index")])
def test_scheduled_dump_uses_env_database_name(tmp_path, flag, expected):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DB_HOST=127.0.0.1\nDB_PORT=1\nDB_USER=test\nDB_PASSWORD=test-only\n"
        "DB_NAME=custom_research\nPG_ORTHANC_DB=custom_index\n",
    )
    binary = tmp_path / "pg_dump"
    # This double never connects to PostgreSQL. Verify the target passed to
    # pg_dump and produce a tiny artifact for the real script's bookkeeping.
    binary.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in --file=*) printf synthetic > \"${arg#--file=}\" ;; esac\n"
        "  last=\"$arg\"\n"
        "done\n"
        "test \"$last\" = \"$EXPECTED_DB\"\n",
    )
    binary.chmod(0o700)
    env = {
        **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "BACKUP_ENV_FILE": str(env_file), "BACKUP_ROOT": str(tmp_path / "backups"), "EXPECTED_DB": expected,
    }
    subprocess.run(["bash", str(STACK / "scripts/backup/backup_pg_db.sh"), flag], env=env, check=True)
    assert (tmp_path / "backups" / expected / "latest.dump").exists()


def test_legacy_mirror_and_monitor_share_toml_config(tmp_path):
    scripts = tmp_path / "scripts"
    (scripts / "backup").mkdir(parents=True)
    (scripts / "cold_storage").mkdir()
    for relative in ("_lib.sh", "backup/check_backup_freshness.sh", "cold_storage/mirror_cold_archive.sh"):
        shutil.copyfile(STACK / "scripts" / relative, scripts / relative)
    source = tmp_path / "cold"
    destination = tmp_path / "mirror"
    source.mkdir()
    destination.mkdir()
    backups = tmp_path / "backups"
    for db, filename in (("research", "latest.dump"), ("index", "latest.dump"), ("orthanc_storage", "latest.tar.gz")):
        (backups / db).mkdir(parents=True)
        (backups / db / filename).write_bytes(b"synthetic")
    (tmp_path / ".env").write_text("DB_NAME=research\nPG_ORTHANC_DB=index\n")
    (tmp_path / "config.toml").write_text(
        f'[storage]\ncold_archive_root="{source}"\n'
        f'[backup]\nbackup_root="{backups}"\nmax_age_hours=3\n'
        f'cold_mirror_dest="{destination}"\ncold_mirror_rsync_args="--bwlimit=123"\n',
    )
    # No network/copy: the stand-in records the actual arguments for assertions.
    executable = tmp_path / "rsync"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o700)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SSC_PYTHON": sys.executable}
    result = subprocess.run(
        ["bash", str(scripts / "cold_storage/mirror_cold_archive.sh"), "--dry-run"],
        env=env, check=True, text=True, capture_output=True,
    )
    assert str(destination) in result.stdout and "--bwlimit=123" in result.stdout
    old = time.time() - 5 * 3600
    os.utime(destination, (old, old))
    result = subprocess.run(
        ["bash", str(scripts / "backup/check_backup_freshness.sh")],
        env=env, text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "STALE: cold_mirror" in result.stdout
