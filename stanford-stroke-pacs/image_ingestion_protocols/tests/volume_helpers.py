"""Scratch archive volumes for tests (roots + sentinels, no config.toml)."""

from pathlib import Path


def make_volumes(base, *names, sentinel=True):
    from archive_volumes import ArchiveVolume, invalidate_status_cache, write_sentinel

    volumes = {}
    for name in names:
        root = (Path(base) / name).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if sentinel:
            write_sentinel(root, name)
        volumes[name] = ArchiveVolume(name, root)
    invalidate_status_cache()
    return volumes
