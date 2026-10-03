#!/usr/bin/env python3
"""Move imaging to the dataset layout: ``<root>/<slug>/<patient_id>/<StudyUID>``.

Every study whose stored ``image_study.study_path`` is not where
``storage_layout`` puts it (the owner enrollment's dataset slug + the imaging
patient id) is moved, in both storage roots, and every stored path follows:

  1. **Files** — each study dir is renamed in ``dicom_data_root`` and in
     ``cold_archive_root`` (same filesystem: renames, no copies; mtimes kept, so
     Orthanc sees nothing new). Each rename is journaled before the next.
  2. **Orthanc indexer** — ``indexer-plugin.db`` ``Files.path`` prefixes
     ``/dicom-data/<old>/`` -> ``/dicom-data/<new>/`` (scripts/migration/
     indexer_paths.py in a throwaway container; the DB is backed up in the
     volume first). ``orthanc_db`` holds no paths.
  3. **stanford-stroke** — ``image_study.study_path``, ``image_series.
     dicom_dir_path`` / ``nifti_path`` / ``dicom_archive_path``,
     ``series_cache_state.cache_path`` and the ``*_labelled`` mirrors, in one
     transaction, audited before commit.

A failure undoes the steps already done (DB rolled back, indexer rewritten
back, renames reversed). Afterwards empty old patient dirs are removed and the
linked view is rebuilt. Idempotent: once nothing is misplaced it is a no-op —
so it is also how a study re-owned by link_patients.py / split_merged_patients.py
(which do not move files) is relocated later.

**Maintenance window.** ``--apply`` refuses unless Orthanc, the web app, every
backup/health timer and ingestion are stopped (it checks; it never runs sudo).

Usage (from the stack root, conda env ssc-pacs):
    python scripts/migration/move_to_dataset_layout.py                 # dry run: plan + preflight
    python scripts/migration/move_to_dataset_layout.py --apply
    python scripts/migration/move_to_dataset_layout.py --rollback maintenance/layout-move/<ts>

The journal lives in ``<checkout>/maintenance/layout-move/<timestamp>/``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(STACK_ROOT / "web-app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import indexer_paths  # noqa: E402
from storage_layout import (  # noqa: E402
    archive_dir_for,
    expected_study_dir,
    registered_slugs,
)

JOURNAL_ROOT = STACK_ROOT.parent / "maintenance" / "layout-move"
CONTAINER_ROOT = "/dicom-data"
INDEX_DB = "/vol/indexer-plugin.db"
HELPER_IMAGE = os.environ.get("LAYOUT_HELPER_IMAGE", "python:3.12-slim")
ORTHANC_STORAGE_VOLUME = os.environ.get(
    "ORTHANC_STORAGE_VOLUME", "stanford-stroke-pacs_ssc-orthanc-storage")

# Must be inactive during --apply / --rollback.
UNITS = (
    "ssc-web-app.service",
    "pg-backup-stanford-stroke.timer", "pg-backup-orthanc.timer",
    "pg-backup-freshness.timer", "orthanc-storage-backup.timer",
    "pacs-remote-backup-tier1.timer", "pacs-remote-backup-imaging.timer",
    "pacs-remote-freshness.timer", "pacs-remote-maintain-tier1.timer",
    "pacs-remote-maintain-imaging.timer",
    "cold-storage-health.timer", "cold-archive-mirror.timer",
)

# (table, column, root) — the stored path columns (cf. repoint_host_paths.py).
LOOSE, ARCHIVE = "loose", "archive"
SERIES_COLUMNS = (
    ("image_series", "dicom_dir_path", LOOSE),
    ("image_series", "nifti_path", LOOSE),
    ("image_series", "dicom_archive_path", ARCHIVE),
    ("image_series_labelled", "dicom_dir_path", LOOSE),
    ("image_series_labelled", "nifti_path", LOOSE),
    ("image_series_labelled", "dicom_archive_path", ARCHIVE),
)
STUDY_COLUMNS = (("image_study", "study_path"), ("image_study_labelled", "study_path"))


@dataclass(frozen=True)
class Move:
    studyinstanceuid: str
    patient_key: str
    old_loose: str
    new_loose: str
    old_archive: str
    new_archive: str

    def inverse(self) -> Move:
        return Move(self.studyinstanceuid, self.patient_key, self.new_loose,
                    self.old_loose, self.new_archive, self.old_archive)


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def build_plan(cur, data_root: Path, cold_root: Path) -> list[Move]:
    cur.execute(
        "SELECT studyinstanceuid, patient_key, patient_id, study_path FROM image_study "
        "WHERE coalesce(study_path, '') <> '' ORDER BY patient_key, studyinstanceuid"
    )
    moves = []
    for uid, key, pid, stored in cur.fetchall():
        new = expected_study_dir(data_root, key, pid, uid)
        if Path(stored) == new:
            continue
        old = Path(stored)
        moves.append(Move(uid, key, stored, str(new),
                          str(archive_dir_for(old, data_root, cold_root)),
                          str(archive_dir_for(new, data_root, cold_root))))
    return moves


def renames_for(moves: list[Move]) -> list[tuple[str, str]]:
    """The directory renames a plan needs (sources that exist)."""
    out = []
    for m in moves:
        for old, new in ((m.old_loose, m.new_loose), (m.old_archive, m.new_archive)):
            if os.path.isdir(old):
                out.append((old, new))
    return out


def preflight(cur, moves: list[Move], data_root: Path, cold_root: Path) -> list[str]:
    """Problems that make --apply refuse (stack state is checked separately)."""
    problems = []
    slugs = registered_slugs(cur)
    cur.execute("SELECT DISTINCT patient_id FROM image_study WHERE patient_id = ANY(%s)",
                (sorted(slugs),))
    clash = [r[0] for r in cur.fetchall()]
    if clash:
        problems.append(f"patient id(s) equal to a dataset slug: {clash}")
    for m in moves:
        for old, new in ((m.old_loose, m.new_loose), (m.old_archive, m.new_archive)):
            if os.path.isdir(old) and os.path.exists(new):
                problems.append(f"target exists: {new}")
    for root in (data_root, cold_root):
        if not root.is_dir():
            problems.append(f"storage root missing: {root}")
    for old, _new in renames_for(moves):
        root = data_root if Path(old).is_relative_to(data_root) else cold_root
        if os.stat(old).st_dev != os.stat(root).st_dev:
            problems.append(f"{old} is on another filesystem than {root} (rename would fail)")
    # A warm writes into a study dir; none may be in flight. (A leftover
    # *.warming dir from a crashed warm just moves along with its study.)
    cur.execute("SELECT count(*) FROM series_cache_state WHERE status = 'warming'")
    warming = cur.fetchone()[0]
    if warming:
        problems.append(f"{warming} series are warming (wait, or let the watchdog expire them)")
    return problems[:50]


def active_stack() -> list[str]:
    """What is still running that must not be during the move."""
    active = []
    for unit in UNITS:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        if r.stdout.strip() in ("active", "activating", "reloading"):
            active.append(unit)
    r = subprocess.run(
        ["systemctl", "list-units", "--state=active", "--no-legend", "--plain",
         "pacs-remote-backup@*.service", "pacs-remote-maintain@*.service",
         "pacs-remote-freshness.service", "pg-backup-*.service", "orthanc-storage-backup.service",
         "cold-archive-mirror.service", "cold-storage-health.service"],
        capture_output=True, text=True)
    active += [line.split()[0] for line in r.stdout.splitlines() if line.strip()]
    r = subprocess.run(["docker", "ps", "-q", "--filter", "name=^ssc-orthanc$"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        active.append(f"docker ps failed: {r.stderr.strip()}")
    elif r.stdout.strip():
        active.append("Orthanc container ssc-orthanc")
    r = subprocess.run(["pgrep", "-f", "execute_image_ingestion_protocol.py"],
                       capture_output=True, text=True)
    if r.stdout.strip():
        active.append("ingestion (execute_image_ingestion_protocol.py)")
    return active


# --------------------------------------------------------------------------- #
# Step 2: indexer
# --------------------------------------------------------------------------- #
def indexer_pairs(moves: list[Move], data_root: Path) -> list[list[str]]:
    pairs = []
    for m in moves:
        old = CONTAINER_ROOT + "/" + str(Path(m.old_loose).relative_to(data_root)) + "/"
        new = CONTAINER_ROOT + "/" + str(Path(m.new_loose).relative_to(data_root)) + "/"
        pairs.append([old, new])
    return pairs


def docker_indexer_runner(mode: str, pairs: list[list[str]], workdir: Path) -> dict:
    """Run indexer_paths.py against the Orthanc storage volume. Returns its stats."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "indexer_map.json").write_text(json.dumps({"pairs": pairs}))
    stats_file = workdir / "indexer_stats.json"
    stats_file.unlink(missing_ok=True)
    helper = Path(indexer_paths.__file__).resolve()
    ro = ":ro" if mode == "check" else ""
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ORTHANC_STORAGE_VOLUME}:/vol{ro}",
         "-v", f"{helper}:/helper.py:ro", "-v", f"{workdir}:/job",
         HELPER_IMAGE, "python", "/helper.py", mode, INDEX_DB,
         "/job/indexer_map.json", "/job/indexer_stats.json"],
        check=False,
    )
    if not stats_file.exists():
        raise RuntimeError("indexer helper produced no stats (docker run failed?)")
    stats = json.loads(stats_file.read_text())
    if "error" in stats:
        raise RuntimeError(f"indexer helper: {stats['error']}")
    return stats


