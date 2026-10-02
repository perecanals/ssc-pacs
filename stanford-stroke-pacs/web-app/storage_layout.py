"""On-disk imaging layout: ``<root>/<dataset slug>/<patient_id>/<StudyUID>/…``.

Imaging is filed under the dataset of the enrollment that *owns* it (the slug
prefix of its ``patient_key``, ``<slug>__<patient_id>``) and the imaging row's
``patient_id`` (the DICOM PatientID). The same layout holds in both storage
roots — the loose/warm tree (``dicom_data_root``) and the archive tree
(``cold_archive_root``), which mirrors it relatively. Slugs are immutable, so a
dataset rename never moves files.

The stored paths (``image_study.study_path``, ``image_series.dicom_dir_path`` /
``dicom_archive_path``) are the source of truth for where a study *is*; this
module says where it *should be*. They differ when ownership changed after
ingestion (``patient_identity.move_studies`` does not move files) or on a tree
still in the pre-v2.2 ``<root>/<patient_id>/<StudyUID>`` layout — both are
fixed by ``scripts/migration/move_to_dataset_layout.py``.

Tree walkers must not assume the depth: ``patient_dirs`` yields both
dataset-level patient dirs and legacy top-level ones, told apart by the
registered slugs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

# A DICOM UID: dot-separated digit groups. Study directories are named by one;
# patient ids and slugs never contain a dot.
DICOM_UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)+$")


def slug_of(patient_key: str) -> str:
    """The dataset slug of a ``patient_key`` (``crisp2-lvo__4-1161`` → ``crisp2-lvo``)."""
    slug, sep, _ = str(patient_key).partition("__")
    if not sep or not slug:
        raise ValueError(f"not a patient_key: {patient_key!r}")
    return slug


def study_relpath(slug: str, patient_id: str, study_uid: str) -> Path:
    """Where a study lives below a storage root."""
    for part in (slug, patient_id, study_uid):
        part = str(part)
        if not part or part in (".", "..") or "/" in part or "\x00" in part:
            raise ValueError(f"unsafe path component: {part!r}")
    return Path(str(slug)) / str(patient_id) / str(study_uid)


def expected_study_dir(root: Path, patient_key: str, patient_id: str, study_uid: str) -> Path:
    return Path(root) / study_relpath(slug_of(patient_key), patient_id, study_uid)


def archive_dir_for(loose_dir: Path, data_root: Path, cold_root: Path) -> Path:
    """The archive-tree twin of a loose-tree directory (same relative path)."""
    return Path(cold_root) / Path(loose_dir).relative_to(data_root)


def registered_slugs(cur) -> set[str]:
    cur.execute("SELECT slug FROM dataset")
    return {r[0] if not isinstance(r, dict) else r["slug"] for r in cur.fetchall()}


def patient_dirs(root: Path, slugs: set[str]) -> Iterator[tuple[str | None, str, Path]]:
    """Every patient directory under ``root`` as ``(slug, patient_id, path)``.

    A top-level entry named like a registered slug is a dataset directory and
    its children are patients; any other top-level directory is a legacy
    ``<root>/<patient_id>`` one (slug ``None``).
    """
    root = Path(root)
    if not root.is_dir():
        return
    for top in sorted(root.iterdir()):
        if not top.is_dir():
            continue
        if top.name in slugs:
            for pdir in sorted(top.iterdir()):
                if pdir.is_dir():
                    yield top.name, pdir.name, pdir
        else:
            yield None, top.name, top


def select_patient_dirs(root: Path, slugs: set[str],
                        selectors: list[str] | None) -> list[tuple[str, str, Path]]:
    """Existing patient dirs as ``(label, patient_id, path)`` for an audit.

    ``selectors`` None = every patient dir. Otherwise each is a ``patient_key``
    (that enrollment's dir) or a bare patient id (its dir in every dataset,
    plus a legacy top-level one).
    """
    root = Path(root)
    if selectors is None:
        found = list(patient_dirs(root, slugs))
    else:
        found = []
        for sel in selectors:
            if "__" in sel:
                slug, _, pid = sel.partition("__")
                found.append((slug, pid, root / slug / pid))
            else:
                found.extend((s, sel, root / s / sel) for s in sorted(slugs))
                found.append((None, sel, root / sel))
    return [(f"{slug}/{pid}" if slug else pid, pid, path)
            for slug, pid, path in found if path.is_dir()]


def study_dir_depth_ok(rel_parts: tuple[str, ...]) -> bool:
    """True when a path below a root is a study directory or deeper.

    The study level is the first component that is a DICOM UID, at depth 1
    (legacy ``<pid>/<study>``) or 2 (``<slug>/<pid>/<study>``). Anything
    shallower is a root, a dataset or a whole patient.
    """
    return any(
        i < len(rel_parts) and DICOM_UID_RE.match(rel_parts[i]) for i in (1, 2)
    )


def misplaced_studies(cur, data_root: Path) -> list[dict]:
    """Studies whose stored ``study_path`` is not where the layout puts them.

    Each item: ``studyinstanceuid``, ``patient_key``, ``patient_id``,
    ``current`` (the stored loose study dir) and ``expected``.
    """
    cur.execute(
        "SELECT studyinstanceuid, patient_key, patient_id, study_path FROM image_study "
        "WHERE coalesce(study_path, '') <> '' ORDER BY patient_key, studyinstanceuid"
    )
    out = []
    for row in cur.fetchall():
        uid, key, pid, current = (
            (row["studyinstanceuid"], row["patient_key"], row["patient_id"], row["study_path"])
            if isinstance(row, dict) else row
        )
        expected = expected_study_dir(data_root, key, pid, uid)
        if Path(current) != expected:
            out.append({"studyinstanceuid": uid, "patient_key": key, "patient_id": pid,
                        "current": current, "expected": str(expected)})
    return out
