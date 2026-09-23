#!/usr/bin/env python3
"""Encrypted remote backups over SFTP. See docs/operations/remote_backups.md."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from dotenv import dotenv_values

STACK = Path(__file__).resolve().parents[2]
TIERS = ("tier1", "imaging")

# Run on either Linux or macOS. Fail before opening an SFTP repository if the
# external disk is absent, a symlink redirects the path, or space is exhausted.
REMOTE_CHECK = """
import json, os, subprocess, sys
from pathlib import Path
root, mount = map(Path, sys.argv[1:3])
reserve = int(sys.argv[3])
if not mount.is_absolute() or not os.path.ismount(mount):
    raise SystemExit('Remote backup disk is not mounted')
if root.resolve() != root or not root.is_dir() or mount not in root.parents:
    raise SystemExit('Remote root must be an existing directory below the mount, without symlinks')
if root.stat().st_dev != mount.stat().st_dev:
    raise SystemExit('Remote root is on the wrong filesystem')
for tier in ('tier1', 'imaging'):
    if (root / tier).is_symlink():
        raise SystemExit('Repository directory must not be a symlink')
# statvfs() wraps block counts at 32 bits on macOS, so a multi-terabyte
# destination under-reports free space. POSIX df prints full 1 KiB counts.
report = subprocess.run(['df', '-Pk', str(root)], check=True, capture_output=True, text=True)
free = int(report.stdout.strip().splitlines()[-1].split()[3]) * 1024
if free < reserve:
    raise SystemExit('Insufficient remote free space')
