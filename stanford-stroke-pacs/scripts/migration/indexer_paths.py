#!/usr/bin/env python3
"""Rewrite path prefixes in Orthanc's Folder Indexer DB (``indexer-plugin.db``).

Helper of move_to_dataset_layout.py, run inside a throwaway container with the
Orthanc storage volume mounted (the volume's files are root-owned), **Orthanc
stopped**. Pure stdlib, so any stock python image works.

The indexer resolves an attachment uuid -> ``Attachments.instanceId`` (a hash
of DICOM UIDs) -> ``Files.path``. Only ``Files.path`` holds a filesystem path,
so moving a study directory needs nothing but its ``Files`` rows' prefix
rewritten: ``/dicom-data/<old study dir>/…`` -> ``/dicom-data/<new study dir>/…``.

The production DB holds tens of millions of ``Files`` rows, so the rows are
not updated in place (re-keying a primary key row by row in one transaction
spills and re-reads the whole log). Instead a **new database file** is built
next to the original: same schema, every table copied, ``Files`` inserted in
the order of its rewritten primary key, indexes created afterwards. It is
checked (row counts per table unchanged, every moved row under its new prefix,
no attachment lost its file row, ``quick_check`` ok), then swapped in; the
original file is kept as ``<db>.pre-layout-<ts>`` — the backup.

    python indexer_paths.py check <db> <map.json> <stats.json>   # read-only, approximate if live
    python indexer_paths.py apply <db> <map.json> <stats.json>

``map.json``: ``{"pairs": [[old_prefix, new_prefix], ...]}``, prefixes ending in ``/``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time


def _validate_pairs(pairs) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for old, new in pairs:
        if not (old.endswith("/") and new.endswith("/")) or old == new:
            raise ValueError(f"bad prefix pair: {old!r} -> {new!r}")
        mapping[old] = new
    for new in mapping.values():
        # A new prefix inside some old one would be rewritten twice.
        if any(new.startswith(o) for o in mapping):
            raise ValueError(f"new prefix {new!r} lies under a prefix being moved")
    return mapping


class _Remapper:
    """Path -> rewritten path, by its leading directory components."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.news = set(mapping.values())
        self.depths = sorted({p.count("/") for p in mapping} | {p.count("/") for p in self.news})
        self.hits: dict[str, int] = {}
        self.occupied: dict[str, int] = {}

    def _prefixes(self, path: str):
        for depth in self.depths:
            cut = -1
            for _ in range(depth):
                cut = path.find("/", cut + 1)
                if cut < 0:
                    return
            yield path[: cut + 1]

    def __call__(self, path: str) -> str:
        for prefix in self._prefixes(path):
            new = self.mapping.get(prefix)
            if new is not None:
                self.hits[prefix] = self.hits.get(prefix, 0) + 1
                return new + path[len(prefix):]
            if prefix in self.news:
                self.occupied[prefix] = self.occupied.get(prefix, 0) + 1
        return path


def _orphan_attachments(con, schema: str = "main") -> int:
    return con.execute(
        f"SELECT count(*) FROM {schema}.Attachments a WHERE NOT EXISTS "
        f"(SELECT 1 FROM {schema}.Files f WHERE f.instanceId = a.instanceId)"
    ).fetchone()[0]


def _quick_check(con, schema: str = "main") -> str:
    return con.execute(f"PRAGMA {schema}.quick_check").fetchone()[0]


def scan(db_path: str, pairs, *, immutable: bool = False) -> dict:
    """Count what a rewrite would move (read-only).

    ``immutable`` reads a live DB on a read-only mount without its WAL: the
    counts are then approximate (a dry run while Orthanc is up).
    """
    remap = _Remapper(_validate_pairs(pairs))
    flags = "mode=ro&immutable=1" if immutable else "mode=ro"
    con = sqlite3.connect(f"file:{db_path}?{flags}", uri=True)
    try:
        con.create_function("remap", 1, remap, deterministic=True)
        total = con.execute("SELECT count(*), sum(remap(path) <> path) FROM Files").fetchone()
        return {
            "files_rows": total[0],
            "rows_to_move": total[1] or 0,
            "prefixes": len(remap.mapping),
            "prefixes_without_rows": len(remap.mapping) - len(remap.hits),
            "target_prefixes_occupied": sorted(remap.occupied)[:20],
            "orphan_attachments": _orphan_attachments(con),
            "applied": False,
        }
    finally:
        con.close()


