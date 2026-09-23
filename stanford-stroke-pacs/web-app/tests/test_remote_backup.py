"""Safety checks for the opt-in remote backup runner; no network or DB needed."""

import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "remote_backup", Path(__file__).resolve().parents[2] / "scripts/backup/remote_backup.py",
)
remote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "STACK", tmp_path)
    (tmp_path / ".env").write_text("DB_NAME=stanford-stroke\nPG_ORTHANC_DB=orthanc_db\n")
    for name in ("key", "password"):
        (tmp_path / name).write_text("test-only")
        (tmp_path / name).chmod(0o600)
    sources = tmp_path / "sources"
    sources.mkdir()
    config = {
        "backup": {"backup_root": str(sources / "dumps"), "max_age_hours": 36},
        "storage": {"cold_archive_root": str(sources / "cold")},
        "remote_backup": {
            "enabled": True, "host": "backup.example", "user": "backup", "backup_id": "test",
            "remote_root": "/disk/backups", "remote_mount": "/disk",
            "ssh_key_file": str(tmp_path / "key"), "password_file": str(tmp_path / "password"),
            "state_dir": str(tmp_path / "state"), "tier1_mount": "/", "imaging_mount": "/",
        },
    }
    for name, suffix in (("stanford-stroke", "dump"), ("orthanc_db", "dump"), ("orthanc_storage", "tar.gz")):
        directory = sources / "dumps" / name
        directory.mkdir(parents=True)
        artifact = directory / f"20260916T000000Z.{suffix}"
        artifact.write_bytes(b"synthetic backup")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        Path(str(artifact) + ".sha256").write_text(f"{digest}  /old/server/{artifact.name}\n")
        (directory / f"latest.{suffix}").symlink_to(artifact.name)
    (sources / "cold").mkdir()
    (sources / "cold" / "sample.tar.zst").write_bytes(b"synthetic archive")
    return config


def test_disabled_prevents_state_and_network(configured):
    configured["remote_backup"]["enabled"] = False
    with pytest.raises(ValueError, match="disabled"):
        remote.Backup(configured, "tier1")
    assert not Path(configured["remote_backup"]["state_dir"]).exists()


def test_selects_completed_backups_and_ignores_absolute_checksum_paths(configured):
    root = Path(configured["backup"]["backup_root"])
    (root / "stanford-stroke" / "new.dump.partial").write_bytes(b"unfinished")
    paths, stamp = remote.latest_artifacts(root, ["stanford-stroke", "orthanc_db"], 36 * 3600)
    assert len(paths) == 6
    assert all("partial" not in str(p) for p in paths)
    assert time.time() - stamp < 60


@pytest.mark.parametrize("problem", ["corrupt", "stale", "escape", "empty"])
def test_bad_local_artifact_rejected(configured, tmp_path, problem):
    root = Path(configured["backup"]["backup_root"])
    latest = root / "stanford-stroke" / "latest.dump"
    artifact = latest.resolve()
    if problem == "corrupt":
        artifact.write_bytes(b"changed")
    elif problem == "empty":
        artifact.write_bytes(b"")
    elif problem == "stale":
        os.utime(artifact, (1, 1))
    else:
        outside = tmp_path / "outside.dump"
        outside.write_bytes(b"outside")
        latest.unlink()
        latest.symlink_to(outside)
    with pytest.raises(ValueError):
        remote.latest_artifacts(root, ["stanford-stroke", "orthanc_db"], 36 * 3600)


def test_missing_mount_rejected(tmp_path):
    source = tmp_path / "missing-mount" / "cold"
    source.mkdir(parents=True)
    with pytest.raises(ValueError, match="not mounted"):
        remote.source_directory(str(source), str(source.parent))


@pytest.mark.parametrize("field", ["password_file", "state_dir"])
def test_secrets_and_state_cannot_be_backed_up(configured, field):
    cfg = configured["remote_backup"]
    dest = Path(configured["backup"]["backup_root"]) / "secret"
    if field == "password_file":
        dest.write_bytes(b"private")
        dest.chmod(0o600)
    cfg[field] = str(dest)
    with pytest.raises(ValueError, match="outside backup sources"):
        remote.Backup(configured, "tier1")


def test_incomplete_upload_keeps_previous_success_record(configured, monkeypatch):
    job = remote.Backup(configured, "imaging")
    job.state.write_text('{"previous": true}')
    monkeypatch.setattr(job, "remote_check", Mock())
    monkeypatch.setattr(job, "restic", Mock(side_effect=subprocess.CalledProcessError(3, "restic")))
    with pytest.raises(subprocess.CalledProcessError):
        job.backup()
    assert json.loads(job.state.read_text()) == {"previous": True}


def test_dry_run_does_not_publish_success(configured, monkeypatch):
    job = remote.Backup(configured, "imaging")
    monkeypatch.setattr(job, "remote_check", Mock())
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"message_type":"summary"}\n'))
    monkeypatch.setattr(job, "restic", run)
    job.backup(dry_run=True)
    assert not job.state.exists()
    assert "--dry-run" in run.call_args.args


def test_missing_remote_snapshot_fails_freshness(configured, monkeypatch):
    job = remote.Backup(configured, "tier1")
    job.state.write_text(json.dumps({
        "repository": job.repository, "backup_id": "test", "snapshot_id": "a" * 64,
        "completed_at": time.time(), "source_time": time.time(),
    }))
    monkeypatch.setattr(job, "restic", Mock(return_value=subprocess.CompletedProcess([], 0, "[]")))
    with pytest.raises(ValueError, match="missing remotely"):
        job.freshness()


