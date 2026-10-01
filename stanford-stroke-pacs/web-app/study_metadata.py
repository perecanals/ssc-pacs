"""Study modality rollups shared by ingestion and administrative deletion.

Call lock_study_rows before changing child series, then refresh in a separate
statement in the same READ COMMITTED transaction. The separate statement sees
any concurrent writer that committed while waiting for the parent lock.
"""

from __future__ import annotations


def _study_ids(study_uids):
    return sorted({str(uid) for uid in study_uids if uid is not None and str(uid)})


def lock_study_rows(cursor, study_uids):
    """Serialize series mutations by locking existing parents in UID order."""
    uids = _study_ids(study_uids)
    if uids:
        cursor.execute(
            "SELECT studyinstanceuid FROM image_study "
            "WHERE studyinstanceuid = ANY(%s) ORDER BY studyinstanceuid FOR UPDATE",
            (uids,),
        )
        cursor.fetchall()


def refresh_study_modalities(cursor, study_uids):
    """Recompute from all persisted children, including clearing empty studies."""
    uids = _study_ids(study_uids)
    if not uids:
        return
    cursor.execute(
        """
        UPDATE image_study st SET modalities = agg.modalities
        FROM (
            SELECT parent.studyinstanceuid,
                   array_agg(DISTINCT UPPER(BTRIM(s.modality))
                             ORDER BY UPPER(BTRIM(s.modality)))
                       FILTER (WHERE NULLIF(BTRIM(s.modality), '') IS NOT NULL) AS modalities
            FROM image_study parent
            LEFT JOIN image_series s ON s.studyinstanceuid = parent.studyinstanceuid
            WHERE parent.studyinstanceuid = ANY(%s)
            GROUP BY parent.studyinstanceuid
        ) agg
        WHERE st.studyinstanceuid = agg.studyinstanceuid
          AND st.modalities IS DISTINCT FROM agg.modalities
        """,
        (uids,),
    )


def lock_study_rows_sqlalchemy(connection, study_uids):
    """Use the SQLAlchemy transaction's DBAPI connection, without committing."""
    with connection.connection.cursor() as cursor:
        lock_study_rows(cursor, study_uids)


def refresh_study_modalities_sqlalchemy(connection, study_uids):
    with connection.connection.cursor() as cursor:
        refresh_study_modalities(cursor, study_uids)
