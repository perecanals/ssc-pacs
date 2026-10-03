"""Dataset layout (v2.2): <root>/<slug>/<patient_id>/<StudyUID>.

storage_layout helpers, the one-off/relocation move
(scripts/migration/move_to_dataset_layout.py + indexer_paths.py) against tmp
storage roots, a real indexer-plugin.db schema and the test DB, and the linked
view (linked_view.py).

Geometry: P-LAY is two different people — enrolled in lvo (owns STUDY_A) and in
crisp2 (owns STUDY_B) — whose studies share the pre-v2.2 folder <root>/P-LAY,
like the 12 split CRISP2/PRECISE ids.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import psycopg2
import pytest

import linked_view
import storage_layout
from tests.conftest import insert_patient

STACK = Path(__file__).resolve().parents[2]
PREPARE_SQL = STACK.parent / "orthanc-indexer-patched" / "src" / "Sources" / "PrepareDatabase.sql"

PID = "P-LAY"
STUDY_A, STUDY_B = "1.2.990.1", "1.2.990.2"
SERIES = {STUDY_A: "1.2.990.1.1", STUDY_B: "1.2.990.2.1"}
OWNER = {STUDY_A: "lvo__P-LAY", STUDY_B: "crisp2__P-LAY"}


def _load(name):
    path = STACK / "scripts" / "migration" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up
    spec.loader.exec_module(mod)
    return mod


mover = _load("move_to_dataset_layout")
indexer_paths = _load("indexer_paths")


# --------------------------------------------------------------------------- #
# storage_layout
# --------------------------------------------------------------------------- #
class TestStorageLayout:
    def test_paths(self):
        assert storage_layout.slug_of("crisp2-lvo__4-1161") == "crisp2-lvo"
        assert storage_layout.study_relpath("precise", "11-001", "1.2.3") == \
            Path("precise/11-001/1.2.3")
        for bad in ("", "..", "a/b"):
            with pytest.raises(ValueError):
                storage_layout.study_relpath("precise", bad, "1.2.3")
        with pytest.raises(ValueError):
            storage_layout.slug_of("no-separator")

    def test_patient_dirs_reads_both_layouts(self, tmp_path):
        for d in ("lvo/P-1/1.2", "lvo/P-2", "P-OLD/1.3", "crisp2"):
            (tmp_path / d).mkdir(parents=True)
        (tmp_path / "stray.txt").write_text("x")
        found = [(s, p) for s, p, _ in storage_layout.patient_dirs(tmp_path, {"lvo", "crisp2"})]
        assert found == [(None, "P-OLD"), ("lvo", "P-1"), ("lvo", "P-2")]
        picked = storage_layout.select_patient_dirs(tmp_path, {"lvo", "crisp2"}, ["P-1", "lvo__P-2"])
        assert [label for label, _pid, _p in picked] == ["lvo/P-1", "lvo/P-2"]

    def test_study_depth(self):
        ok = storage_layout.study_dir_depth_ok
        assert ok(("lvo", "P-1", "1.2.3")) and ok(("P-1", "1.2.3"))
        assert not ok(("lvo", "P-1")) and not ok(("P-1",)) and not ok(())


# --------------------------------------------------------------------------- #
# The move
# --------------------------------------------------------------------------- #
@pytest.fixture()
def roots(tmp_path):
    return tmp_path / "imaging", tmp_path / "cold"


@pytest.fixture()
def legacy_tree(seeded_db, roots):
    """P-LAY's two people filed under one pre-v2.2 folder, on disk and in the DB."""
    data_root, cold_root = roots
    conn = psycopg2.connect(**seeded_db)
    with conn.cursor() as cur:
        insert_patient(cur, PID, "lvo")
        insert_patient(cur, PID, "crisp2")
        for uid, key in OWNER.items():
            study = data_root / PID / uid
            series = study / "CTA" / SERIES[uid] / "DICOM"
            archive = cold_root / PID / uid / "CTA" / SERIES[uid] / "DICOM.tar.zst"
            cur.execute(
                "INSERT INTO image_study (patient_id, patient_key, subject_id, studyinstanceuid, "
                "study_path) VALUES (%s, %s, %s, %s, %s)", (PID, key, key, uid, str(study)))
            cur.execute(
                "INSERT INTO image_series (patient_id, patient_key, subject_id, studyinstanceuid, "
                "seriesinstanceuid, dicom_dir_path, dicom_archive_path) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (PID, key, key, uid, SERIES[uid], str(series), str(archive)))
            cur.execute("INSERT INTO series_cache_state (seriesinstanceuid, status, cache_path) "
                        "VALUES (%s, 'hot', %s)", (SERIES[uid], str(series)))
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"zst")
        # STUDY_A is warm (loose files present); STUDY_B is evicted.
        loose_a = data_root / PID / STUDY_A / "CTA" / SERIES[STUDY_A] / "DICOM"
        loose_a.mkdir(parents=True)
        (loose_a / "IM1.dcm").write_bytes(b"dcm")
    conn.commit()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM series_cache_state WHERE seriesinstanceuid = ANY(%s)",
                    (list(SERIES.values()),))
        cur.execute("DELETE FROM image_series WHERE studyinstanceuid = ANY(%s)", (list(OWNER),))
        cur.execute("DELETE FROM image_study WHERE studyinstanceuid = ANY(%s)", (list(OWNER),))
        cur.execute("DELETE FROM patient WHERE patient_id = %s", (PID,))
    conn.commit()
    conn.close()