def test_retention_groups_dated_dumps_together_and_requires_freshness(configured, monkeypatch):
    job = remote.Backup(configured, "tier1")
    monkeypatch.setattr(job, "remote_check", Mock())
    monkeypatch.setattr(job, "freshness", Mock())
    run = Mock()
    monkeypatch.setattr(job, "restic", run)
    job.run("maintain", dry_run=True)
    args = run.call_args.args
    assert args[args.index("--group-by") + 1] == "host,tags"
    assert "--dry-run" in args and "--prune" not in args
    run.reset_mock()
    monkeypatch.setattr(job, "freshness", Mock(side_effect=ValueError("stale")))
    with pytest.raises(ValueError, match="stale"):
        job.run("maintain")
    run.assert_not_called()


def test_ssh_and_restic_settings_do_not_inherit_repository_override(configured, monkeypatch):
    job = remote.Backup(configured, "tier1")
    monkeypatch.setenv("RESTIC_REPOSITORY", "/wrong")
    monkeypatch.setenv("RESTIC_PASSWORD", "do-not-forward")
    run = Mock()
    monkeypatch.setattr(remote.subprocess, "run", run)
    job.restic("snapshots")
    args, kwargs = run.call_args
    assert "StrictHostKeyChecking=yes" in args[0][2]
    assert "BatchMode=yes" in args[0][2]
    assert kwargs["env"]["RESTIC_REPOSITORY"] == job.repository
    assert "RESTIC_PASSWORD" not in kwargs["env"]


def test_restore_include_limits_to_absolute_source_paths(configured, monkeypatch, tmp_path):
    job = remote.Backup(configured, "imaging")
    monkeypatch.setattr(job, "remote_check", Mock())
    run = Mock()
    monkeypatch.setattr(job, "restic", run)
    job.run("restore", target=str(tmp_path / "new"), snapshot="b" * 64,
            include=["/data/cold/a/DICOM.tar.zst", "/data/cold/b/DICOM.tar.zst"])
    args = run.call_args.args
    assert args.count("--include") == 2 and "/data/cold/b/DICOM.tar.zst" in args
    run.reset_mock()
    with pytest.raises(ValueError, match="absolute"):
        job.run("restore", target=str(tmp_path / "new"), snapshot="b" * 64, include=["../escape"])
    run.assert_not_called()


def test_restore_never_overwrites_existing_directory(configured, monkeypatch, tmp_path):
    job = remote.Backup(configured, "tier1")
    monkeypatch.setattr(job, "remote_check", Mock())
    run = Mock()
    monkeypatch.setattr(job, "restic", run)
    with pytest.raises(ValueError, match="already exist"):
        job.run("restore", target=str(tmp_path), snapshot="a" * 64)
    run.assert_not_called()


def test_initial_imaging_upload_budgets_source_size(configured, monkeypatch):
    job = remote.Backup(configured, "imaging")
    root = Path(configured["storage"]["cold_archive_root"])
    (root / "unfinished.tmp").write_bytes(b"x" * 500)
    check = Mock()
    monkeypatch.setattr(job, "remote_check", check)
    monkeypatch.setattr(job, "restic", Mock(return_value=subprocess.CompletedProcess(
        [], 0, '{"message_type":"summary"}\n',
    )))
    job.backup(dry_run=True)
    check.assert_called_once_with(len(b"synthetic archive"))


def test_remote_guard_rejects_unmounted_directory(tmp_path):
    mount = tmp_path / "disk"
    root = mount / "repo"
    root.mkdir(parents=True)
    result = subprocess.run(
        ["python", "-c", remote.REMOTE_CHECK, str(root), str(mount), "0"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "not mounted" in result.stderr


def test_remote_guard_reports_free_space_from_df(tmp_path):
    # macOS statvfs() wraps at 32 bits; the guard must agree with df instead.
    mount = tmp_path
    while not os.path.ismount(mount):
        mount = mount.parent
    root = tmp_path / "repo"
    root.mkdir()
    result = subprocess.run(
        ["python", "-c", remote.REMOTE_CHECK, str(root), str(mount), "0"],
        capture_output=True, text=True, check=True,
    )
    report = subprocess.run(["df", "-Pk", str(root)], capture_output=True, text=True, check=True)
    expected = int(report.stdout.strip().splitlines()[-1].split()[3]) * 1024
    free = json.loads(result.stdout)["free_bytes"]
    assert free > 0 and abs(free - expected) < 64 * 2**20


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("script", ["start_stack.sh", "stop_stack.sh"])
def test_stack_lifecycle_preserves_remote_opt_in(tmp_path, enabled, script):
    executable = tmp_path / "systemctl"
    executable.write_text(f"#!/bin/sh\nexit {0 if enabled else 1}\n")
    executable.chmod(0o700)
    path = Path(__file__).resolve().parents[2] / "scripts/linux" / script
    result = subprocess.run(
        ["bash", str(path), "--dry-run", *(["--enable"] if script == "start_stack.sh" else [])],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True, text=True, check=True,
    )
    assert ("pacs-remote-backup-imaging.timer" in result.stdout) is enabled


def test_database_names_follow_env_and_freshness_follows_shared_policy(configured, tmp_path):
    (tmp_path / ".env").write_text("DB_NAME=renamed_research\nPG_ORTHANC_DB=renamed_index\n")
    configured["backup"]["max_age_hours"] = 12
    job = remote.Backup(configured, "tier1")
    assert job.database_names == ["renamed_research", "renamed_index"]
    assert job.max_age == 12 * 3600


@pytest.mark.parametrize("key,value", [("database_names", ["old", "names"]), ("max_age_hours", 99)])
def test_obsolete_remote_duplicates_are_rejected(configured, key, value):
    configured["remote_backup"][key] = value
    with pytest.raises(ValueError, match="Remove remote"):
        remote.Backup(configured, "tier1")
