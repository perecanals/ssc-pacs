#!/usr/bin/env python3
"""Enrich Orthanc's PostgreSQL index with patient_id and seriesdescription.

Since the DICOM files may be anonymized, the web viewer can show "Anonymous"
for all patients. This script directly updates Orthanc's index tables so that:
  - Study rows display patient_id as Patient ID
  - Study rows display patient_id as Patient Name
  - Study rows display studydescription (from image_study) as Study Description
  - Series rows display seriesdescription as Series Description

The Folder Indexer will NOT overwrite these changes unless a file's modification
time on disk changes. Safe to re-run -- it is idempotent.

Usage:
    python enrich_orthanc.py
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

SRC_DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)

ORT_DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("PG_ORTHANC_DB", "orthanc_db"),
    user=os.getenv("PG_ORTHANC_USER"),
    password=os.getenv("PG_ORTHANC_PASSWORD"),
)

# DICOM tag codes (decimal)
TAG_PATIENT_NAME = (16, 16)           # (0010,0010)
TAG_PATIENT_ID = (16, 32)             # (0010,0020)
TAG_STUDY_DESCRIPTION = (8, 4144)     # (0008,1030)
TAG_SERIES_DESCRIPTION = (8, 4158)    # (0008,103E)
TAG_STUDY_INSTANCE_UID = (32, 13)     # (0020,000D)
TAG_SERIES_INSTANCE_UID = (32, 14)    # (0020,000E)

RESOURCE_TYPE_STUDY = 1
RESOURCE_TYPE_SERIES = 2


def build_lookup_maps():
    """Build patient_id and seriesdescription lookups from the source DB."""
    conn = psycopg2.connect(**SRC_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT s.studyinstanceuid, s.patient_id, st.studydescription "
                "FROM image_series s "
                "LEFT JOIN image_study st ON s.studyinstanceuid = st.studyinstanceuid "
                "WHERE s.studyinstanceuid IS NOT NULL"
            )
            study_map = {}
            for uid, patient_id, studydesc in cur.fetchall():
                study_map[uid] = (
                    str(patient_id) if patient_id else "",
                    str(studydesc) if studydesc else "",
                )

            cur.execute(
                "SELECT seriesinstanceuid, seriesdescription "
                "FROM image_series "
                "WHERE seriesinstanceuid IS NOT NULL"
            )
            series_map = {}
            for uid, desc in cur.fetchall():
                series_map[uid] = desc if desc else ""
    finally:
        conn.close()
    return study_map, series_map


def upsert_tag(cur, resource_id, tag, value, table):
    """Update a tag value, or insert if it doesn't exist yet."""
    tg, te = tag
    cur.execute(
        f"UPDATE {table} SET value = %s WHERE id = %s AND taggroup = %s AND tagelement = %s",
        (value, resource_id, tg, te),
    )
    if cur.rowcount == 0:
        cur.execute(
            f"INSERT INTO {table} (id, taggroup, tagelement, value) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (resource_id, tg, te, value),
        )


def enrich_studies(ort_conn, study_map):
    """Update study-level Patient Name, Patient ID, and Study Description."""
    cur = ort_conn.cursor()

    cur.execute(
        "SELECT r.internalid, d.value "
        "FROM resources r "
        "JOIN dicomidentifiers d ON r.internalid = d.id "
        "WHERE r.resourcetype = %s AND d.taggroup = %s AND d.tagelement = %s",
        (RESOURCE_TYPE_STUDY, *TAG_STUDY_INSTANCE_UID),
    )
    rows = cur.fetchall()

    updated = 0
    for resource_id, study_uid in rows:
        if study_uid not in study_map:
            continue
        patient_id, studydesc = study_map[study_uid]

        for tag, value in [
            (TAG_PATIENT_NAME, patient_id),
            (TAG_PATIENT_ID, patient_id),
        ]:
            upsert_tag(cur, resource_id, tag, value, "maindicomtags")
            upsert_tag(cur, resource_id, tag, value.upper(), "dicomidentifiers")

        if studydesc:
            upsert_tag(cur, resource_id, TAG_STUDY_DESCRIPTION, studydesc, "maindicomtags")

        updated += 1

    ort_conn.commit()
    cur.close()
    return updated, len(rows)


def enrich_series(ort_conn, series_map):
    """Update series-level Series Description."""
    cur = ort_conn.cursor()

    cur.execute(
        "SELECT r.internalid, d.value "
        "FROM resources r "
        "JOIN dicomidentifiers d ON r.internalid = d.id "
        "WHERE r.resourcetype = %s AND d.taggroup = %s AND d.tagelement = %s",
        (RESOURCE_TYPE_SERIES, *TAG_SERIES_INSTANCE_UID),
    )
    rows = cur.fetchall()

    updated = 0
    for resource_id, series_uid in rows:
        if series_uid not in series_map:
            continue
        desc = series_map[series_uid]
        upsert_tag(cur, resource_id, TAG_SERIES_DESCRIPTION, desc, "maindicomtags")
        updated += 1

    ort_conn.commit()
    cur.close()
    return updated, len(rows)


def main():
    # No flags — but parse argv so `--help` documents instead of running.
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()

    if not SRC_DB["user"] or not ORT_DB["user"]:
        print("Error: database credentials not set. Check your .env file.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading .env from: {ENV_PATH}")
    print("Building lookup maps from image_series / image_study ...")
    study_map, series_map = build_lookup_maps()
    print(f"  {len(study_map)} studies, {len(series_map)} series in source table\n")

    print(f"Connecting to Orthanc DB ({ORT_DB['dbname']}) ...")
    ort_conn = psycopg2.connect(**ORT_DB)

    try:
        print("Enriching studies (Patient Name/ID -> patient_id) ...")
        study_updated, study_total = enrich_studies(ort_conn, study_map)
        print(f"  {study_updated}/{study_total} studies enriched\n")

        print("Enriching series (Series Description -> seriesdescription) ...")
        series_updated, series_total = enrich_series(ort_conn, series_map)
        print(f"  {series_updated}/{series_total} series enriched\n")
    finally:
        ort_conn.close()

    print("Done. Refresh the web viewer to see the changes.")


if __name__ == "__main__":
    main()