@pytest.fixture()
def index_db(tmp_path):
    db = tmp_path / "indexer-plugin.db"
    con = sqlite3.connect(db)
    con.executescript(PREPARE_SQL.read_text())
    rows = [
        (f"/dicom-data/{PID}/{STUDY_A}/CTA/{SERIES[STUDY_A]}/DICOM/IM1.dcm", "iA"),
        (f"/dicom-data/{PID}/{STUDY_B}/CTA/{SERIES[STUDY_B]}/DICOM/IM1.dcm", "iB"),
        ("/dicom-data/P-OTHER/1.2.991/CTA/1.2.991.1/DICOM/IM1.dcm", "iO"),
    ]
    con.executemany("INSERT INTO Files VALUES (?, 1, 1, 1, ?)", rows)
    con.executemany("INSERT INTO Attachments VALUES (?, ?)",
                    [("uA", "iA"), ("uB", "iB"), ("uO", "iO")])
    con.commit()
    con.close()
    return db


def _runner(db, calls=None, fail=False):
    def run(mode, pairs, _workdir):
        if calls is not None:
            calls.append(mode)
        if fail:
            raise RuntimeError("indexer down")
        return indexer_paths.rewrite(str(db), pairs, apply=(mode == "apply"))
    return run


def _paths(index_db):
    con = sqlite3.connect(index_db)
    try:
        return sorted(r[0] for r in con.execute("SELECT path FROM Files"))
    finally:
        con.close()


def _plan(conn, roots):
    with conn.cursor() as cur:
        moves = mover.build_plan(cur, *roots)
    conn.rollback()
    return [m for m in moves if m.studyinstanceuid in OWNER]


def _stored(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT st.studyinstanceuid, st.study_path, s.dicom_dir_path, "
                    "s.dicom_archive_path, c.cache_path FROM image_study st "
                    "JOIN image_series s USING (studyinstanceuid) "
                    "JOIN series_cache_state c USING (seriesinstanceuid) "
                    "WHERE st.studyinstanceuid = ANY(%s) ORDER BY 1", (list(OWNER),))
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _execute(conn, moves, roots, journal_dir, runner):
    journal = mover.Journal(journal_dir)
    (journal_dir / "plan.json").write_text(json.dumps([m.__dict__ for m in moves]))
    try:
        return mover.execute(conn, moves, mover.renames_for(moves), moves, journal=journal,
                             indexer_runner=runner, data_root=roots[0], cold_root=roots[1])
    finally:
        journal.close()


