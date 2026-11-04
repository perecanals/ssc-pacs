#!/usr/bin/env python3
"""Set up and inspect archive volumes (where the cold *.tar.zst trees live).

Archive volumes are named roots (disk1, disk2, ...) declared in config.toml
[storage.archive_volumes.<name>]; every volume uses the same relative layout
(<root>/<dataset slug>/<patient_id>/<StudyUID>/...). A volume is online while
its root holds the sentinel file .ssc-volume naming it — this script writes
that sentinel. See docs/cold_storage/archive_volumes.md.

Usage:
    python scripts/admin/manage_archive_volumes.py status
    python scripts/admin/manage_archive_volumes.py init disk2 /media/pacs-ssd-2/ssc-pacs/compressed            # dry-run
    sudo "$(which python)" scripts/admin/manage_archive_volumes.py init disk2 /media/pacs-ssd-2/ssc-pacs/compressed --execute
    sudo "$(which python)" scripts/admin/manage_archive_volumes.py adopt disk1 /media/pacs-ssd-1/ssc-pacs/ssc-pacs-data/compressed --execute
    python scripts/admin/manage_archive_volumes.py check-dataset GUIDE [--archive-volume disk2]

init   — a NEW, empty root: validates it (absolute, outside every other storage
         root, not on /), creates it owned by the service user, writes the
         sentinel, and prints the config.toml snippet plus the remaining steps.
         Run it before the first ingest.
adopt  — an EXISTING, populated root (e.g. disk1 when upgrading from the single
         cold_archive_root): writes the sentinel only.
status — every configured volume: online/offline (and why), free space, and the
         archived series per dataset it holds (from image_series paths).
check-dataset — which volume new studies of a dataset would be filed on, and why.

init/adopt never touch the database or config.toml. Both are dry-run unless
--execute, and --execute must run as root (sudo): adding a disk is an admin
act. Use the project interpreter (sudo "$(which python)" ...) — root's python3
may lack the dependencies. The volume root is owned by the invoking user
(SUDO_USER, or --owner); the sentinel stays root-owned, read-only to the app.
Mounting is not done here: the script prints those commands for you to run.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "web-app"))

import archive_volumes as av  # noqa: E402

import config  # noqa: E402


def _gib(n: int | None) -> str:
    return "?" if n is None else f"{n / 1024**3:,.0f} GiB"


def _existing(path: Path) -> Path:
    """``path`` or its nearest existing ancestor."""
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _mount_point(path: Path) -> Path:
    p = _existing(path)
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def _other_roots(name: str) -> list[tuple[str, Path]]:
    roots = [("dicom_data_root", config.DICOM_DATA_ROOT)]
    if config.LINKED_VIEW_ROOT:
        roots.append(("linked_view_root", config.LINKED_VIEW_ROOT))
    roots += [(f"archive volume {n}", v.root) for n, v in config.ARCHIVE_VOLUMES.items() if n != name]
    return roots


def _validate_target(name: str, root: Path) -> list[str]:
    problems = []
    if not av.VOLUME_NAME_RE.match(name):
        problems.append(f"invalid volume name {name!r} (use e.g. disk2)")
    if not root.is_absolute():
        problems.append(f"root must be an absolute path: {root}")
        return problems
    for label, other in _other_roots(name):
        if root == other or root.is_relative_to(other) or other.is_relative_to(root):
            problems.append(f"{root} overlaps {label} ({other})")
    configured = config.ARCHIVE_VOLUMES.get(name)
    if configured is not None and configured.root != root:
        problems.append(f"config.toml already has {name} at {configured.root}, not {root}")
    sentinel = av.read_sentinel(root) if root.is_dir() else None
    if isinstance(sentinel, dict) and sentinel.get("name") != name:
        problems.append(f"{root} already carries the sentinel of volume {sentinel.get('name')!r}")
    return problems


def _writable_by(root: Path, owner: pwd.struct_passwd) -> str | None:
    """None if ``owner`` can create files in ``root`` (checked as that user)."""
    pid = os.fork()
    if pid == 0:  # child: drop to the owner and try
        try:
            os.setgid(owner.pw_gid)
            os.setuid(owner.pw_uid)
            with tempfile.NamedTemporaryFile(dir=root, prefix=".write-test-"):
                pass
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, code = os.waitpid(pid, 0)
    return None if code == 0 else f"{owner.pw_name} cannot write to {root}"


def _volume_owner(name: str | None) -> pwd.struct_passwd:
    name = name or os.environ.get("SUDO_USER")
    if not name or name == "root":
        raise SystemExit("Error: run through sudo from the service account, or pass "
                         "--owner <service user> (the volume root must not be root-owned)")
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise SystemExit(f"Error: unknown user {name!r}") from None


def _print_mount_help(root: Path) -> None:
    user = os.environ.get("USER", "sscpacs")
    print(f"""
  Optional — make {root} a dedicated mount (a whole disk, or a bind of a
  directory on one) instead of a plain directory. Run as root, BEFORE init:
      sudo mkdir -p {root}
      sudo chattr +i {root}          # the bare mount point: writes fail while the disk is unmounted
      # /etc/fstab — 'nofail' so a missing disk never blocks boot (the app reports it offline):
      #   <device or UUID=...>  {root}  ext4  defaults,nofail  0 2
      #   <directory on disk>   {root}  none  bind,nofail,x-systemd.requires-mounts-for=<that disk>  0 0
      sudo mount {root}
      sudo chown {user}:{user} {root}
  then re-run this init.""")


def _init(name: str, root: Path, execute: bool, adopt: bool, owner_name: str | None = None) -> int:
    root = Path(root)
    verb = "adopt" if adopt else "init"
    if execute and os.geteuid() != 0:
        print(f"Error: {verb} --execute requires root — adding an archive disk is an admin act.\n"
              f"    sudo {sys.executable} {Path(__file__).resolve()} {verb} {name} {root} --execute",
              file=sys.stderr)
        return 1
    owner = _volume_owner(owner_name) if execute else None
    problems = _validate_target(name, root)
    if problems:
        for p in problems:
            print(f"Error: {p}", file=sys.stderr)
        return 1
    root = root.resolve()
    existing = av.read_sentinel(root) if root.is_dir() else None

    if adopt:
        if not root.is_dir():
            print(f"Error: adopt needs an existing root; {root} does not exist "
                  "(use init for a new volume)", file=sys.stderr)
            return 1
    else:
        if _mount_point(root) == Path("/"):
            print(f"Error: {root} would be on the root filesystem — is the intended disk mounted?",
                  file=sys.stderr)
            return 1
        if root.is_dir():
            entries = [e.name for e in root.iterdir() if e.name not in (av.SENTINEL_NAME, "lost+found")]
            if entries:
                print(f"Error: {root} is not empty ({len(entries)} entries, e.g. {entries[:3]}); "
                      "use adopt for a populated root", file=sys.stderr)
                return 1

    mount = _mount_point(root)
    data_mount = _mount_point(config.DICOM_DATA_ROOT)
    print(f"Volume {name}: {root}")
    print(f"  filesystem mounted at {mount}"
          + (" (the root itself is a mount point)" if root.exists() and os.path.ismount(root) else ""))
    if mount == data_mount and not adopt:
        print(f"  NOTE: same filesystem as dicom_data_root ({data_mount}) — no extra space gained")
    print(f"  free: {_gib(shutil.disk_usage(_existing(root)).free)}")

    if isinstance(existing, dict):
        print(f"  sentinel already present (written {existing.get('created_at')} on {existing.get('host')})")
    elif not execute:
        if not adopt and not root.exists():
            print(f"  would create {root}")
        print(f"  would write {root / av.SENTINEL_NAME}")
        if not adopt:
            _print_mount_help(root)
        print(f"\nDry-run: nothing written. Re-run with --execute to {verb} {name}.")
        return 0
    else:
        if not adopt:
            root.mkdir(parents=True, exist_ok=True)
            os.chown(root, owner.pw_uid, owner.pw_gid)
            print(f"  created {root} (owner {owner.pw_name})")
        err = _writable_by(root, owner)
        if err:
            print(f"Error: {err} — the web app and ingestion write archives there as that user",
                  file=sys.stderr)
            return 1
        sentinel = av.write_sentinel(root, name)
        os.chmod(sentinel, 0o644)  # root-owned: the service account can read, not rewrite
        print(f"  wrote {sentinel}")

    configured = name in config.ARCHIVE_VOLUMES
    print("\nNext steps:")
    if not configured:
        print(f"""  1. Add to config.toml (stack root):

     [storage.archive_volumes.{name}]
     root = "{root}"
     # required = true      # only if the stack must not boot without this disk
     # backup_repository = "imaging"   # restic repo under [remote_backup].remote_root

     # optional: file a dataset's new studies here by default
     [storage.dataset_archive_volume]
     "<DATASET NAME>" = "{name}"
