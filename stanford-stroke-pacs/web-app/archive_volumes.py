"""Named archive volumes: where the cold ``*.tar.zst`` trees live.

The archive tree may be spread over several disks. Each **volume** is a named
root (``disk1``, ``disk2``, …) declared in ``config.toml``::

    [storage]
    default_archive_volume = "disk1"

    [storage.archive_volumes.disk1]
    root = "/media/pacs-ssd-1/…/compressed"
    required = true

    [storage.archive_volumes.disk2]
    root = "/media/pacs-ssd-2/ssc-pacs/compressed"

    [storage.dataset_archive_volume]      # optional default for new data
    "GUIDE" = "disk2"

Every volume uses the same relative layout as the loose tree
(``<root>/<dataset slug>/<patient_id>/<StudyUID>/…``, see ``storage_layout``).
The stored absolute ``image_series.dicom_archive_path`` stays the source of
truth for where an archive *is*; its volume is the configured root that
contains it (``volume_for_path`` — the one answer every safety check uses).
A study's archives all live on one volume.

A volume is **online** when its root holds a sentinel file (``.ssc-volume``,
written by ``scripts/admin/manage_archive_volumes.py init|adopt``) naming it.
An unmounted disk leaves an empty mount-point directory without a sentinel,
and the wrong disk carries the wrong name — both read as offline instead of as
mass data loss. An offline volume never stops the service: the app starts,
says so (startup WARN, ``/healthz``), and refuses only the operations that
need that volume. See docs/cold_storage/archive_volumes.md.

This module is pure (no import of ``config``) except for the thin
``configured_*`` / ``resolve_volume`` helpers, which read the parsed config
lazily so ``config`` itself can use the parser.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SENTINEL_NAME = ".ssc-volume"
VOLUME_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# A restic repository name below [remote_backup].remote_root.
_REPOSITORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VOLUME_KEYS = {"root", "required", "backup_repository"}
LEGACY_VOLUME_NAME = "disk1"
DEFAULT_BACKUP_REPOSITORY = "imaging"
_STATUS_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class ArchiveVolume:
    name: str
    root: Path
    # Required volumes are hard boot dependencies (RequiresMountsFor) and
    # make /healthz degraded when offline; others only warn.
    required: bool = False
    backup_repository: str = DEFAULT_BACKUP_REPOSITORY


@dataclass(frozen=True)
class VolumeStatus:
    online: bool
    reason: str | None = None
    free_bytes: int | None = None
    total_bytes: int | None = None


class ArchiveVolumeOffline(RuntimeError):
    """An operation needs a volume whose sentinel is not readable."""

    def __init__(self, volume: ArchiveVolume, reason: str | None):
        self.volume = volume
        self.reason = reason
        super().__init__(f"archive volume {volume.name} ({volume.root}) is offline: {reason}")


class ArchiveVolumeError(ValueError):
    """A path or study that does not map cleanly onto the configured volumes."""


# --------------------------------------------------------------------------- #
# Parsing (called by config.py)
# --------------------------------------------------------------------------- #
def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def parse_archive_volumes(
    storage: dict, dicom_data_root: Path | None,
) -> tuple[dict[str, ArchiveVolume], str, dict[str, str], bool]:
    """Validate the ``[storage]`` volume keys.

    Returns ``(volumes, default_name, dataset_map, legacy)``; ``legacy`` is
    True when the volumes came from the deprecated single
    ``cold_archive_root`` key (one volume, ``disk1``, required). Raises
    ``RuntimeError`` on any inconsistency — this runs at startup.
    ``dicom_data_root`` None skips the overlap check against the loose tree
    (callers that only need the volumes, e.g. the backup runner).
    """
    raw_volumes = storage.get("archive_volumes")
    legacy_root = storage.get("cold_archive_root")
    if raw_volumes is not None and legacy_root is not None:
        raise RuntimeError(
            "config.toml [storage] sets both cold_archive_root and "
            "[storage.archive_volumes]; keep only the volumes table."
        )

    volumes: dict[str, ArchiveVolume] = {}
    legacy = raw_volumes is None
    if legacy:
        if legacy_root in (None, ""):
            raise RuntimeError("config.toml [storage] defines no archive volumes")
        volumes[LEGACY_VOLUME_NAME] = ArchiveVolume(
            LEGACY_VOLUME_NAME, _absolute(legacy_root, "cold_archive_root"), required=True,
        )
    else:
        if not isinstance(raw_volumes, dict) or not raw_volumes:
            raise RuntimeError("config.toml [storage.archive_volumes] must define at least one volume")
        for name, spec in raw_volumes.items():
            where = f"[storage.archive_volumes.{name}]"
            if not VOLUME_NAME_RE.match(name):
                raise RuntimeError(f"config.toml {where}: invalid volume name (use e.g. disk2)")
            if not isinstance(spec, dict):
                raise RuntimeError(f"config.toml {where} must be a table")
            unknown = set(spec) - _VOLUME_KEYS
            if unknown:
                raise RuntimeError(f"config.toml {where}: unknown key(s) {sorted(unknown)}")
            if "root" not in spec:
                raise RuntimeError(f"config.toml {where}: missing root")
            required = spec.get("required", False)
            if not isinstance(required, bool):
                raise RuntimeError(f"config.toml {where}: required must be true or false")
            repo = spec.get("backup_repository", DEFAULT_BACKUP_REPOSITORY)
            if not isinstance(repo, str) or not _REPOSITORY_RE.match(repo):
                raise RuntimeError(f"config.toml {where}: invalid backup_repository {repo!r}")
            volumes[name] = ArchiveVolume(
                name, _absolute(spec["root"], f"{where} root"), required, repo,
            )

    names = list(volumes)
    for i, a in enumerate(names):
        root = volumes[a].root
        if dicom_data_root is not None and _overlaps(root, dicom_data_root):
            raise RuntimeError(
                f"config.toml archive volume {a} ({root}) overlaps dicom_data_root ({dicom_data_root})"
            )
        for b in names[i + 1:]:
            if _overlaps(root, volumes[b].root):
                raise RuntimeError(
                    f"config.toml archive volumes {a} ({root}) and {b} ({volumes[b].root}) overlap"
                )

    default = storage.get("default_archive_volume")
    if default is None:
        if len(volumes) > 1:
            raise RuntimeError(
                "config.toml [storage] default_archive_volume is required when "
                "more than one archive volume is configured"
            )
        default = names[0]
    if default not in volumes:
        raise RuntimeError(
            f"config.toml [storage] default_archive_volume = {default!r} is not a configured volume {names}"
        )

    dataset_map = storage.get("dataset_archive_volume") or {}
    if not isinstance(dataset_map, dict):
        raise RuntimeError("config.toml [storage.dataset_archive_volume] must be a table")
    for dataset, vol in dataset_map.items():
        if vol not in volumes:
            raise RuntimeError(
                f"config.toml [storage.dataset_archive_volume] {dataset!r} = {vol!r} "
                f"is not a configured volume {names}"
            )
    return volumes, default, dict(dataset_map), legacy


def _absolute(raw, what: str) -> Path:
    path = Path(str(raw))
    if not path.is_absolute():
        raise RuntimeError(f"config.toml {what} must be an absolute path (got {raw!r})")
    return path.resolve()


# --------------------------------------------------------------------------- #
# Path → volume
# --------------------------------------------------------------------------- #
def volume_for_path(path, volumes: dict[str, ArchiveVolume] | None = None) -> ArchiveVolume | None:
    """The volume whose root strictly contains ``path`` (symlinks resolved).

    A root itself is not "inside" its volume, and a symlink pointing out of a
    root resolves outside it — both return None.
    """
    if volumes is None:
        volumes = configured_volumes()
    p = Path(path).resolve()
    for vol in volumes.values():
        if p != vol.root and p.is_relative_to(vol.root):
            return vol
    return None


def volume_of_archives(paths, volumes: dict[str, ArchiveVolume] | None = None) -> ArchiveVolume | None:
    """The single volume a study's archive paths live on (None if no paths).

    Raises ``ArchiveVolumeError`` when a path is outside every volume or the
    paths span volumes — both break the one-study-one-volume invariant.
    """
    found: dict[str, ArchiveVolume] = {}
    for raw in paths:
        if not raw:
            continue
        vol = volume_for_path(raw, volumes)
        if vol is None:
            raise ArchiveVolumeError(f"archive path is outside every archive volume: {raw}")
        found[vol.name] = vol
    if len(found) > 1:
        raise ArchiveVolumeError(f"study archives span volumes {sorted(found)}")
    return next(iter(found.values()), None)


def study_volume(cur, study_uid: str,
                 volumes: dict[str, ArchiveVolume] | None = None) -> ArchiveVolume | None:
    """The volume holding a study's archived series (None if none archived)."""
    cur.execute(
        "SELECT dicom_archive_path FROM image_series "
        "WHERE studyinstanceuid = %s AND coalesce(dicom_archive_path, '') <> ''",
        (study_uid,),
    )
    rows = cur.fetchall()
    return volume_of_archives(
        [r["dicom_archive_path"] if isinstance(r, dict) else r[0] for r in rows], volumes,
    )


