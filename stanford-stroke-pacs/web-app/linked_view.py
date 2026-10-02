"""The linked view: a browsable symlink tree of imaging shared across enrollments.

Imaging is filed once, under the enrollment that owns it
(``<root>/<slug>/<patient_id>/<StudyUID>``, see ``storage_layout``). A person
enrolled in several datasets (one subject, linked with link_patients.py) sees
studies owned by another of their enrollments only through the database. This
tree gives the same view on disk:

    <linked_view_root>/<slug>/<patient_id>/<StudyUID>  ->  <cold_archive_root>/<owner's study dir>

one link per (enrollment, study of its subject it does not own). Targets are the
archive tree — canonical in cold_path_cache mode (the loose tree is only a
cache). The tree is a **convenience view, not a source of truth**: generated,
rebuildable at any time, and kept outside the storage roots (config.py checks)
so Orthanc, the audits, deletion and backups never walk it.

Rebuilt by scripts/cold_storage/build_linked_view.py and after
link_patients.py / split_merged_patients.py / move_to_dataset_layout.py apply.
Unset ``[storage].linked_view_root`` = disabled (every call is a no-op).
"""

from __future__ import annotations

import os
from pathlib import Path

from storage_layout import study_relpath

# Every enrollment whose subject has imaging it does not own, with that imaging.
_SHARED_SQL = """
SELECT p.patient_key, p.dataset_slug, p.patient_id, st.studyinstanceuid, st.study_path
FROM (SELECT pt.*, d.slug AS dataset_slug FROM patient pt JOIN dataset d ON d.name = pt.dataset) p
JOIN image_study st ON st.subject_id = p.subject_id AND st.patient_key <> p.patient_key
WHERE coalesce(st.study_path, '') <> ''
ORDER BY p.patient_key, st.studyinstanceuid
"""


def desired_links(cur, data_root: Path, cold_root: Path) -> dict[Path, Path]:
    """{link path relative to the view root: absolute target}."""
    cur.execute(_SHARED_SQL)
    out: dict[Path, Path] = {}
    for row in cur.fetchall():
        _key, slug, pid, uid, study_path = (
            (row["patient_key"], row["dataset_slug"], row["patient_id"],
             row["studyinstanceuid"], row["study_path"]) if isinstance(row, dict) else row
        )
        try:
            rel = Path(study_path).relative_to(data_root)
        except ValueError:
            continue  # stored outside the loose root: nothing to point at
        out[study_relpath(slug, pid, uid)] = Path(cold_root) / rel
    return out


def rebuild(cur, view_root: Path | None, data_root: Path, cold_root: Path,
            *, apply: bool) -> dict:
    """Make ``view_root`` hold exactly the desired links. Idempotent.

    Removes stale or dangling links and the directories they leave empty;
    never removes anything that is not a symlink (reported as ``foreign``).
    Returns counts plus up to a few examples per kind.
    """
    result = {"enabled": view_root is not None, "created": [], "replaced": [],
              "removed": [], "foreign": [], "kept": 0}
    if view_root is None:
        return result
    view_root = Path(view_root)
    want = desired_links(cur, data_root, cold_root)

    existing: dict[Path, Path] = {}
    if view_root.is_dir():
        for dirpath, dirnames, filenames in os.walk(view_root):
            for name in dirnames + filenames:
                full = Path(dirpath) / name
                if full.is_symlink():
                    existing[full.relative_to(view_root)] = Path(os.readlink(full))
                elif name in filenames:
                    result["foreign"].append(str(full))
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]

    for rel, target in existing.items():
        if rel not in want:
            result["removed"].append(str(rel))
        elif want[rel] != target or not target.exists():
            result["replaced"].append(str(rel))
    for rel in want:
        if rel not in existing:
            result["created"].append(str(rel))
        elif rel not in result["replaced"]:
            result["kept"] += 1
    replaced = set(result["replaced"])

    if apply:
        for rel in result["removed"] + result["replaced"]:
            (view_root / rel).unlink()
        for rel, target in want.items():
            if str(rel) in replaced or str(rel) in result["created"]:
                link = view_root / rel
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
        _prune_empty_dirs(view_root)
    return result


def _prune_empty_dirs(view_root: Path) -> None:
    for dirpath, _dirnames, _filenames in os.walk(view_root, topdown=False):
        path = Path(dirpath)
        if path != view_root and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()


def summary(result: dict) -> str:
    if not result["enabled"]:
        return "linked view: disabled ([storage].linked_view_root unset)"
    return (f"linked view: {len(result['created'])} created, {len(result['replaced'])} "
            f"replaced, {len(result['removed'])} removed, {result['kept']} kept"
            + (f", {len(result['foreign'])} non-link file(s) left alone"
               if result["foreign"] else ""))


def after_identity_change(conn) -> list[str]:
    """Run after linking/unlinking/moving studies: rebuild the view, report misplaced.

    Returns lines to print. A failure to rebuild is reported, never raised: the
    identity change is already committed and the view is rebuildable.
    """
    from config import COLD_ARCHIVE_ROOT, DICOM_DATA_ROOT, LINKED_VIEW_ROOT
    from storage_layout import misplaced_studies

    lines = []
    with conn.cursor() as cur:
        try:
            result = rebuild(cur, LINKED_VIEW_ROOT, DICOM_DATA_ROOT, COLD_ARCHIVE_ROOT,
                             apply=True)
            lines.append(summary(result))
        except OSError as exc:
            lines.append(f"linked view NOT rebuilt ({exc}); run "
                         "scripts/cold_storage/build_linked_view.py --apply")
        misplaced = misplaced_studies(cur, DICOM_DATA_ROOT)
    conn.rollback()  # read-only
    if misplaced:
        lines.append(
            f"{len(misplaced)} studie(s) are filed under another enrollment's folder "
            "(files are not moved on link/split; everything keeps working). Relocate "
            "them in a maintenance window: scripts/migration/move_to_dataset_layout.py")
    return lines
