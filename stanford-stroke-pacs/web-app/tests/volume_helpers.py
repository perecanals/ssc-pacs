"""Scratch archive volumes for tests: patch config.toml's volume set."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import archive_volumes
import config
from archive_volumes import ArchiveVolume


def volume_set(*roots, sentinel=True) -> dict[str, ArchiveVolume]:
    """``{"disk1": <roots[0]>, "disk2": <roots[1]>, ...}`` with sentinels."""
    vols = {}
    for i, root in enumerate(roots, start=1):
        name = f"disk{i}"
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if sentinel:
            archive_volumes.write_sentinel(root, name)
        vols[name] = ArchiveVolume(name, root)
    return vols


@contextmanager
def archive_volumes_at(*roots, sentinel=True):
    """Make ``roots`` the configured volumes (first = default) for the block."""
    vols = volume_set(*roots, sentinel=sentinel)
    archive_volumes.invalidate_status_cache()
    with patch.object(config, "ARCHIVE_VOLUMES", vols), \
         patch.object(config, "DEFAULT_ARCHIVE_VOLUME", "disk1"):
        yield vols
    archive_volumes.invalidate_status_cache()
