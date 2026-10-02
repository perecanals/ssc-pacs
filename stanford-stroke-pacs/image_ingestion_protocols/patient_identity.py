"""Subject operations on the patient identity model (Alembic 0026).

A `patient` row is an enrollment — one (dataset, patient_id) pair, keyed by
patient_key — and enrollments of the same person share a subject_id. Each study
(and its series) is owned by one enrollment (patient_key) and carries that
enrollment's subject. These helpers are the only sanctioned way to change a
subject or an owner after ingestion; they keep the invariants reconciliation
checks (a series has its study's owner and subject; a study has its owner's
subject).

All functions take a psycopg2 cursor and run inside the caller's transaction.
After a batch of changes, call :func:`refresh_subjects` once for every subject
touched, so stroke_date, ranks, timepoints and the mirrors follow.
"""

from __future__ import annotations

from labelled_table_sync import sync_labelled_rows
from series_classification import ASSIGN_RANKS_SQL, CLEAR_RANKS_SQL
from subject_timepoints import recompute_subject_timepoints


def enrollment(cur, patient_key: str) -> dict | None:
    cur.execute(
        "SELECT patient_key, subject_id, patient_id, dataset FROM patient "
        "WHERE patient_key = %s",
        (patient_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ("patient_key", "subject_id", "patient_id", "dataset")
    return dict(row) if isinstance(row, dict) else dict(zip(keys, row, strict=True))


def enrollment_key(cur, dataset: str, patient_id: str) -> str | None:
    cur.execute(
        "SELECT patient_key FROM patient WHERE dataset = %s AND patient_id = %s",
        (dataset, patient_id),
    )
    row = cur.fetchone()
    return None if row is None else (row["patient_key"] if isinstance(row, dict) else row[0])


def dataset_slug(cur, dataset: str) -> str | None:
    cur.execute("SELECT slug FROM dataset WHERE name = %s", (dataset,))
    row = cur.fetchone()
    return None if row is None else (row["slug"] if isinstance(row, dict) else row[0])


def create_enrollment(cur, dataset: str, patient_id: str, subject_id: str,
                      import_label: str) -> str:
    """Enroll ``patient_id`` in ``dataset`` as part of an existing subject
    (no imaging of its own). Returns the new patient_key."""
    slug = dataset_slug(cur, dataset)
    if slug is None:
        raise ValueError(f"dataset {dataset!r} is not registered")
    key = f"{slug}__{patient_id}"
    cur.execute(
        "INSERT INTO patient (patient_key, subject_id, patient_id, dataset, import_label) "
        "VALUES (%s, %s, %s, %s, %s)",
        (key, subject_id, patient_id, dataset, import_label),
    )
    return key


def _set_subject(cur, new_subject: str, *, where: str, params: tuple) -> None:
    for table in ("patient", "image_study", "image_series"):
        cur.execute(f"UPDATE {table} SET subject_id = %s WHERE {where}", (new_subject, *params))


def link(cur, source_key: str, target_key: str) -> tuple[str, str]:
    """Make ``source_key``'s person the same as ``target_key``'s.

    Every enrollment and every study/series of the source's subject joins the
    target's subject (imaging both sides own stays owned where it is). Returns
    (old_subject, new_subject); equal when they were already linked.
    """
    source, target = enrollment(cur, source_key), enrollment(cur, target_key)
    if source is None or target is None:
        raise ValueError(f"unknown enrollment: {source_key if source is None else target_key}")
    old, new = source["subject_id"], target["subject_id"]
    if old != new:
        _set_subject(cur, new, where="subject_id = %s", params=(old,))
    return old, new


def unlink(cur, patient_key: str) -> tuple[str, str | None]:
    """Make ``patient_key`` its own person again, with the imaging it owns.

    The rest of the subject keeps the rest. When the enrollment was the one
    whose key names the subject, the rest move to a new subject named after
    their first key. Returns (subject of patient_key, subject of the rest or
    None when nothing else shared it).
    """
    me = enrollment(cur, patient_key)
    if me is None:
        raise ValueError(f"unknown enrollment: {patient_key}")
    subject = me["subject_id"]
    cur.execute(
        "SELECT patient_key FROM patient WHERE subject_id = %s AND patient_key <> %s "
        "ORDER BY patient_key",
        (subject, patient_key),
    )
    rest = [r["patient_key"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    if not rest:
        return subject, None
    if subject == patient_key:
        # The others keep their imaging under a subject named after one of them.
        _set_subject(cur, rest[0], where="subject_id = %s AND patient_key <> %s",
                     params=(subject, patient_key))
        return patient_key, rest[0]
    _set_subject(cur, patient_key, where="patient_key = %s", params=(patient_key,))
    return patient_key, subject


def move_studies(cur, study_uids: list[str], owner_key: str) -> int:
    """Hand studies (and their series) to another enrollment as owner.

    They take its patient_key and subject_id. Their patient_id is left alone:
    on imaging rows it is the DICOM PatientID and the on-disk folder name. Files
    are not moved: readers follow the stored paths, so the study keeps working
    under its old ``<slug>/<patient_id>`` folder. ``storage_layout.
    misplaced_studies`` lists it; scripts/migration/move_to_dataset_layout.py
    relocates it (maintenance window). Returns the number of studies moved.
    """
    owner = enrollment(cur, owner_key)
    if owner is None:
        raise ValueError(f"unknown enrollment: {owner_key}")
    if not study_uids:
        return 0
    moved = 0
    for table in ("image_study", "image_series"):
        cur.execute(
            f"UPDATE {table} SET patient_key = %s, subject_id = %s "
            "WHERE studyinstanceuid = ANY(%s)",
            (owner["patient_key"], owner["subject_id"], list(study_uids)),
        )
        if table == "image_study":
            moved = cur.rowcount
    return moved


def refresh_subjects(cur, conn, subject_ids) -> None:
    """Re-derive everything that follows a subject's membership or imaging.

    stroke_date of each enrollment (MIN acquisition over the subject's studies),
    per-subject series ranks, episodes/timepoints, and the three labelled
    mirrors for the affected rows. ``conn`` is the cursor's connection (the
    mirror sync takes a connection).
    """
    subject_ids = sorted({s for s in subject_ids if s})
    if not subject_ids:
        return
    cur.execute(
        "UPDATE patient p SET stroke_date = sub.first_study, updated_at = now() "
        "FROM (SELECT q.patient_key, "
        "             (SELECT MIN(st.acquisitiondatetime) FROM image_study st "
        "              WHERE st.subject_id = q.subject_id) AS first_study "
        "      FROM patient q WHERE q.subject_id = ANY(%s)) sub "
        "WHERE p.patient_key = sub.patient_key",
        (subject_ids,),
    )
    cur.execute(ASSIGN_RANKS_SQL)
    cur.execute(CLEAR_RANKS_SQL)
    recompute_subject_timepoints(cur, subject_ids)
    for level, sql in (
        ("patient", "SELECT patient_key FROM patient WHERE subject_id = ANY(%s)"),
        ("study", "SELECT studyinstanceuid FROM image_study WHERE subject_id = ANY(%s)"),
        ("series", "SELECT seriesinstanceuid FROM image_series WHERE subject_id = ANY(%s)"),
    ):
        cur.execute(sql, (subject_ids,))
        ids = [r[0] if not isinstance(r, dict) else next(iter(r.values())) for r in cur.fetchall()]
        if ids:
            sync_labelled_rows(conn, level, ids)
