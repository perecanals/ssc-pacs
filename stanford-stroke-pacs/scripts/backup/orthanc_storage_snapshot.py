#!/usr/bin/env python3
"""In-container helper for the Orthanc storage-volume backup.

Runs inside a throwaway container (e.g. python:3.12-slim) with the Orthanc
storage volume mounted READ-ONLY at /vol. Emits a single gzip-compressed tar
to **stdout** containing a consistent snapshot of the whole volume:

  - every file under /vol EXCEPT the live SQLite trio
    (indexer-plugin.db, -wal, -shm) — i.e. the immutable OHIF SR DICOMs and
    Orthanc attachment files, copied straight from the read-only mount; plus
  - a consistent copy of indexer-plugin.db produced by copying the trio into
    the container's ephemeral /work, letting SQLite WAL-recover it in
    isolation, then checkpoint+TRUNCATE so it is self-contained.

The production volume is mounted :ro and is never written. Pure stdlib (sqlite3
+ tarfile + gzip) — no third-party imports, no external binaries — so any
stock python image works. Diagnostics (integrity_check result, counts) go to
stderr so stdout stays a clean tar stream.

Exit codes: 0 ok; 5 the DB snapshot failed (SR files are still streamed).
"""
import os
import shutil
import sqlite3
import sys
import tarfile

VOL = "/vol"
WORK = "/work"
DB = "indexer-plugin.db"
TRIO = (DB, DB + "-wal", DB + "-shm")


def log(msg):
    sys.stderr.write(f"[snapshot] {msg}\n")
    sys.stderr.flush()


def stage_consistent_db():
    """Copy the live SQLite trio into /work and fold the WAL into the main file,
    yielding a self-contained, integrity-checked indexer-plugin.db. Returns the
    path on success, or None if the DB could not be snapshotted consistently."""
    os.makedirs(WORK, exist_ok=True)
    src_db = os.path.join(VOL, DB)
    if not os.path.exists(src_db):
        log(f"WARNING: {src_db} not found — skipping DB snapshot")
        return None
    for name in TRIO:
        src = os.path.join(VOL, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(WORK, name))
    work_db = os.path.join(WORK, DB)
    try:
        con = sqlite3.connect(work_db)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        log(f"ERROR: snapshot DB unreadable ({exc}); DB will be omitted")
        return None
    log(f"indexer-plugin.db integrity_check={integrity}")
    if integrity != "ok":
        log("WARNING: integrity_check != ok — likely a torn copy of the live "
            "DB; the index is rebuildable and the SR files are unaffected")
    # Drop any leftover sidecars so only the self-contained .db is tarred.
    for sidecar in (work_db + "-wal", work_db + "-shm"):
        if os.path.exists(sidecar):
            os.remove(sidecar)
    return work_db


def main():
    work_db = stage_consistent_db()

    n_files = 0
    total = 0
    # mode 'w|gz' → streaming, no seeking, safe for a pipe to stdout.
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz", compresslevel=6) as tar:
        for root, _, files in os.walk(VOL):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, VOL)
                if rel in TRIO:
                    continue  # live DB trio replaced by the consistent snapshot
                try:
                    tar.add(full, arcname=rel, recursive=False)
                    n_files += 1
                    total += os.path.getsize(full)
                except OSError as exc:
                    log(f"WARNING: skipped {rel} ({exc})")
        if work_db is not None:
            tar.add(work_db, arcname=DB, recursive=False)
            n_files += 1
            total += os.path.getsize(work_db)

    log(f"streamed {n_files} files, ~{total / 1048576:.1f} MB uncompressed")
    sys.exit(0 if work_db is not None else 5)


if __name__ == "__main__":
    main()