# --------------------------------------------------------------------------- #
# Sentinel + status
# --------------------------------------------------------------------------- #
def sentinel_path(vol: ArchiveVolume) -> Path:
    return vol.root / SENTINEL_NAME


def read_sentinel(root: Path) -> dict | None:
    try:
        return json.loads((Path(root) / SENTINEL_NAME).read_text())
    except (OSError, ValueError):
        return None


def write_sentinel(root: Path, name: str) -> Path:
    """Atomically write the sentinel naming ``root`` as volume ``name``."""
    path = Path(root) / SENTINEL_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({
        "name": name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
    }, indent=2) + "\n")
    os.replace(tmp, path)
    return path


_status_cache: dict[tuple[str, Path], tuple[float, VolumeStatus]] = {}


def status(vol: ArchiveVolume, max_age: float = _STATUS_TTL_SECONDS) -> VolumeStatus:
    """Online/offline + free space, cached for ``max_age`` seconds."""
    key = (vol.name, vol.root)
    now = time.monotonic()
    hit = _status_cache.get(key)
    if hit and now - hit[0] < max_age:
        return hit[1]
    result = _probe(vol)
    _status_cache[key] = (now, result)
    return result


def invalidate_status_cache() -> None:
    _status_cache.clear()


def _probe(vol: ArchiveVolume) -> VolumeStatus:
    if not vol.root.is_dir():
        return VolumeStatus(False, f"root missing: {vol.root}")
    sentinel = sentinel_path(vol)
    if not sentinel.is_file():
        return VolumeStatus(
            False, f"sentinel {SENTINEL_NAME} missing (disk not mounted, or volume never "
                   "initialised: scripts/admin/manage_archive_volumes.py init|adopt)",
        )
    data = read_sentinel(vol.root)
    if not isinstance(data, dict):
        return VolumeStatus(False, f"sentinel {sentinel} unreadable")
    if data.get("name") != vol.name:
        return VolumeStatus(False, f"sentinel names {data.get('name')!r}, expected {vol.name!r} (wrong disk mounted?)")
    try:
        usage = shutil.disk_usage(vol.root)
    except OSError as exc:
        return VolumeStatus(False, f"disk usage failed: {exc}")
    return VolumeStatus(True, None, usage.free, usage.total)