print(json.dumps({'free_bytes': free, 'mount': str(mount)}))
"""


def absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Expected an absolute path without '..': {path}")
    return path


def private_file(value: str) -> Path:
    path = absolute(value)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Must be an owner-only file owned by the job user: {path}")
    if info.st_size == 0:
        raise ValueError(f"Empty credential file: {path}")
    return path


def source_directory(value: str, mount_value: str) -> Path:
    path, mount = absolute(value), absolute(mount_value)
    if not mount.is_mount() or not path.is_dir() or path.resolve() != path:
        raise ValueError("Source is missing, redirected, or its disk is not mounted")
    if mount not in path.parents or path.stat().st_dev != mount.stat().st_dev:
        raise ValueError("Source is not on the configured source filesystem")
    return path


def latest_artifacts(root: Path, names: list[str], max_age: float) -> tuple[list[Path], float]:
    """Resolve completed artifacts, ignoring sidecar absolute paths from the producer."""
    paths, times = [], []
    for name in [*names, "orthanc_storage"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Invalid database directory name")
        folder = root / name
        suffix = "tar.gz" if name == "orthanc_storage" else "dump"
        artifact = (folder / f"latest.{suffix}").resolve(strict=True)
        if artifact.parent != folder or not artifact.name.endswith(f".{suffix}"):
            raise ValueError(f"Latest artifact escapes its directory: {name}")
        info = artifact.stat()
        age = time.time() - info.st_mtime
        if not artifact.is_file() or info.st_size == 0 or not 0 <= age <= max_age:
            raise ValueError(f"Missing, empty, stale, or future-dated local backup: {name}")
        checksum = Path(str(artifact) + ".sha256")
        if checksum.is_symlink():
            raise ValueError("Dated checksum must not be a symlink")
        expected = checksum.read_text().split()[0]
        with artifact.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if expected != actual:
            raise ValueError(f"Local checksum mismatch: {name}")
        paths.extend([artifact, checksum])
        times.append(info.st_mtime)
    return paths, min(times)


class Backup:
    def __init__(self, config: dict, tier: str, env_file: Path | None = None):
        self.config, self.tier = config, tier
        self.settings = cfg = config.get("remote_backup", {})
        if cfg.get("enabled") is not True:
            raise ValueError("Remote backups are disabled in config.toml")
        if "database_names" in cfg or "max_age_hours" in cfg:
            raise ValueError("Remove remote database_names/max_age_hours: use .env and [backup].max_age_hours")
        for key in ("host", "user", "backup_id"):
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", cfg[key]):
                raise ValueError(f"Invalid remote_backup.{key}")
        self.remote_root = absolute(cfg["remote_root"])
        self.remote_mount = absolute(cfg["remote_mount"])
        if self.remote_mount not in self.remote_root.parents:
            raise ValueError("Remote root must be below remote_mount")
        self.key = private_file(cfg["ssh_key_file"])
        self.password = private_file(cfg["password_file"])
        self.state_dir = absolute(cfg["state_dir"])
        # Never place the password, restic cache or status inside the input tree.
        for section, key in (("backup", "backup_root"), ("storage", "cold_archive_root")):
            source = absolute(config[section][key]).resolve()
            for path in (self.key.resolve(), self.password.resolve(), self.state_dir.resolve()):
                if path == source or source in path.parents:
                    raise ValueError("Credentials and state directory must be outside backup sources")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("State directory must be owned by the job user with mode 0700")
        self.state = self.state_dir / f"{tier}.json"
        self.repository = f"sftp:{cfg['user']}@{cfg['host']}:{self.remote_root}/{tier}"
        self.ssh = [
            "ssh", "-i", str(self.key), "-p", str(int(cfg.get("port", 22))),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6",
            f"{cfg['user']}@{cfg['host']}",
        ]
        self.max_age = float(config["backup"]["max_age_hours"]) * 3600
        # Read names without exporting or logging any of the file's secrets.
        env = dotenv_values(env_file or STACK / ".env", interpolate=False)
        self.database_names = [env.get("DB_NAME"), env.get("PG_ORTHANC_DB")]
        if (not isinstance(self.database_names, list) or len(self.database_names) != 2
                or not all(isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in self.database_names)
                or len(set(self.database_names)) != 2 or "orthanc_storage" in self.database_names):
            raise ValueError(".env must define distinct DB_NAME and PG_ORTHANC_DB backup directory names")
        self.keep_daily = int(cfg.get("keep_daily", 60))
        self.reserve = int(float(cfg.get("min_free_gib", 100)) * 2**30)
        if self.max_age <= 0 or self.keep_daily < 1 or self.reserve < 0:
            raise ValueError("Invalid freshness, retention or free-space setting")

    def remote_check(self, extra_bytes: int | None = None):
        command = shlex.join([
            self.settings.get("remote_python", "python3"), "-c", REMOTE_CHECK,
            str(self.remote_root), str(self.remote_mount),
            str(0 if extra_bytes is None else self.reserve + extra_bytes),
        ])
        subprocess.run([*self.ssh, command], check=True, timeout=45)

    def restic(self, *args: str, capture: bool = False):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        env.update(RESTIC_REPOSITORY=self.repository, RESTIC_PASSWORD_FILE=str(self.password))
        command = [
            self.settings.get("restic_bin", "restic"),
            "-o", "sftp.command=" + shlex.join([*self.ssh[:-1], "-s", self.ssh[-1], "sftp"]),
            "--cache-dir", str(self.state_dir / "cache"),
            "--limit-upload", str(int(self.settings.get("upload_kib_per_second", 0))),
            *args,
        ]
        return subprocess.run(command, env=env, check=True, text=True, capture_output=capture)

    def freshness(self):
        state = json.loads(self.state.read_text())
        if state["repository"] != self.repository or state["backup_id"] != self.settings["backup_id"]:
            raise ValueError("Success record belongs to a different destination or backup identity")
        for key in ("completed_at", "source_time"):
            if not 0 <= time.time() - state[key] <= self.max_age:
                raise ValueError(f"Stale remote backup: {self.tier} ({key})")
        snapshots = json.loads(self.restic("snapshots", "--json", state["snapshot_id"], capture=True).stdout)
        if not any(s["id"] == state["snapshot_id"] for s in snapshots):
            raise ValueError("Last successful snapshot is missing remotely")
        print(f"OK: {self.tier}, snapshot {state['snapshot_id'][:8]}")
        return state

    def backup(self, dry_run: bool = False):
        started = time.time()
        if self.tier == "tier1":
            root = source_directory(self.config["backup"]["backup_root"], self.settings["tier1_mount"])
            paths, source_time = latest_artifacts(
                root, self.database_names, self.max_age,
            )
            self.remote_check(sum(p.stat().st_size for p in paths))
        else:
            root = source_directory(self.config["storage"]["cold_archive_root"], self.settings["imaging_mount"])
            if next(root.rglob("*.tar.zst"), None) is None:
                raise ValueError("Cold archive tree contains no completed archives")
            paths, source_time = [root], started
            previous = json.loads(self.state.read_text()) if self.state.exists() else {}
            initial_bytes = 0
            if previous.get("repository") != self.repository:
                # A new destination needs room for the whole canonical tree.
                # Do not assume meaningful deduplication of already-compressed
                # archives. Ignore unfinished files and non-followed symlinks.
                for directory, _, files in os.walk(root):
                    for name in files:
                        path = Path(directory) / name
                        if not path.is_symlink() and not name.endswith((".tmp", ".partial")):
                            initial_bytes += path.stat().st_size
            self.remote_check(initial_bytes)
        args = [
            "backup", "--json", "--host", self.settings["backup_id"], "--tag", self.tier,
            "--exclude", "*.tmp", "--exclude", "*.partial", "--one-file-system",
        ]
        if dry_run:
            args.append("--dry-run")
        # Nonzero (including restic's incomplete-backup exit 3) never advances
        # success state. Keep the previous recovery point visible to monitoring.
        result = self.restic(*args, "--", *(str(p) for p in paths), capture=True)
        summary = next(
            json.loads(line) for line in reversed(result.stdout.splitlines())
            if json.loads(line).get("message_type") == "summary"
        )
        print(json.dumps(summary))
        if dry_run:
            return
        state = {
            "repository": self.repository, "backup_id": self.settings["backup_id"],
            "snapshot_id": summary["snapshot_id"], "completed_at": time.time(), "source_time": source_time,
        }
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state) + "\n")
        tmp.replace(self.state)

    def run(self, action: str, dry_run: bool = False, target: str | None = None,
            snapshot: str | None = None, include: list[str] | None = None):
        # Monitoring can inspect the previous committed record during a long
        # upload. Atomic state replacement prevents a partially written record.
        if action == "freshness":
            self.remote_check()
            self.freshness()
            return
        # One operation per tier. Independent repositories let Tier 1 continue
        # while a large initial imaging upload is running.
        with (self.state_dir / f"{self.tier}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if action == "backup":
                self.backup(dry_run)
                return
            self.remote_check()
            if action == "init":
                self.restic("init")
            elif action == "check":
                self.restic("check", "--read-data")
            elif action == "maintain":
                self.freshness()
                self.restic("check", "--read-data-subset", "1/12")
                args = [
                    "forget", "--host", self.settings["backup_id"], "--tag", self.tier,
                    "--group-by", "host,tags", "--keep-daily", str(self.keep_daily),
                ]
                self.restic(*args, *(["--dry-run"] if dry_run else ["--prune"]))
            elif action == "restore":
                if not target or not snapshot or not re.fullmatch(r"[0-9a-f]{8,64}", snapshot):
                    raise ValueError("Restore requires --snapshot ID and --target NEW_ABSOLUTE_DIRECTORY")
                dest = absolute(target)
                if dest.exists() or dest.is_symlink():
                    raise ValueError("Restore target must not already exist")
                # Optional spot restore: absolute source paths (or restic glob
                # patterns on them) recreated beneath the target.
                filters = [arg for path in include or [] for arg in ("--include", str(absolute(path)))]
                self.restic("restore", snapshot, "--target", str(dest), "--verify", *filters)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "backup", "freshness", "check", "maintain", "restore"])
    parser.add_argument("tier", choices=TIERS)
    parser.add_argument("--config", type=Path, default=STACK / "config.toml")
    parser.add_argument("--dry-run", action="store_true", help="Only valid for backup and maintain")
    parser.add_argument("--target")
    parser.add_argument("--snapshot")
    parser.add_argument("--include", action="append", metavar="SOURCE_PATH",
                        help="restore only these source paths (repeatable; restore only)")
    args = parser.parse_args()
    if args.include and args.action != "restore":
        parser.error("--include is only valid for restore")
    if args.dry_run and args.action not in ("backup", "maintain"):
        parser.error("--dry-run is only valid for backup and maintain")
    os.umask(0o077)
    try:
        with args.config.open("rb") as stream:
            config = tomllib.load(stream)
        Backup(config, args.tier, args.config.parent / ".env").run(
            args.action, args.dry_run, args.target, args.snapshot, args.include,
        )
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        print(f"Remote backup failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