class TestMove:
    def test_plan_and_preflight(self, legacy_tree, roots):
        moves = _plan(legacy_tree, roots)
        assert {(m.studyinstanceuid, m.new_loose) for m in moves} == {
            (STUDY_A, str(roots[0] / "lvo" / PID / STUDY_A)),
            (STUDY_B, str(roots[0] / "crisp2" / PID / STUDY_B)),
        }
        # Warm A has a loose dir; evicted B only its archive.
        assert len(mover.renames_for(moves)) == 3
        with legacy_tree.cursor() as cur:
            assert mover.preflight(cur, moves, *roots) == []
        (roots[0] / "lvo" / PID / STUDY_A).mkdir(parents=True)
        with legacy_tree.cursor() as cur:
            assert any("target exists" in p for p in mover.preflight(cur, moves, *roots))

    def test_apply_moves_files_indexer_and_db(self, legacy_tree, roots, index_db, tmp_path):
        data_root, cold_root = roots
        moves = _plan(legacy_tree, roots)
        result = _execute(legacy_tree, moves, roots, tmp_path / "j", _runner(index_db))
        assert result["renames"] == 3

        assert (data_root / "lvo" / PID / STUDY_A / "CTA" / SERIES[STUDY_A] / "DICOM"
                / "IM1.dcm").is_file()
        for uid, slug in ((STUDY_A, "lvo"), (STUDY_B, "crisp2")):
            assert (cold_root / slug / PID / uid / "CTA" / SERIES[uid] / "DICOM.tar.zst").is_file()
        assert not (data_root / PID).exists() and not (cold_root / PID).exists()

        a, b = _stored(legacy_tree)
        assert a[1] == str(data_root / "lvo" / PID / STUDY_A)
        assert a[2] == a[4] == f"{a[1]}/CTA/{SERIES[STUDY_A]}/DICOM"
        assert a[3] == str(cold_root / "lvo" / PID / STUDY_A / "CTA" / SERIES[STUDY_A]
                           / "DICOM.tar.zst")
        assert b[1] == str(data_root / "crisp2" / PID / STUDY_B)

        assert _paths(index_db) == sorted([
            f"/dicom-data/lvo/{PID}/{STUDY_A}/CTA/{SERIES[STUDY_A]}/DICOM/IM1.dcm",
            f"/dicom-data/crisp2/{PID}/{STUDY_B}/CTA/{SERIES[STUDY_B]}/DICOM/IM1.dcm",
            "/dicom-data/P-OTHER/1.2.991/CTA/1.2.991.1/DICOM/IM1.dcm",
        ])
        # Idempotent: nothing left to move.
        assert _plan(legacy_tree, roots) == []

    def test_indexer_failure_undoes_the_renames(self, legacy_tree, roots, index_db, tmp_path):
        before_db, before_idx = _stored(legacy_tree), _paths(index_db)
        moves = _plan(legacy_tree, roots)
        with pytest.raises(RuntimeError, match="indexer down"):
            _execute(legacy_tree, moves, roots, tmp_path / "j", _runner(index_db, fail=True))
        assert (roots[0] / PID / STUDY_A / "CTA").is_dir()
        assert (roots[1] / PID / STUDY_B).is_dir()
        assert not (roots[0] / "lvo").exists() and not (roots[1] / "crisp2").exists()
        assert _stored(legacy_tree) == before_db and _paths(index_db) == before_idx

    def test_db_failure_undoes_indexer_and_renames(self, legacy_tree, roots, index_db,
                                                   tmp_path, monkeypatch):
        before_db, before_idx = _stored(legacy_tree), _paths(index_db)

        def broken(cur, moves):
            raise RuntimeError("db audit failed")

        monkeypatch.setattr(mover, "rewrite_db", broken)
        calls = []
        moves = _plan(legacy_tree, roots)
        with pytest.raises(RuntimeError, match="db audit failed"):
            _execute(legacy_tree, moves, roots, tmp_path / "j", _runner(index_db, calls))
        assert calls == ["apply", "apply"]  # forward, then inverse
        assert _stored(legacy_tree) == before_db and _paths(index_db) == before_idx
        assert (roots[0] / PID / STUDY_A / "CTA").is_dir()

    def test_rollback_restores_everything(self, legacy_tree, roots, index_db, tmp_path,
                                          monkeypatch):
        before_db, before_idx = _stored(legacy_tree), _paths(index_db)
        jdir = tmp_path / "j"
        _execute(legacy_tree, _plan(legacy_tree, roots), roots, jdir, _runner(index_db))
        assert _stored(legacy_tree) != before_db

        monkeypatch.setattr(mover, "active_stack", lambda: [])
        monkeypatch.setattr(mover, "docker_indexer_runner", _runner(index_db))
        args = type("A", (), {"no_indexer": False})()
        assert mover._rollback(legacy_tree, jdir, args, *roots) == 0
        assert _stored(legacy_tree) == before_db and _paths(index_db) == before_idx
        assert (roots[0] / PID / STUDY_A / "CTA" / SERIES[STUDY_A] / "DICOM" / "IM1.dcm").is_file()
        assert not (roots[0] / "lvo").exists()

    def test_apply_refuses_while_the_stack_runs(self, monkeypatch, legacy_tree):
        monkeypatch.setattr(mover, "active_stack", lambda: ["Orthanc container ssc-orthanc"])
        monkeypatch.setattr("sys.argv", ["move_to_dataset_layout.py", "--apply"])
        monkeypatch.setattr(mover, "build_plan", lambda cur, a, b: [
            mover.Move(STUDY_A, OWNER[STUDY_A], "/x/a", "/x/b", "/y/a", "/y/b")])
        monkeypatch.setattr(mover, "preflight", lambda *a: [])
        monkeypatch.setattr(mover, "_print_plan", lambda *a: None)
        assert mover.main() == 1