def build_rewritten(db_path: str, new_path: str, pairs) -> dict:
    """Write ``new_path``: a copy of ``db_path`` with the Files prefixes rewritten."""
    stats = scan(db_path, pairs)
    if stats["target_prefixes_occupied"]:
        raise RuntimeError(f"target prefix(es) already hold rows, e.g. "
                           f"{stats['target_prefixes_occupied'][0]}")
    remap = _Remapper(_validate_pairs(pairs))
    if os.path.exists(new_path):
        os.remove(new_path)
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        con.create_function("remap", 1, remap, deterministic=True)
        con.execute("ATTACH DATABASE ? AS new", (new_path,))
        con.execute("PRAGMA new.journal_mode = OFF")  # a scratch file until verified
        con.execute("PRAGMA new.synchronous = OFF")
        user_version = con.execute("PRAGMA main.user_version").fetchone()[0]
        objects = con.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        counts = {}
        con.execute("BEGIN")
        for kind, name, _tbl, ddl in objects:
            if kind == "table":
                con.execute(_qualify(ddl, name))
        for kind, name, _tbl, _ddl in objects:
            if kind != "table":
                continue
            if name == "Files":
                cols = [r[1] for r in con.execute("PRAGMA main.table_info(Files)")]
                sel = ", ".join("remap(path)" if c == "path" else c for c in cols)
                con.execute(f"INSERT INTO new.Files ({', '.join(cols)}) "
                            f"SELECT {sel} FROM main.Files ORDER BY remap(path)")
            else:
                con.execute(f'INSERT INTO new."{name}" SELECT * FROM main."{name}"')
            counts[name] = con.execute(f'SELECT count(*) FROM main."{name}"').fetchone()[0]
            if con.execute(f'SELECT count(*) FROM new."{name}"').fetchone()[0] != counts[name]:
                raise RuntimeError(f"row count of {name} changed")
        for kind, name, _tbl, ddl in objects:
            if kind in ("index", "trigger", "view"):
                con.execute(_qualify(ddl, name))
        con.execute(f"PRAGMA new.user_version = {int(user_version)}")
        con.execute("COMMIT")

        moved = sum(remap.hits.values())
        if moved != stats["rows_to_move"]:
            raise RuntimeError(f"moved {moved} rows, expected {stats['rows_to_move']}")
        for old, new in remap.mapping.items():
            hi_old, hi_new = old[:-1] + "0", new[:-1] + "0"
            if con.execute("SELECT count(*) FROM new.Files WHERE path >= ? AND path < ?",
                           (old, hi_old)).fetchone()[0]:
                raise RuntimeError(f"rows left under {old}")
            if con.execute("SELECT count(*) FROM new.Files WHERE path >= ? AND path < ?",
                           (new, hi_new)).fetchone()[0] != remap.hits.get(old, 0):
                raise RuntimeError(f"rows missing under {new}")
        if _orphan_attachments(con, "new") != stats["orphan_attachments"]:
            raise RuntimeError("an attachment lost its Files row")
        check = _quick_check(con, "new")
        if check != "ok":
            raise RuntimeError(f"quick_check of the new DB: {check}")
        con.execute("DETACH DATABASE new")
    finally:
        con.close()
    stats.update(applied=True, rows_moved=moved, tables=counts)
    return stats


def _qualify(ddl: str, name: str) -> str:
    """Point a CREATE statement at the attached ``new`` schema."""
    for kw in ("CREATE TABLE ", "CREATE UNIQUE INDEX ", "CREATE INDEX ", "CREATE TRIGGER ",
               "CREATE VIEW "):
        if ddl.upper().startswith(kw):
            head, rest = ddl[: len(kw)], ddl[len(kw):].lstrip()
            for prefix in ("IF NOT EXISTS ",):
                if rest.upper().startswith(prefix):
                    head, rest = head + rest[: len(prefix)], rest[len(prefix):]
            return f"{head}new.{rest}"
    raise ValueError(f"unexpected schema object {name!r}: {ddl[:60]}")


def apply_rewrite(db_path: str, pairs) -> dict:
    """Rewrite in place: build the new file, then swap it in; keep the original."""
    # The ordered copy sorts tens of millions of keys: spill next to the DB
    # (the volume), not into the container's small root filesystem.
    os.environ.setdefault("SQLITE_TMPDIR", os.path.dirname(os.path.abspath(db_path)))
    # Fold any WAL into the main file so the original is complete on its own.
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    con.close()
    for sidecar in ("-wal", "-shm"):
        if os.path.exists(db_path + sidecar) and os.path.getsize(db_path + sidecar):
            if sidecar == "-wal":
                raise RuntimeError("indexer-plugin.db-wal is not empty: is Orthanc stopped?")
    new_path = db_path + ".layout-new"
    stats = build_rewritten(db_path, new_path, pairs)
    st = os.stat(db_path)
    os.chmod(new_path, st.st_mode & 0o7777)
    try:
        os.chown(new_path, st.st_uid, st.st_gid)
    except PermissionError:
        pass  # tests run unprivileged on their own files
    con = sqlite3.connect(new_path)
    con.execute(f"PRAGMA journal_mode = {journal_mode}")
    con.close()
    backup = f"{db_path}.pre-layout-{time.strftime('%Y%m%dT%H%M%S')}"
    for sidecar in ("-wal", "-shm"):
        if os.path.exists(db_path + sidecar):
            os.remove(db_path + sidecar)
    os.replace(db_path, backup)
    os.replace(new_path, db_path)
    stats["backup"] = backup
    return stats


def rewrite(db_path: str, pairs, *, apply: bool) -> dict:
    return apply_rewrite(db_path, pairs) if apply else scan(db_path, pairs)


def main(argv) -> int:
    if len(argv) != 5 or argv[1] not in ("check", "apply"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, db_path, map_path, stats_path = argv[1:]
    with open(map_path) as fh:
        pairs = json.load(fh)["pairs"]
    try:
        if mode == "apply":
            stats = apply_rewrite(db_path, pairs)
        else:
            stats = scan(db_path, pairs, immutable=True)
        rc = 0
    except Exception as exc:  # reported to the caller through the stats file
        stats, rc = {"error": f"{type(exc).__name__}: {exc}", "applied": False}, 1
    with open(stats_path, "w") as fh:
        json.dump(stats, fh, indent=2)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