""")
        if config._storage.get("_legacy_volumes"):
            print("     config.toml still uses the legacy [storage] cold_archive_root: replace it with\n"
                  f"     [storage.archive_volumes.{config.DEFAULT_ARCHIVE_VOLUME}] root = "
                  f"\"{config.ARCHIVE_VOLUMES[config.DEFAULT_ARCHIVE_VOLUME].root}\" and add\n"
                  f"     default_archive_volume = \"{config.DEFAULT_ARCHIVE_VOLUME}\" under [storage].")
        print("  2. Restart the web app:  sudo systemctl restart ssc-web-app")
    else:
        print("  1. Restart the web app so it picks up the sentinel:  sudo systemctl restart ssc-web-app")
    print("  -  Verify:  python scripts/admin/manage_archive_volumes.py status   (and /healthz; no sudo)")
    print("  -  Backups: python scripts/backup/remote_backup.py backup imaging --dry-run")
    print("  -  If you set required = true: re-run scripts/linux/install_systemd.sh")
    print(f"  -  Ingest: archive_volume: {name} in the YAML, or the dataset default above")
    return 0


def _status() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
        from db import get_conn
    except ImportError as exc:
        print(f"Error: {exc} — run inside the project environment (conda activate ssc-pacs)",
              file=sys.stderr)
        return 1

    print(f"default_archive_volume: {config.DEFAULT_ARCHIVE_VOLUME}")
    for ds, vol in sorted(config.DATASET_ARCHIVE_VOLUME.items()):
        print(f"dataset default: {ds!r} -> {vol}")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            total = 0
            exit_code = 0
            for vol in config.ARCHIVE_VOLUMES.values():
                st = av.status(vol, max_age=0)
                state = "online" if st.online else f"OFFLINE ({st.reason})"
                print(f"\n{vol.name}  {vol.root}  [{'required' if vol.required else 'optional'}]  {state}")
                if st.online:
                    print(f"  free {_gib(st.free_bytes)} of {_gib(st.total_bytes)}  "
                          f"backup repository: {vol.backup_repository}")
                else:
                    exit_code = 2
                cur.execute(
                    "SELECT split_part(patient_key, '__', 1) AS slug, count(*), "
                    "       coalesce(sum(compressed_size_mb), 0) "
                    "FROM image_series WHERE starts_with(dicom_archive_path, %s) "
                    "GROUP BY 1 ORDER BY 1",
                    (f"{vol.root}/",),
                )
                rows = cur.fetchall()
                if not rows:
                    print("  no archived series")
                for slug, n, mb in rows:
                    total += n
                    print(f"  {slug or '(no patient_key)':<20} {n:>8} series  {mb / 1024:>10,.1f} GiB")
            cur.execute("SELECT count(*) FROM image_series WHERE coalesce(dicom_archive_path, '') <> ''")
            (archived,) = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    if archived != total:
        print(f"\nWARNING: {archived - total} archived series are outside every configured "
              "volume (stale paths? run scripts/migration/reconcile_migration.py)")
        exit_code = exit_code or 1
    return exit_code


def _check_dataset(dataset: str, override: str | None) -> int:
    try:
        vol, why = av.resolve_volume(dataset, override)
    except av.ArchiveVolumeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    st = av.status(vol, max_age=0)
    print(f"New studies of {dataset!r} go to {vol.name} ({vol.root}) — from {why}")
    print(f"  {'online, ' + _gib(st.free_bytes) + ' free' if st.online else 'OFFLINE: ' + str(st.reason)}")
    print("  (studies that already exist stay on the volume they are on)")
    return 0 if st.online else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Volumes: online state, free space, series per dataset.")
    for verb, text in (("init", "Prepare a NEW, empty volume root."),
                       ("adopt", "Mark an EXISTING, populated root as a volume.")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("name", help="Volume name, e.g. disk2.")
        p.add_argument("root", type=Path, help="Absolute root directory.")
        p.add_argument("--execute", action="store_true",
                       help="Write the sentinel (as root). Without it, validate and report only.")
        p.add_argument("--owner", help="Service user that owns the volume root "
                                       "(default: SUDO_USER)")
    chk = sub.add_parser("check-dataset", help="Where new studies of a dataset would go.")
    chk.add_argument("dataset", help="Dataset name as in the ingestion YAML.")
    chk.add_argument("--archive-volume", help="Simulate the YAML archive_volume key.")
    args = parser.parse_args()

    if args.command == "status":
        return _status()
    if args.command in ("init", "adopt"):
        return _init(args.name, args.root, args.execute, adopt=args.command == "adopt",
                     owner_name=args.owner)
    return _check_dataset(args.dataset, args.archive_volume)


if __name__ == "__main__":
    raise SystemExit(main())