# --------------------------------------------------------------------------- #
# Step 3: stanford-stroke
# --------------------------------------------------------------------------- #
def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = %s AND column_name = %s", (table, column))
    return cur.fetchone() is not None


def rewrite_db(cur, moves: list[Move]) -> dict[str, int]:
    """Rewrite every stored path of the moved studies (caller commits)."""
    cur.execute("CREATE TEMP TABLE layout_moves (uid text PRIMARY KEY, old_loose text, "
                "new_loose text, old_archive text, new_archive text) ON COMMIT DROP")
    cur.executemany("INSERT INTO layout_moves VALUES (%s, %s, %s, %s, %s)",
                    [(m.studyinstanceuid, m.old_loose, m.new_loose, m.old_archive,
                      m.new_archive) for m in moves])
    counts: dict[str, int] = {}

    def prefix_set(col, old, new):
        # Prefix compare with left(), not LIKE: '_' and '%' in paths are literal.
        return (f"{col} = m.{new} || substr({col}, length(m.{old}) + 1)",
                f"left({col}, length(m.{old}) + 1) = m.{old} || '/'")

    for table, column in STUDY_COLUMNS:
        if _column_exists(cur, table, column):
            cur.execute(f"UPDATE {table} t SET {column} = m.new_loose FROM layout_moves m "
                        f"WHERE t.studyinstanceuid = m.uid AND t.{column} = m.old_loose")
            counts[f"{table}.{column}"] = cur.rowcount
    for table, column, root in SERIES_COLUMNS:
        if not _column_exists(cur, table, column):
            continue
        old, new = ("old_loose", "new_loose") if root == LOOSE else ("old_archive", "new_archive")
        set_sql, where_sql = prefix_set(f"t.{column}", old, new)
        set_sql = set_sql.replace(f"t.{column} =", f"{column} =", 1)
        cur.execute(f"UPDATE {table} t SET {set_sql} FROM layout_moves m "
                    f"WHERE t.studyinstanceuid = m.uid AND {where_sql}")
        counts[f"{table}.{column}"] = cur.rowcount
    if _column_exists(cur, "series_cache_state", "cache_path"):
        set_sql, where_sql = prefix_set("c.cache_path", "old_loose", "new_loose")
        cur.execute(
            "UPDATE series_cache_state c SET "
            + set_sql.replace("c.cache_path =", "cache_path =", 1)
            + " FROM image_series s JOIN layout_moves m ON m.uid = s.studyinstanceuid "
            "WHERE c.seriesinstanceuid = s.seriesinstanceuid AND " + where_sql)
        counts["series_cache_state.cache_path"] = cur.rowcount

    # Audit: no stored path of a moved study still points at its old location.
    cur.execute(
        "SELECT count(*) FROM image_series s JOIN layout_moves m ON m.uid = s.studyinstanceuid "
        "WHERE left(s.dicom_dir_path, length(m.old_loose) + 1) = m.old_loose || '/' "
        "   OR left(s.dicom_archive_path, length(m.old_archive) + 1) = m.old_archive || '/'")
    left_behind = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM image_study t JOIN layout_moves m ON m.uid = "
                "t.studyinstanceuid WHERE t.study_path <> m.new_loose")
    left_behind += cur.fetchone()[0]
    if left_behind:
        raise RuntimeError(f"{left_behind} stored path(s) of moved studies were not rewritten")
    return counts


