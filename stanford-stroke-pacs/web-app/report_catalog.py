"""Catalogue Orthanc-owned SR metadata without copying DICOM files."""
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from fastapi import HTTPException

from db import get_conn
from labelled_table_sync import sync_labelled_rows
from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER
from study_metadata import lock_study_rows, refresh_study_modalities

logger = logging.getLogger(__name__)


def orthanc_json(method, path, **kwargs):
    response = requests.request(method, ORTHANC_URL + path,
                                auth=(ORTHANC_USER, ORTHANC_PASS), timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def require_filesystem_series(uid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT storage_source FROM image_series WHERE seriesinstanceuid = %s", (uid,))
            row = cur.fetchone()
            if row and row[0] == "orthanc":
                raise HTTPException(409, "This report is stored in Orthanc; filesystem operations are unavailable")
    finally:
        conn.close()


def catalog_reports(study_uid=None, *, execute=False):
    query = {"Modality": "SR"}
    if study_uid:
        query["StudyInstanceUID"] = study_uid
    reports = orthanc_json("POST", "/tools/find", json={"Level": "Series", "Query": query, "Expand": True})
    results = []
    conn = get_conn()
    try:
        for report in sorted(reports, key=lambda r: (r["ParentStudy"], r["MainDicomTags"]["SeriesInstanceUID"])):
            tags = report["MainDicomTags"]
            uid = tags["SeriesInstanceUID"]
            parent = orthanc_json("GET", "/studies/" + report["ParentStudy"])
            study = parent["MainDicomTags"]["StudyInstanceUID"]
            with conn.cursor() as cur:
                lock_study_rows(cur, [study])
                cur.execute("SELECT patient_id, patient_key, subject_id FROM image_study WHERE studyinstanceuid = %s", (study,))
                owner = cur.fetchone()
                cur.execute("SELECT storage_source, studyinstanceuid FROM image_series WHERE seriesinstanceuid = %s", (uid,))
                existing = cur.fetchone()
                if not owner or (existing and existing != ("orthanc", study)):
                    results.append({"series": uid, "status": "skipped"})
                    continue
                results.append({"series": uid, "status": "catalogued" if execute else "would catalogue"})
                if not execute:
                    continue
                # Recheck under the parent lock: an admin may have deleted it
                # after discovery. Never resurrect its application row.
                try:
                    report = orthanc_json("GET", "/series/" + report["ID"])
                except requests.HTTPError as exc:
                    if exc.response.status_code == 404:
                        results[-1]["status"] = "skipped"
                        continue
                    raise
                date = None
                try:
                    date = datetime.strptime(tags.get("SeriesDate", "") + tags.get("SeriesTime", "").split(".")[0].ljust(6, "0"), "%Y%m%d%H%M%S")
                except ValueError:
                    pass
                cur.execute("""INSERT INTO image_series
                    (seriesinstanceuid, studyinstanceuid, patient_id, patient_key, subject_id,
                     modality, seriesdescription, number_of_slices, acquisitiondatetime, storage_source)
                    VALUES (%s,%s,%s,%s,%s,'SR',%s,%s,%s,'orthanc')
                    ON CONFLICT (seriesinstanceuid) DO UPDATE SET
                    seriesdescription=EXCLUDED.seriesdescription, number_of_slices=EXCLUDED.number_of_slices,
                    acquisitiondatetime=EXCLUDED.acquisitiondatetime
                    WHERE image_series.storage_source='orthanc'
                    AND image_series.studyinstanceuid=EXCLUDED.studyinstanceuid""",
                    (uid, study, *owner, tags.get("SeriesDescription") or "Structured report", len(report["Instances"]), date))
                refresh_study_modalities(cur, [study])
            if execute:
                sync_labelled_rows(conn, "series", [uid])
                sync_labelled_rows(conn, "study", [study])
        if execute:
            conn.commit()
        return results
    finally:
        conn.close()


def successful_sops(body):
    """Read only Referenced SOP Sequence from JSON or Native DICOM XML."""
    if body.lstrip().startswith(b"<" if isinstance(body, bytes) else "<"):
        root = ET.fromstring(body)
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        return [value.text for value in root.findall(
            "./DicomAttribute[@tag='00081199']/Item/"
            "DicomAttribute[@tag='00081155']/Value") if value.text]
    payload = json.loads(body)
    return [item["00081155"]["Value"][0]
            for item in payload.get("00081199", {}).get("Value", [])]


def catalog_stow_response(body):
    """Use successful SOP references, never assume HTTP success means all stored."""
    try:
        studies = set()
        for uid in successful_sops(body):
            matches = orthanc_json("POST", "/tools/lookup", data=uid)
            for match in matches:
                if match.get("Type") == "Instance":
                    instance = orthanc_json("GET", "/instances/" + match["ID"])
                    series = orthanc_json("GET", "/series/" + instance["ParentSeries"])
                    study = orthanc_json("GET", "/studies/" + series["ParentStudy"])
                    studies.add(study["MainDicomTags"]["StudyInstanceUID"])
        for study in studies:
            catalog_reports(study, execute=True)
    except Exception:
        logger.exception("SR cataloguing failed; recover with scripts/admin/sync_ohif_reports.py --execute")
