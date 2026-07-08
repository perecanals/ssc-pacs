"""Synthetic 2-patient end-to-end exercise of ImageIngestionProtocol.

Gated: skipped unless SSC_INGEST_AUDIT=1 (needs local Postgres with the .env
credentials, same prerequisite as make test-backend). Creates a scratch
database (ssc_ingest_audit_test) from the ssc-sql-db DDL mirrors, drives the
protocol class directly against scratch dicom/cold roots — no executor, no
Orthanc, no production paths — and drops the database afterwards.

Run with: SSC_INGEST_AUDIT=1 pytest tests/test_end_to_end_scratch_db.py
"""

import os
from pathlib import Path
from urllib.parse import quote_plus

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("SSC_INGEST_AUDIT") != "1",
    reason="integration exercise; set SSC_INGEST_AUDIT=1 (needs local Postgres)",
)

_PKG_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PKG_DIR.parent          # stanford-stroke-pacs/
_DDL_DIR = _REPO_ROOT.parent / "ssc-sql-db"
SCRATCH_DB = "ssc_ingest_audit_test"

STUDY_UIDS = {"11-001": "1.9.1.1", "11-002": "1.9.2.1"}
# Per patient: series A (3 own-folder instances + 1 stray in a mixed folder)
# and series B (1 instance in the mixed folder) -> 4 series, 2 studies total.
SERIES_UIDS = {
    "11-001": {"A": "1.9.1.10", "B": "1.9.1.20"},
    "11-002": {"A": "1.9.2.10", "B": "1.9.2.20"},
}
ACQ_DATE = {"11-001": "20260102", "11-002": "20260205"}


def _env_creds():
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=_REPO_ROOT / ".env")
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": os.getenv("DB_PORT", "5432"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
    }


@pytest.fixture(scope="module")
def scratch_engine():
    import psycopg2
    from sqlalchemy import create_engine, text

    creds = _env_creds()
    admin = psycopg2.connect(dbname="postgres", **{k: v for k, v in creds.items()})
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
        cur.execute(f"CREATE DATABASE {SCRATCH_DB}")
    admin.close()

    engine = create_engine(
        f"postgresql://{quote_plus(creds['user'])}:{quote_plus(creds['password'])}"
        f"@{creds['host']}:{creds['port']}/{SCRATCH_DB}"
    )
    with engine.begin() as conn:
        for ddl in ("create_image_series.sql", "create_image_study.sql",
                    "create_patient.sql"):
            sql = "\n".join(
                line for line in (_DDL_DIR / ddl).read_text().splitlines()
                if not line.startswith("\\")  # strip psql meta-commands
            )
            conn.execute(text(sql))
        conn.execute(text(
            "CREATE TABLE lvo_clinical_data (study_id text, stroke_date date)"))
        # 11-001 clinically matched; 11-002 deliberately unmatched.
        conn.execute(text(
            "INSERT INTO lvo_clinical_data VALUES ('11-001', '2026-01-01')"))

    yield engine

    engine.dispose()
    admin = psycopg2.connect(dbname="postgres", **{k: v for k, v in creds.items()})
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    admin.close()


def _write_case(src_root, patient_id):
    import pydicom
    from test_image_ingestion_grouping import _write_dcm

    case = src_root / patient_id
    study = STUDY_UIDS[patient_id]
    uids = SERIES_UIDS[patient_id]
    specs = [
        (case / "series_a" / "a1.dcm", uids["A"], 100, 1, "SERIES_A"),
        (case / "series_a" / "a2.dcm", uids["A"], 100, 2, "SERIES_A"),
        (case / "series_a" / "a3.dcm", uids["A"], 100, 3, "SERIES_A"),
        (case / "mixed" / "a_stray.dcm", uids["A"], 100, 4, "SERIES_A"),
        (case / "mixed" / "b1.dcm", uids["B"], 200, 1, "SERIES_B"),
    ]
    for path, series_uid, number, instance, desc in specs:
        _write_dcm(path, series_uid, number, instance, study_uid=study,
                   series_desc=desc, patient_id=patient_id)
        # Stamp an acquisition date so patient.stroke_date is derivable.
        ds = pydicom.dcmread(str(path))
        ds.AcquisitionDate = ACQ_DATE[patient_id]
        ds.AcquisitionTime = "101500"
        ds.save_as(str(path))
    return case


def _manifest(root):
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size)
        for p in Path(root).rglob("*") if p.is_file()
    )


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    scratch = tmp_path_factory.mktemp("ingest_audit")
    (scratch / "src").mkdir()
    (scratch / "dicom_root").mkdir()
    (scratch / "cold_root").mkdir()
    return scratch


