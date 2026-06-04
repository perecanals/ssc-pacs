#!/usr/bin/env python3
"""Pre-populate Orthanc study labels from image_series and image_study.

Reads study_type (from image_study) and modality (from image_series) and
applies them as labels to the corresponding Orthanc studies via the REST API.

Labels are additive and idempotent -- PUTting an existing label is a no-op.
Safe to re-run at any time (e.g. after new studies are indexed).

Usage:
    python label_studies.py
"""

import os
import sys
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from db import DB_CONFIG as SRC_DB, get_conn  # noqa: E402

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER")
ORTHANC_PASSWORD = os.getenv("ORTHANC_ADMIN_PASSWORD")


def fetch_study_metadata():
    """Return a dict mapping StudyInstanceUID to (study_type, modalities set)."""
    conn = psycopg2.connect(**SRC_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.studyinstanceuid, st.study_type, s.modality "
                "FROM image_series s "
                "LEFT JOIN image_study st ON s.studyinstanceuid = st.studyinstanceuid "
                "WHERE s.studyinstanceuid IS NOT NULL"
            )
            study_meta = {}
            for uid, study_type, modality in cur.fetchall():
                if uid not in study_meta:
                    study_meta[uid] = {"study_type": None, "modalities": set()}
                if study_type:
                    study_meta[uid]["study_type"] = study_type.strip().upper()
                if modality:
                    study_meta[uid]["modalities"].add(modality.strip().upper())
    finally:
        conn.close()
    return study_meta


def resolve_orthanc_id(session, study_instance_uid):
    """Resolve a StudyInstanceUID to an Orthanc internal study ID."""
    resp = session.post(
        f"{ORTHANC_URL}/tools/lookup",
        data=study_instance_uid,
    )
    if resp.status_code != 200:
        return None
    results = resp.json()
    for entry in results:
        if entry.get("Type") == "Study":
            return entry["ID"]
    return None


def apply_label(session, orthanc_id, label):
    """Apply a single label to an Orthanc study. Idempotent."""
    resp = session.put(f"{ORTHANC_URL}/studies/{orthanc_id}/labels/{label}")
    return resp.status_code == 200


def main():
    if not SRC_DB["user"]:
        print("Error: database credentials not set. Check your .env file.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading .env from: {REPO_ROOT / '.env'}")
    print("Fetching study metadata from image_series / image_study ...")
    study_meta = fetch_study_metadata()
    print(f"  {len(study_meta)} unique studies in source table\n")

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASSWORD)

    try:
        resp = session.get(f"{ORTHANC_URL}/system", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Error: cannot reach Orthanc at {ORTHANC_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Connected to Orthanc at {ORTHANC_URL}")
    print("Applying labels ...\n")

    labeled = 0
    not_found = 0
    errors = 0

    for i, (uid, meta) in enumerate(study_meta.items(), 1):
        orthanc_id = resolve_orthanc_id(session, uid)
        if orthanc_id is None:
            not_found += 1
            continue

        labels_to_apply = []
        if meta["study_type"]:
            labels_to_apply.append(meta["study_type"])
        for mod in meta["modalities"]:
            labels_to_apply.append(mod)

        ok = True
        for label in labels_to_apply:
            if not apply_label(session, orthanc_id, label):
                ok = False
                errors += 1

        if ok and labels_to_apply:
            labeled += 1

        if i % 100 == 0 or i == len(study_meta):
            print(f"  [{i}/{len(study_meta)}] labeled={labeled} not_found={not_found} errors={errors}")

    print(f"\nDone. {labeled} studies labeled, {not_found} not found in Orthanc, {errors} errors.")


if __name__ == "__main__":
    main()
