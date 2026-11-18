"""Authenticated SR provenance. Unknown or incomplete ownership grants no rights.

Parent study locks serialize web-app saves with deletion. Pending journal rows
are committed separately before forwarding, so an uncertain upload never loses
its attribution attempt or silently grants deletion permission.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from common import ensure_series_access
from db import get_conn
from study_metadata import lock_study_rows

logger = logging.getLogger(__name__)


def orthanc_json(*args, **kwargs):
    from report_catalog import orthanc_json as request_json
    return request_json(*args, **kwargs)


def series_instances(uid, study=None):
    matches = orthanc_json("POST", "/tools/lookup", data=uid)
    series = [m for m in matches if m.get("Type") == "Series"]
    if not series:
        return []
    if len(series) != 1:
        raise HTTPException(409, "Ambiguous report identity")
    resource = orthanc_json("GET", "/series/" + series[0]["ID"])
    parent = orthanc_json("GET", "/studies/" + resource["ParentStudy"])
    if (resource["MainDicomTags"].get("SeriesInstanceUID") != uid
            or (study and parent["MainDicomTags"].get("StudyInstanceUID") != study)
            or resource["MainDicomTags"].get("Modality") != "SR"):
        raise HTTPException(409, "Not an SR series")
    return [orthanc_json("GET", "/instances/" + iid)["MainDicomTags"]["SOPInstanceUID"]
            for iid in resource["Instances"]]


def owned_series(cur, uid, username, account_created_at, study=None):
    """Verify every actual Orthanc instance, not just catalogue metadata."""
    sops = series_instances(uid, study)
    if not sops:
        return False
    cur.execute("""SELECT sop_uid FROM sr_provenance WHERE series_uid=%s
        AND sop_uid=ANY(%s) AND saved_by=%s AND account_created_at=%s AND confirmed""",
                (uid, sops, username, account_created_at))
    return {r[0] for r in cur.fetchall()} == set(sops)


def authorize_series_delete(conn, uid, username, *, lock=False):
    with conn.cursor() as cur:
        cur.execute("SELECT is_admin, is_staff, allowed_datasets, created_at FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user or not (user[0] or user[1]):
            raise HTTPException(403, "Staff or admin access required")
        if user[0]:
            return
        ensure_series_access(cur, uid, list(user[2] or []))
        cur.execute("SELECT studyinstanceuid FROM image_series WHERE seriesinstanceuid=%s", (uid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Series not found")
        if lock:
            lock_study_rows(cur, [row[0]])
            ensure_series_access(cur, uid, list(user[2] or []))
        cur.execute("SELECT storage_source, modality FROM image_series WHERE seriesinstanceuid=%s", (uid,))
        kind = cur.fetchone()
        if (kind != ("orthanc", "SR")
                or not owned_series(cur, uid, username, user[3], row[0])):
            raise HTTPException(403, "Only your own fully attributed SR series may be deleted")


def begin_upload(instances, username):
    """Return a connection holding all parent locks until upload completion."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for key in sorted({"series:" + i["series"] for i in instances}
                              | {"sop:" + i["sop"] for i in instances}):
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
            lock_study_rows(cur, [i["study"] for i in instances])
            cur.execute("SELECT is_admin, created_at FROM users WHERE username=%s", (username,))
            user = cur.fetchone()
            if not user:
                raise HTTPException(403, "Unknown user")
            pending = []
            for item in instances:
                if not item["sr"]:
                    cur.execute("SELECT 1 FROM sr_provenance WHERE sop_uid=%s OR series_uid=%s LIMIT 1",
                                (item["sop"], item["series"]))
                    if cur.fetchone():
                        raise HTTPException(409, "SEG cannot replace an SR")
                    for match in orthanc_json("POST", "/tools/lookup", data=item["series"]):
                        if match.get("Type") == "Series":
                            resource = orthanc_json("GET", "/series/" + match["ID"])
                            if resource["MainDicomTags"].get("Modality") == "SR":
                                raise HTTPException(409, "SEG cannot replace an SR")
                    for match in orthanc_json("POST", "/tools/lookup", data=item["sop"]):
                        if match.get("Type") == "Instance":
                            resource = orthanc_json("GET", "/instances/" + match["ID"])
                            parent = orthanc_json("GET", "/series/" + resource["ParentSeries"])
                            if parent["MainDicomTags"].get("Modality") == "SR":
                                raise HTTPException(409, "SEG cannot replace an SR")
                    continue
                existing = series_instances(item["series"], item["study"])
                cur.execute("SELECT storage_source, studyinstanceuid FROM image_series WHERE seriesinstanceuid=%s", (item["series"],))
                catalogued = cur.fetchone()
                if catalogued and catalogued != ("orthanc", item["study"]):
                    raise HTTPException(409, "Series identity conflict")
                if not user[0] and existing and not owned_series(cur, item["series"], username, user[1], item["study"]):
                    raise HTTPException(403, "Cannot modify another or unknown owner's report")
                cur.execute("SELECT study_uid, series_uid, saved_by, account_created_at, confirmed FROM sr_provenance WHERE sop_uid=%s", (item["sop"],))
                previous = cur.fetchone()
                matches = orthanc_json("POST", "/tools/lookup", data=item["sop"])
                if previous:
                    if previous[:2] != (item["study"], item["series"]):
                        raise HTTPException(409, "Instance identity conflict")
                    if not user[0] and (previous[2:4] != (username, user[1]) or not previous[4]):
                        raise HTTPException(403, "Report ownership is unknown or belongs to another user")
                elif not matches:
                    pending.append(item)
                elif not user[0]:
                    raise HTTPException(403, "Existing report has unknown ownership")
            journal = get_conn()
            try:
                with journal.cursor() as jc:
                    for item in pending:
                        jc.execute("""INSERT INTO sr_provenance
                            (sop_uid, study_uid, series_uid, saved_by, account_created_at)
                            VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                                   (item["sop"], item["study"], item["series"], username, user[1]))
                journal.commit()
            finally:
                journal.close()
        return conn
    except BaseException:
        conn.close()
        raise


def confirm_upload(instances, username, successful):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for item in instances:
                if item["sr"] and item["sop"] in successful:
                    cur.execute("""UPDATE sr_provenance SET confirmed=true
                        WHERE sop_uid=%s AND study_uid=%s AND series_uid=%s AND saved_by=%s
                        AND account_created_at=(SELECT created_at FROM users WHERE username=%s)""",
                                (item["sop"], item["study"], item["series"], username, username))
        conn.commit()
    finally:
        conn.close()


def attach_sr_permissions(cursor, rows, username):
    with cursor.connection.cursor() as cur:
        _attach_sr_permissions(cur, rows, username)


def _attach_sr_permissions(cur, rows, username):
    cur.execute("SELECT is_admin, is_staff, created_at FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    for row in rows:
        row["can_delete"] = bool(user and user[0])
        row["sr_ownership"] = "unknown"
        row["sr_saved_by"] = None
        row["sr_saved_at"] = None
        if row.get("storage_source") != "orthanc" or row.get("modality") != "SR":
            continue
        uid = row["seriesinstanceuid"]
        try:
            sops = series_instances(uid, row["studyinstanceuid"])
            cur.execute("SELECT sop_uid, saved_by, account_created_at, saved_at FROM sr_provenance WHERE series_uid=%s AND confirmed", (uid,))
            records = cur.fetchall()
            records = [r for r in records if r[0] in sops]
            owners = {(r[1], r[2]) for r in records}
            complete = bool(sops) and {r[0] for r in records} == set(sops)
            if complete and len(owners) == 1:
                owner, created = next(iter(owners))
                row["sr_saved_by"] = owner
                row["sr_saved_at"] = min(r[3] for r in records).isoformat()
                row["sr_ownership"] = "confirmed"
                if user and user[1] and (owner, created) == (username, user[2]):
                    row["can_delete"] = True
            elif len(owners) > 1:
                row["sr_ownership"] = "mixed"
        except Exception:
            logger.warning("Cannot verify SR ownership for %s", uid, exc_info=True)


def refresh_series_summary(conn, uid):
    with conn.cursor() as cur:
        cur.execute("SELECT studyinstanceuid, storage_source, modality FROM image_series WHERE seriesinstanceuid=%s", (uid,))
        row = cur.fetchone()
        if not row:
            return
        result = {"seriesinstanceuid": uid, "studyinstanceuid": row[0],
                  "storage_source": row[1], "modality": row[2]}
        _attach_sr_permissions(cur, [result], "")
        cur.execute("UPDATE image_series SET sr_saved_by=%s, sr_saved_at=%s WHERE seriesinstanceuid=%s",
                    (result["sr_saved_by"], result["sr_saved_at"], uid))