def require_online(vol: ArchiveVolume) -> None:
    st = status(vol, max_age=0)
    if not st.online:
        raise ArchiveVolumeOffline(vol, st.reason)


# --------------------------------------------------------------------------- #
# Configured set (lazy: config imports this module)
# --------------------------------------------------------------------------- #
def configured_volumes() -> dict[str, ArchiveVolume]:
    import config
    return config.ARCHIVE_VOLUMES


def default_volume() -> ArchiveVolume:
    import config
    return config.ARCHIVE_VOLUMES[config.DEFAULT_ARCHIVE_VOLUME]


def get_volume(name: str) -> ArchiveVolume:
    vols = configured_volumes()
    if name not in vols:
        raise ArchiveVolumeError(f"unknown archive volume {name!r}; configured: {sorted(vols)}")
    return vols[name]


def resolve_volume(dataset: str | None, override: str | None = None,
                   dataset_slug: str | None = None) -> tuple[ArchiveVolume, str]:
    """Where *new* studies of a batch go, and why.

    Order: explicit ``override`` (the ingestion YAML's ``archive_volume``),
    then ``[storage.dataset_archive_volume]`` (by dataset name or slug), then
    ``default_archive_volume``. Existing studies ignore this and stay on their
    current volume.
    """
    import config
    if override:
        return get_volume(override), "yaml archive_volume"
    for key in (dataset, dataset_slug):
        if key and key in config.DATASET_ARCHIVE_VOLUME:
            return get_volume(config.DATASET_ARCHIVE_VOLUME[key]), (
                f"config.toml [storage.dataset_archive_volume] {key!r}"
            )
    return default_volume(), "config.toml [storage] default_archive_volume"


def offline_volumes() -> list[tuple[ArchiveVolume, VolumeStatus]]:
    return [(v, st) for v in configured_volumes().values() if not (st := status(v)).online]