def _run_case(scratch, engine, patient_id, import_id=1):
    from image_ingestion_protocol import ImageIngestionProtocol

    proto = ImageIngestionProtocol(
        str(scratch / "src" / patient_id), engine,
        import_id=import_id, import_label="audit_batch", dataset="audit",
        cold_archive_root=str(scratch / "cold_root"), compress_workers=2,
    )
    proto.base_dir = str(scratch / "dicom_root")
    return proto.execute_image_ingestion_protocol()


def test_end_to_end_two_patients(roots, scratch_engine, capsys):
    from sqlalchemy import text

    from image_ingestion_protocol import ImageIngestionProtocol

    for pid in ("11-001", "11-002"):
        _write_case(roots / "src", pid)
    src_manifest = _manifest(roots / "src")

    results = {pid: _run_case(roots, scratch_engine, pid)
               for pid in ("11-001", "11-002")}
    out = capsys.readouterr().out

    for pid, res in results.items():
        assert res["studyinstanceuids"] == [STUDY_UIDS[pid]]
        assert res["seriesinstanceuids"] == sorted(SERIES_UIDS[pid].values())
        assert res["skipped_existing_seriesinstanceuids"] == []
    # 11-002 has no lvo_clinical_data row -> warned, still ingested.
    assert "11-002 is not present in lvo_clinical_data" in out

    with scratch_engine.begin() as conn:
        series = conn.execute(text(
            "SELECT seriesinstanceuid, dicom_dir_path, dicom_archive_path, "
            "number_of_slices, compressed_size_mb, decompressed_size_mb "
            "FROM image_series ORDER BY seriesinstanceuid")).mappings().all()
        studies = conn.execute(text(
            "SELECT studyinstanceuid, compressed_size_mb, decompressed_size_mb "
            "FROM image_study ORDER BY studyinstanceuid")).mappings().all()
        patients = conn.execute(text(
            "SELECT patient_id, stroke_date, dataset, import_label "
            "FROM patient ORDER BY patient_id")).mappings().all()

    assert len(series) == 4
    for row in series:
        assert row["dicom_dir_path"].startswith(str(roots / "dicom_root"))
        assert row["dicom_archive_path"].startswith(str(roots / "cold_root"))
        ImageIngestionProtocol._verify_archive(
            row["dicom_archive_path"], row["number_of_slices"])
        # Sizes recorded (synthetic files are tiny -> may round to 0.0 MB).
        assert row["compressed_size_mb"] is not None
        assert row["decompressed_size_mb"] is not None
    by_uid = {r["seriesinstanceuid"]: r for r in series}
    for pid in ("11-001", "11-002"):
        assert by_uid[SERIES_UIDS[pid]["A"]]["number_of_slices"] == 4  # merged
        assert by_uid[SERIES_UIDS[pid]["B"]]["number_of_slices"] == 1

    assert len(studies) == 2
    for row in studies:
        # Rollup stamped non-NULL only when every child series has sizes.
        assert row["compressed_size_mb"] is not None
        assert row["decompressed_size_mb"] is not None

    assert [p["patient_id"] for p in patients] == ["11-001", "11-002"]
    for p in patients:
        # stroke_date = MIN(image_study.acquisitiondatetime), imaging-derived.
        assert p["stroke_date"].strftime("%Y%m%d") == ACQ_DATE[p["patient_id"]]
        assert p["dataset"] == ["audit"]
        assert p["import_label"] == "audit_batch"

    # Source tree untouched.
    assert _manifest(roots / "src") == src_manifest


def test_idempotent_rerun_skips_everything(roots, scratch_engine):
    archives = sorted((roots / "cold_root").rglob("*.tar.zst"))
    mtimes_before = [a.stat().st_mtime_ns for a in archives]

    for pid in ("11-001", "11-002"):
        res = _run_case(roots, scratch_engine, pid)
        assert res["seriesinstanceuids"] == []
        assert res["skipped_existing_seriesinstanceuids"] == sorted(
            SERIES_UIDS[pid].values())

    assert [a.stat().st_mtime_ns for a in archives] == mtimes_before


def test_drift_series_reingested_alone(roots, scratch_engine):
    from sqlalchemy import text
    from test_image_ingestion_grouping import _write_dcm

    # One extra instance appears in 11-001's series A source -> drift.
    pid = "11-001"
    _write_dcm(roots / "src" / pid / "series_a" / "a5.dcm",
               SERIES_UIDS[pid]["A"], 100, 5, study_uid=STUDY_UIDS[pid],
               series_desc="SERIES_A", patient_id=pid)

    res = _run_case(roots, scratch_engine, pid)
    assert res["seriesinstanceuids"] == [SERIES_UIDS[pid]["A"]]
    assert res["skipped_existing_seriesinstanceuids"] == [SERIES_UIDS[pid]["B"]]

    with scratch_engine.begin() as conn:
        n = conn.execute(
            text("SELECT number_of_slices FROM image_series "
                 "WHERE seriesinstanceuid = :uid"),
            {"uid": SERIES_UIDS[pid]["A"]},
        ).scalar()
    assert n == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