class TestIndexerPaths:
    def test_refuses_an_occupied_target_and_a_nested_target(self, index_db):
        with pytest.raises(RuntimeError, match="already hold rows"):
            indexer_paths.rewrite(str(index_db), [[f"/dicom-data/{PID}/{STUDY_A}/",
                                                   "/dicom-data/P-OTHER/1.2.991/"]], apply=True)
        with pytest.raises(ValueError, match="under a prefix being moved"):
            indexer_paths.rewrite(str(index_db), [["/dicom-data/a/", "/dicom-data/a/b/"]],
                                  apply=False)

    def test_check_counts_without_writing(self, index_db):
        before = _paths(index_db)
        stats = indexer_paths.rewrite(
            str(index_db), [[f"/dicom-data/{PID}/{STUDY_A}/", f"/dicom-data/lvo/{PID}/{STUDY_A}/"],
                            ["/dicom-data/none/1.2/", "/dicom-data/lvo/none/1.2/"]], apply=False)
        assert (stats["rows_to_move"], stats["prefixes_without_rows"]) == (1, 1)
        assert _paths(index_db) == before


# --------------------------------------------------------------------------- #
# Linked view
# --------------------------------------------------------------------------- #
class TestLinkedView:
    def test_links_shared_imaging_and_prunes(self, legacy_tree, roots, tmp_path):
        data_root, cold_root = roots
        view = tmp_path / "view"
        with legacy_tree.cursor() as cur:
            # Link crisp2__P-LAY to the lvo person (same subject).
            cur.execute("UPDATE patient SET subject_id = 'lvo__P-LAY' "
                        "WHERE patient_key = 'crisp2__P-LAY'")
            cur.execute("UPDATE image_study SET subject_id = 'lvo__P-LAY' "
                        "WHERE studyinstanceuid = %s", (STUDY_B,))
            (view / "lvo" / "STALE").mkdir(parents=True)
            (view / "lvo" / "STALE" / "1.9").symlink_to(tmp_path / "gone")
            dry = linked_view.rebuild(cur, view, data_root, cold_root, apply=False)
            assert not (view / "crisp2").exists()
            result = linked_view.rebuild(cur, view, data_root, cold_root, apply=True)
            again = linked_view.rebuild(cur, view, data_root, cold_root, apply=True)
        legacy_tree.rollback()
        assert dry["created"] == result["created"]
        assert sorted(result["created"]) == [f"crisp2/{PID}/{STUDY_A}", f"lvo/{PID}/{STUDY_B}"]
        assert result["removed"] == ["lvo/STALE/1.9"] and not (view / "lvo" / "STALE").exists()
        assert (view / "crisp2" / PID / STUDY_A).resolve() == cold_root / PID / STUDY_A
        assert (again["created"], again["removed"], again["kept"]) == ([], [], 2)

    def test_disabled_without_a_root(self, legacy_tree, roots):
        with legacy_tree.cursor() as cur:
            assert linked_view.rebuild(cur, None, *roots, apply=True)["enabled"] is False
        legacy_tree.rollback()

    def test_root_must_be_outside_the_storage_roots(self):
        import config

        with pytest.raises(RuntimeError, match="outside"):
            config._linked_view_root(str(config.COLD_ARCHIVE_ROOT / "view"))
        with pytest.raises(RuntimeError, match="absolute"):
            config._linked_view_root("relative/view")
        assert config._linked_view_root("") is None
