#!/usr/bin/env python3
"""Rewrite path prefixes in Orthanc's Folder Indexer DB (``indexer-plugin.db``).

Helper of move_to_dataset_layout.py, run inside a throwaway container with the
Orthanc storage volume mounted (the volume's files are root-owned), **Orthanc
stopped**. Pure stdlib, so any stock python image works.

The indexer resolves an attachment uuid -> ``Attachments.instanceId`` (a hash
of DICOM UIDs) -> ``Files.path``. Only ``Files.path`` holds a filesystem path,
so moving a study directory needs nothing but its ``Files`` rows' prefix
rewritten: ``/dicom-data/<old study dir>/…`` -> ``/dicom-data/<new study dir>/…``.

Each prefix is rewritten with one PK range update (the range the plugin's own
``RemoveFilesUnderPrefix`` uses), all in one transaction, checked before commit:
the row count is unchanged, every moved row landed under its new prefix, no
attachment lost its file row, and ``integrity_check`` is ok.

    python indexer_paths.py check   <db> <map.json> <stats.json>   # read-only (works on a copy)
    python indexer_paths.py apply   <db> <map.json> <stats.json>   # backs up <db> first

``map.json``: ``{"pairs": [[old_prefix, new_prefix], ...]}``, prefixes ending in ``/``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time


def _range(prefix: str) -> tuple[str, str]:
    # Every path starting with "<dir>/": [<dir>/, <dir>0) — '0' sorts right after '/'.
    assert prefix.endswith("/"), prefix
    return prefix, prefix[:-1] + "0"


def _count(con, prefix: str) -> int:
    lo, hi = _range(prefix)
    return con.execute("SELECT count(*) FROM Files WHERE path >= ? AND path < ?",
                       (lo, hi)).fetchone()[0]


def _orphan_attachments(con) -> int:
    return con.execute(
        "SELECT count(*) FROM Attachments a WHERE NOT EXISTS "
        "(SELECT 1 FROM Files f WHERE f.instanceId = a.instanceId)"
    ).fetchone()[0]


def _validate_pairs(pairs) -> list[tuple[str, str]]:
    out = []
    olds = set()
    for old, new in pairs:
        if not (old.endswith("/") and new.endswith("/")) or old == new:
            raise ValueError(f"bad prefix pair: {old!r} -> {new!r}")
        olds.add(old)
        out.append((old, new))
    for _old, new in out:
        # A new prefix inside some old range would be rewritten twice.
        if any(new.startswith(o) for o in olds):
            raise ValueError(f"new prefix {new!r} lies under a prefix being moved")
    return out


def rewrite(db_path: str, pairs, *, apply: bool) -> dict:
    """Rewrite (or, without ``apply``, just count) the given prefixes. Returns stats."""
    pairs = _validate_pairs(pairs)
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check before: {integrity}")
        total_before = con.execute("SELECT count(*) FROM Files").fetchone()[0]
        orphans_before = _orphan_attachments(con)
        per_pair = [_count(con, old) for old, _new in pairs]
        occupied = [new for (_old, new) in pairs if _count(con, new)]
        stats = {
            "files_rows": total_before,
            "rows_to_move": sum(per_pair),
            "prefixes": len(pairs),
            "prefixes_without_rows": sum(1 for n in per_pair if n == 0),
            "target_prefixes_occupied": occupied[:20],
            "orphan_attachments": orphans_before,
            "applied": False,
        }
        if occupied:
            raise RuntimeError(f"{len(occupied)} target prefix(es) already hold rows, "
                               f"e.g. {occupied[0]}")
        if not apply:
            return stats

        con.execute("BEGIN IMMEDIATE")
        try:
            for (old, new), expected in zip(pairs, per_pair, strict=True):
                if not expected:
                    continue
                lo, hi = _range(old)
                con.execute(
                    "UPDATE Files SET path = ? || substr(path, ?) WHERE path >= ? AND path < ?",
                    (new, len(old) + 1, lo, hi),
                )
                if _count(con, new) != expected or _count(con, old):
                    raise RuntimeError(f"rewrite of {old} did not move all {expected} row(s)")
            total_after = con.execute("SELECT count(*) FROM Files").fetchone()[0]
            if total_after != total_before:
                raise RuntimeError(f"Files rows {total_before} -> {total_after}")
            if _orphan_attachments(con) != orphans_before:
                raise RuntimeError("an attachment lost its Files row")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check after: {integrity}")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stats["applied"] = True
        return stats
    finally:
        con.close()


def main(argv) -> int:
    if len(argv) != 5 or argv[1] not in ("check", "apply"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, db_path, map_path, stats_path = argv[1:]
    with open(map_path) as fh:
        pairs = json.load(fh)["pairs"]
    try:
        if mode == "apply":
            backup = f"{db_path}.pre-layout-{time.strftime('%Y%m%dT%H%M%S')}"
            # Fold the WAL in first so the single-file copy is complete.
            con = sqlite3.connect(db_path)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            shutil.copy2(db_path, backup)
        else:
            # Read-only mount: count on a private copy of the trio.
            work = "/tmp/indexer-check"
            os.makedirs(work, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(db_path + suffix):
                    shutil.copy(db_path + suffix, os.path.join(work, "i.db" + suffix))
            db_path, backup = os.path.join(work, "i.db"), None
        stats = rewrite(db_path, pairs, apply=(mode == "apply"))
        stats["backup"] = backup
        rc = 0
    except Exception as exc:  # reported to the caller through the stats file
        stats, rc = {"error": f"{type(exc).__name__}: {exc}", "applied": False}, 1
    with open(stats_path, "w") as fh:
        json.dump(stats, fh, indent=2)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