# --------------------------------------------------------------------------- #
# Journal + execution
# --------------------------------------------------------------------------- #
class Journal:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.dir / "journal.jsonl", "a", buffering=1)  # noqa: SIM115

    def write(self, **event) -> None:
        self._fh.write(json.dumps(event) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()

    @staticmethod
    def read(directory: Path) -> list[dict]:
        with open(directory / "journal.jsonl") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def _rename(src: str, dst: str) -> None:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # same filesystem; refuses to cross devices


def _prune_empty(path: Path, stop: Path) -> None:
    """rmdir now-empty dirs from ``path`` upward, never ``stop`` or above."""
    path, stop = Path(path), Path(stop)
    while path != stop and path.is_relative_to(stop):
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def execute(conn, db_moves: list[Move], renames: list[tuple[str, str]],
            indexer_moves: list[Move], *, journal: Journal, indexer_runner,
            data_root: Path, cold_root: Path) -> dict:
    """Run the three steps; on any failure undo what was done, then re-raise.

    Forward, all three take the same plan; a rollback passes only the steps its
    journal shows were done.
    """
    done: list[tuple[str, str]] = []
    indexer_done = False
    pairs = indexer_pairs(indexer_moves, data_root)
    try:
        for src, dst in renames:
            _rename(src, dst)
            done.append((src, dst))
            journal.write(event="renamed", src=src, dst=dst)
        journal.write(event="renames_done", count=len(done))

        stats = None
        if pairs:
            journal.write(event="indexer_start")
            stats = indexer_runner("apply", pairs, journal.dir / "indexer")
            indexer_done = True
            journal.write(event="indexer_done", stats=stats)

        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'move_to_dataset_layout'")
            counts = rewrite_db(cur, db_moves) if db_moves else {}
        conn.commit()
        journal.write(event="db_committed", counts=counts)
    except BaseException as exc:
        conn.rollback()
        journal.write(event="failed", error=f"{type(exc).__name__}: {exc}")
        if indexer_done:
            inverse = [[new, old] for old, new in pairs]
            indexer_runner("apply", inverse, journal.dir / "indexer-undo")
            journal.write(event="indexer_undone")
        for src, dst in reversed(done):
            _rename(dst, src)
        for _src, dst in done:
            _prune_empty(Path(dst).parent, _root_of(dst, data_root, cold_root))
        journal.write(event="undone", renames=len(done))
        raise

    for src, _dst in done:
        _prune_empty(Path(src).parent, _root_of(src, data_root, cold_root))
    return {"renames": len(done), "indexer": stats, "db": counts}


def _root_of(path: str, data_root: Path, cold_root: Path) -> Path:
    return data_root if Path(path).is_relative_to(data_root) else cold_root


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_plan(moves: list[Move], renames, data_root: Path) -> None:
    legacy = sum(1 for m in moves if len(Path(m.old_loose).relative_to(data_root).parts) == 2)
    by_slug: dict[str, int] = {}
    for m in moves:
        by_slug[m.patient_key.split("__", 1)[0]] = by_slug.get(m.patient_key.split("__", 1)[0], 0) + 1
    print(f"Studies to move:          {len(moves)}  ({legacy} from the pre-v2.2 layout, "
          f"{len(moves) - legacy} relocations)")
    for slug, n in sorted(by_slug.items()):
        print(f"  into {slug + '/':24s}{n}")
    loose = sum(1 for s, _ in renames if Path(s).is_relative_to(data_root))
    print(f"Directory renames:        {len(renames)}  ({loose} loose, {len(renames) - loose} archive)")
    for m in moves[:3]:
        print(f"  e.g. {m.old_loose}\n    -> {m.new_loose}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Move (default: dry run)")
    mode.add_argument("--rollback", type=Path, metavar="JOURNAL_DIR",
                      help="Undo a completed (or interrupted) --apply from its journal")
    ap.add_argument("--no-indexer", action="store_true",
                    help="Skip the Orthanc indexer step (only for a stack without Orthanc)")
    args = ap.parse_args()

    import linked_view
    from db import get_conn

    from config import COLD_ARCHIVE_ROOT, DICOM_DATA_ROOT

    data_root, cold_root = DICOM_DATA_ROOT, COLD_ARCHIVE_ROOT
    conn = get_conn()
    try:
        if args.rollback:
            return _rollback(conn, args.rollback, args, data_root, cold_root)
        with conn.cursor() as cur:
            moves = build_plan(cur, data_root, cold_root)
            problems = preflight(cur, moves, data_root, cold_root) if moves else []
        conn.rollback()
        if not moves:
            print("Nothing to move: every study is where the dataset layout puts it.")
            return 0
        renames = renames_for(moves)
        _print_plan(moves, renames, data_root)

        if not args.apply:
            if not args.no_indexer:
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        stats = docker_indexer_runner(
                            "check", indexer_pairs(moves, data_root), Path(tmp))
                    print(f"Indexer rows to rewrite:  {stats['rows_to_move']} of "
                          f"{stats['files_rows']} ({stats['prefixes_without_rows']} "
                          "moved studies have none: never indexed or evicted-and-pruned)")
                except (OSError, RuntimeError) as exc:
                    print(f"Indexer check skipped: {exc}")
            for p in problems:
                print(f"PREFLIGHT: {p}")
            running = active_stack()
            if running:
                print("Still running (stop before --apply): " + ", ".join(running))
            print("\nDRY RUN — nothing changed. Back up both databases and the Orthanc "
                  "storage volume, then re-run with --apply.")
            return 0

        if problems:
            for p in problems:
                print(f"PREFLIGHT: {p}", file=sys.stderr)
            return 1
        running = active_stack()
        if running:
            print("Refusing: still running: " + ", ".join(running), file=sys.stderr)
            return 1

        journal = Journal(JOURNAL_ROOT / time.strftime("%Y%m%dT%H%M%S"))
        (journal.dir / "plan.json").write_text(json.dumps([asdict(m) for m in moves]))
        journal.write(event="start", moves=len(moves), renames=len(renames))
        print(f"Journal: {journal.dir}")
        started = time.monotonic()
        try:
            result = execute(conn, moves, renames, [] if args.no_indexer else moves,
                             journal=journal, indexer_runner=docker_indexer_runner,
                             data_root=data_root, cold_root=cold_root)
        finally:
            journal.close()
        print(f"Moved {len(moves)} studies in {time.monotonic() - started:.0f}s: "
              f"{result['renames']} renames, DB {result['db']}")
        if result["indexer"]:
            print(f"Indexer: {result['indexer']['rows_to_move']} rows rewritten "
                  f"(backup in the volume: {result['indexer'].get('backup')})")
        for line in linked_view.after_identity_change(conn):
            print(line)
        print("\nNext: start Orthanc (scripts/orthanc/dc.sh up -d), the web app and the "
              "timers; then run the audits (docs/operations/dataset_layout.md).")
        return 0
    finally:
        conn.close()


def _rollback(conn, journal_dir: Path, args, data_root: Path, cold_root: Path) -> int:
    events = Journal.read(journal_dir)
    kinds = {e["event"] for e in events}
    if "undone" in kinds:
        print("That run already undid itself; nothing to roll back.")
        return 0
    running = active_stack()
    if running:
        print("Refusing: still running: " + ", ".join(running), file=sys.stderr)
        return 1
    plan = [Move(**m) for m in json.loads((journal_dir / "plan.json").read_text())]
    inverse = [m.inverse() for m in plan]
    renamed = [(e["dst"], e["src"]) for e in events if e["event"] == "renamed"][::-1]
    print(f"Rolling back {journal_dir}: {len(renamed)} renames, "
          f"indexer={'indexer_done' in kinds}, db={'db_committed' in kinds}")
    journal = Journal(journal_dir / "rollback")
    try:
        # Undo only what the forward run did (an interrupted run may not have
        # reached the indexer or the DB commit).
        execute(conn, inverse if "db_committed" in kinds else [], renamed,
                inverse if "indexer_done" in kinds and not args.no_indexer else [],
                journal=journal, indexer_runner=docker_indexer_runner,
                data_root=data_root, cold_root=cold_root)
    finally:
        journal.close()
    print("Rolled back. Start the stack and run the audits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
