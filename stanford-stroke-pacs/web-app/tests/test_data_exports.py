"""Data Exports permission boundaries, query semantics and export lifecycle."""

import csv
import io
import secrets
from dataclasses import replace
from uuid import uuid4
from zipfile import ZipFile

import psycopg2
import pytest
from psycopg2 import sql

from data_exports import database
from data_exports.exports import ExportWorker
from data_exports.policy import READER_TABLES, TABLES
from data_exports.query import build_query, validate_sql
from data_exports.settings import Settings
from tests.conftest import TEST_USER, USER_LVO, login_as

ROOT = "/api/data-exports"
ID = "00000000-0000-0000-0000-000000000001"
ENDPOINTS = [
    ("GET", "/capabilities"),
    ("GET", "/catalog"),
    ("GET", "/values"),
    ("POST", "/preview"),
    ("GET", "/reports"),
    ("POST", "/reports"),
    ("PUT", f"/reports/{ID}"),
    ("DELETE", f"/reports/{ID}"),
    ("GET", "/exports"),
    ("POST", "/exports"),
    ("GET", f"/exports/{ID}"),
    ("GET", f"/exports/{ID}/download"),
    ("POST", f"/exports/{ID}/cancel"),
]


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM patient_labelled",
        "SELECT 1; SELECT 2",
        "SELECT * INTO x FROM patient_labelled",
        "SELECT * FROM patient_labelled FOR UPDATE",
        "WITH x AS (DELETE FROM patient_labelled RETURNING *) SELECT * FROM x",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('transaction_read_only','off',true)",
        "SELECT public.count(*) FROM patient_labelled",
        "SELECT * FROM users",
        "SELECT * FROM pg_catalog.pg_roles",
        "SELECT * FROM pg_catalog.patient_labelled",
        "SELECT * FROM patient",
        "SELECT * FROM image_study",
        "SELECT * FROM image_series",
        "SELECT * FROM label_definitions",
        "SELECT * FROM annotations",
        "SELECT * FROM clinical_data",
        "SELECT * FROM data_exports_jobs",
        "COPY patient_labelled TO STDOUT",
        "SELECT 'users'::regclass",
        "SELECT * FROM patient_labelled TABLESAMPLE SYSTEM(1)",
        "WITH x AS (WITH pg_roles AS (SELECT 1) SELECT 1) SELECT * FROM pg_roles",
        "WITH x AS (SELECT * FROM pg_roles), pg_roles AS (SELECT 1) SELECT * FROM x",
        "SELECT * FROM patient_labelled UNION ALL SELECT * FROM users",
        "SELECT pg_sleep(1)",
        "SELECT nextval('x')",
        "SELECT lo_export(1,'/tmp/x')",
        "SELECT 1 OPERATOR(public.+) 2",
        "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_rejects_unsafe_sql(query):
    with pytest.raises(ValueError):
        validate_sql(query, set(TABLES))


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM patient_labelled",
        "SELECT count(*) FROM patient_labelled",
        "SELECT patient_id FROM patient_labelled WHERE patient_id IN ('a','b')",
        "WITH p AS (SELECT * FROM patient_labelled) SELECT * FROM p",
        "SELECT lower(patient_id), row_number() OVER (ORDER BY patient_id) FROM patient_labelled",
        "SELECT dataset FROM patient_labelled WHERE dataset @> ARRAY['a']",
        "SELECT patient_id, count(*) FROM patient_labelled GROUP BY patient_id HAVING count(*) > 0",
        "SELECT CASE WHEN patient_id IS NULL THEN 'empty' ELSE patient_id END FROM patient_labelled",
    ],
)
def test_supported_sql(query):
    assert "ONLY public.patient_labelled" in validate_sql(query, set(TABLES))


def test_builder_binds_literals_and_checks_identifiers():
    cat = [{"name": "patient_labelled", "columns": [{"name": "patient_id", "type": "text"}]}]
    config = {
        "table": "patient_labelled",
        "columns": ["patient_labelled.patient_id"],
        "filters": {"column": "patient_labelled.patient_id", "op": "eq", "value": "';DROP TABLE patient_labelled;--"},
    }
    query, params = build_query(config, cat)
    assert "DROP" not in query
    assert params == ["';DROP TABLE patient_labelled;--"]
    config["columns"] = ["users.password_hash"]
    with pytest.raises(ValueError):
        build_query(config, cat)


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_every_endpoint_denies_anonymous_and_nonadmin(client, method, path):
    assert client.request(method, ROOT + path, json={}).status_code == 401
    login_as(client, USER_LVO)
    assert client.request(method, ROOT + path, json={}).status_code == 403


def test_disabled_module(logged_in_client):
    assert logged_in_client.get(ROOT + "/capabilities").json() == {"enabled": False}
    assert logged_in_client.get(ROOT + "/catalog").status_code == 404
    assert logged_in_client.get(ROOT + "/values", params={"column": "patient_labelled.patient_id"}).status_code == 404


def test_existing_values_search_and_array_filter_roundtrip(data_exports):
    client, _ = data_exports
    response = client.get(ROOT + "/values", params={"column": "patient_labelled.dataset", "operator": "contains"})
    assert response.status_code == 200
    assert response.json() == {"values": ["crisp2", "lvo"], "has_more": False}
    assert set(
        client.get(ROOT + "/values", params={"column": "patient_labelled.dataset", "operator": "eq"}).json()["values"]
    ) == {"{lvo,crisp2}", "{lvo}"}
    config = {
        "builder": {
            "table": "patient_labelled",
            "columns": ["patient_labelled.patient_id"],
            "filters": {"column": "patient_labelled.dataset", "op": "contains", "value": response.json()["values"][0]},
        }
    }
    assert client.post(ROOT + "/preview", json=config).json()["rows"] == [["P-0001"]]
    assert client.get(
        ROOT + "/values", params={"column": "patient_labelled.dataset", "operator": "contains", "search": "CRISP"}
    ).json()["values"] == ["crisp2"]
    for search in ("%", "_", "';DROP TABLE patient_labelled;--"):
        assert (
            client.get(ROOT + "/values", params={"column": "patient_labelled.patient_id", "search": search}).json()[
                "values"
            ]
            == []
        )
    for column in (
        "users.username",
        "label_definitions.name",
        "patient.patient_id",
        "patient_labelled.missing",
        'patient_labelled.patient_id";--',
    ):
        assert client.get(ROOT + "/values", params={"column": column}).status_code == 422


def test_existing_values_limits_empty_strings_and_timestamps(data_exports):
    client, _ = data_exports
    database.records(
        "INSERT INTO patient_labelled (patient_id) SELECT 'choice-' || lpad(n::text, 3, '0') FROM generate_series(1, 110) n"
    )
    database.records("UPDATE patient_labelled SET dataset=ARRAY['', NULL, 'lvo', 'lvo'] WHERE patient_id='P-0001'")
    response = client.get(ROOT + "/values", params={"column": "patient_labelled.patient_id"}).json()
    assert len(response["values"]) == 100
    assert response["has_more"] is True
    assert client.get(
        ROOT + "/values", params={"column": "patient_labelled.patient_id", "search": "choice-110"}
    ).json() == {"values": ["choice-110"], "has_more": False}
    assert client.get(ROOT + "/values", params={"column": "patient_labelled.dataset", "operator": "contains"}).json()[
        "values"
    ] == ["", "lvo"]
    value = client.get(ROOT + "/values", params={"column": "image_study_labelled.acquisitiondatetime"}).json()[
        "values"
    ][0]
    config = {
        "builder": {
            "table": "image_study_labelled",
            "columns": ["image_study_labelled.studyinstanceuid"],
            "filters": {"column": "image_study_labelled.acquisitiondatetime", "op": "eq", "value": value},
        }
    }
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 200
    assert len(response.json()["rows"]) > 0


def test_existing_values_timeout_and_concurrency(data_exports, monkeypatch):
    from data_exports import api

    client, _ = data_exports

    def timeout(*args):
        raise psycopg2.errors.QueryCanceled()

    monkeypatch.setattr(database, "distinct_values", timeout)
    params = {"column": "patient_labelled.patient_id"}
    assert client.get(ROOT + "/values", params=params).status_code == 422
    assert api._preview_slots.acquire(blocking=False)
    assert api._preview_slots.acquire(blocking=False)
    try:
        assert client.get(ROOT + "/values", params=params).status_code == 429
    finally:
        api._preview_slots.release()
        api._preview_slots.release()


def test_nested_boolean_conditions_preview_report_and_export(data_exports):
    client, worker = data_exports

    def condition(value):
        return {"column": "patient_labelled.patient_id", "op": "eq", "value": value}

    filters = {
        "op": "and",
        "rules": [
            {"op": "or", "rules": [condition("P-0001"), condition("P-0002")]},
            {
                "op": "and",
                "negated": True,
                "rules": [
                    condition("P-0002"),
                    {"op": "or", "rules": [{"column": "patient_labelled.dataset", "op": "contains", "value": "lvo"}]},
                ],
            },
        ],
    }
    config = {"builder": {"table": "patient_labelled", "columns": ["patient_labelled.patient_id"], "filters": filters}}
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 200
    assert response.json()["rows"] == [["P-0001"]]
    assert "NOT (" in response.json()["sql"]
    report = client.post(ROOT + "/reports", json={"name": "Nested logic", "configuration": config}).json()
    assert report["configuration"]["builder"]["filters"] == filters
    job = client.post(ROOT + "/exports", json={**config, "name": "Test export", "format": "csv"}).json()
    assert run_job(worker, job)["row_count"] == 1
    filters["negated"] = "false"
    assert client.post(ROOT + "/preview", json=config).status_code == 422


def test_dataset_scope_applies_to_all_tables_values_and_export_history(data_exports):
    client, worker = data_exports
    database.records(
        "INSERT INTO image_series_labelled (patient_id, studyinstanceuid, seriesinstanceuid, series_type) "
        "VALUES ('P-0002', '2.2.2.2.2', '2.2.2.2.2.2', 'CTA')"
    )
    database.records(
        "INSERT INTO series_dicom_tags (seriesinstanceuid) VALUES ('1.2.3.4.5.6'), ('2.2.2.2.2.2'), ('orphan')"
    )
    assert client.get(ROOT + "/catalog").json()["datasets"] == ["crisp2", "lvo"]
    for table, key in [
        ("patient_labelled", "patient_id"),
        ("image_study_labelled", "studyinstanceuid"),
        ("image_series_labelled", "seriesinstanceuid"),
        ("series_dicom_tags", "seriesinstanceuid"),
    ]:
        config = {"dataset": "crisp2", "builder": {"table": table, "columns": [f"{table}.{key}"]}}
        response = client.post(ROOT + "/preview", json=config)
        assert response.status_code == 200, response.text
        assert len(response.json()["rows"]) == 1
        assert "'crisp2'" in response.json()["sql"]
        assert "'crisp2'" not in response.json()["base_sql"]
        values = client.get(ROOT + "/values", params={"column": f"{table}.{key}", "dataset": "crisp2"})
        assert values.status_code == 200
        assert len(values.json()["values"]) == 1
        config["dataset"] = "missing'; SELECT 'injection"
        assert client.post(ROOT + "/preview", json=config).json()["rows"] == []
    assert (
        client.get(
            ROOT + "/values",
            params={"column": "image_series_labelled.series_type", "dataset": "crisp2", "search": "CTA"},
        ).json()["values"]
        == []
    )
    config = {"dataset": "crisp2", "mode": "sql", "sql": "SELECT patient_id FROM patient_labelled"}
    report = client.post(ROOT + "/reports", json={"name": "Scoped export", "configuration": config}).json()
    assert report["configuration"]["dataset"] == "crisp2"
    job = client.post(ROOT + "/exports", json={**config, "name": "Test export", "format": "csv"}).json()
    assert run_job(worker, job)["row_count"] == 1
    assert client.get(ROOT + "/exports", params={"dataset": "lvo"}).json() == []
    assert client.get(ROOT + "/exports", params={"dataset": "crisp2"}).json()[0]["id"] == job["id"]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT patient_id FROM patient_labelled",
        "SELECT public.patient_labelled.patient_id FROM public.patient_labelled",
        "SELECT p.patient_id FROM patient_labelled p",
        "WITH patient_labelled AS (SELECT patient_id FROM public.patient_labelled) SELECT * FROM patient_labelled",
        "SELECT * FROM (SELECT patient_id FROM patient_labelled) p",
        "SELECT p.patient_id FROM patient_labelled p FULL JOIN image_study_labelled s ON p.patient_id=s.patient_id",
        "SELECT patient_id FROM patient_labelled UNION SELECT patient_id FROM image_study_labelled",
        "SELECT patient_id FROM patient_labelled WHERE EXISTS (SELECT 1 FROM image_study_labelled s WHERE s.patient_id=patient_labelled.patient_id)",
    ],
)
def test_sql_dataset_scope_covers_aliases_ctes_subqueries_and_joins(data_exports, query):
    client, _ = data_exports
    response = client.post(ROOT + "/preview", json={"mode": "sql", "sql": query, "dataset": "crisp2"})
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [["P-0001"]]


def test_in_conditions_preserve_values_and_dataset_in_reports_and_exports(data_exports):
    client, worker = data_exports
    config = {
        "dataset": "crisp2",
        "builder": {
            "table": "patient_labelled",
            "columns": ["patient_labelled.patient_id"],
            "filters": {
                "op": "or",
                "rules": [
                    {
                        "column": "patient_labelled.patient_id",
                        "op": "in",
                        "value": ["P-0001", "P-0002", "'; DROP TABLE patient_labelled;--"],
                    }
                ],
            },
        },
    }
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [["P-0001"]]
    report = client.post(ROOT + "/reports", json={"name": "Several choices", "configuration": config}).json()
    assert report["configuration"]["builder"]["filters"] == config["builder"]["filters"]
    job = client.post(ROOT + "/exports", json={**config, "name": "Test export", "format": "csv"}).json()
    assert run_job(worker, job)["row_count"] == 1
    config["dataset"] = None
    assert len(client.post(ROOT + "/preview", json=config).json()["rows"]) == 2
    for invalid in ([], "P-0001", [None], [["P-0001"]], ["P-0001"] * 1001):
        config["builder"]["filters"]["rules"][0]["value"] = invalid
        assert client.post(ROOT + "/preview", json=config).status_code == 422


@pytest.fixture()
def data_exports(logged_in_client, seeded_db, monkeypatch, tmp_path):
    name = "test_data_exports_" + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    admin = psycopg2.connect(**seeded_db)
    admin.autocommit = True
    from labelled_table_sync import rebuild_labelled_tables

    rebuild_labelled_tables(admin)
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(name)), (password,))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name)))
        cur.execute(
            "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' AND relname=ANY(%s)",
            (list(READER_TABLES),),
        )
        for (table,) in cur.fetchall():
            cur.execute(sql.SQL("GRANT SELECT ON public.{} TO {}").format(sql.Identifier(table), sql.Identifier(name)))
    monkeypatch.setenv("DATA_EXPORTS_DB_USER", name)
    monkeypatch.setenv("DATA_EXPORTS_DB_PASSWORD", password)
    settings = Settings(enabled=True, spool_dir=str(tmp_path / "spool"), reserve_bytes=1)
    worker = ExportWorker(settings)
    worker.root.mkdir(mode=0o700)
    (worker.root / ".ssc-data-exports-spool").write_text("SSC Data Exports temporary artifacts\n")
    import app

    monkeypatch.setattr(app.app.state, "data_exports_settings", settings)
    monkeypatch.setattr(app.app.state, "data_exports_worker", worker)
    monkeypatch.setattr(app.app.state, "data_exports_error", None)
    with admin.cursor() as cur:
        cur.execute("TRUNCATE data_exports_downloads, data_exports_jobs, data_exports_reports")
    yield logged_in_client, worker
    worker.stop()
    with admin.cursor() as cur:
        cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
    admin.close()


def submit(client, query="SELECT * FROM patient_labelled ORDER BY patient_id", format="csv"):
    response = client.post(
        ROOT + "/exports", json={"name": "Test export", "mode": "sql", "sql": query, "format": format}
    )
    assert response.status_code == 202, response.text
    return response.json()


def run_job(worker, job):
    database.records("UPDATE data_exports_jobs SET status='running', started_at=now() WHERE id=%s", (job["id"],))
    worker.run(job)
    return database.records("SELECT * FROM data_exports_jobs WHERE id=%s", (job["id"],), one=True)


def test_role_cannot_write_or_read_sensitive_tables(data_exports):
    database.check_role()
    for query in [
        "UPDATE public.patient_labelled SET patient_id=patient_id",
        "SELECT * FROM public.users",
        "SELECT * FROM public.data_exports_jobs",
    ]:
        with database.reader() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg2.Error):
                cur.execute(query)
    with database.reader() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM public.patient_labelled")


def test_catalog_preview_join_and_shared_report(data_exports):
    client, _ = data_exports
    catalog = client.get(ROOT + "/catalog").json()
    names = {t["name"] for t in catalog["tables"]}
    assert "patient_labelled" in names
    assert not names & {"users", "clinical_data", "data_exports_reports", "series_cache_state"}
    config = {
        "mode": "builder",
        "builder": {
            "table": "patient_labelled",
            "joins": ["image_study_labelled", "image_series_labelled"],
            "columns": ["patient_labelled.patient_id", "image_series_labelled.seriesinstanceuid"],
            "filters": {
                "op": "and",
                "rules": [{"column": "patient_labelled.dataset", "op": "contains", "value": "crisp2"}],
            },
            "sort": [{"column": "patient_labelled.patient_id", "direction": "asc"}],
        },
    }
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 200, response.text
    assert response.json()["rows"][0] == ["P-0001", "1.2.3.4.5.6"]
    assert "'crisp2'" in response.json()["sql"]
    report = client.post(ROOT + "/reports", json={"name": "My cohort", "configuration": config}).json()
    assert report["created_by"] == TEST_USER
    assert client.get(ROOT + "/reports").json()[0]["name"] == "My cohort"
    config["builder"]["columns"] = ["patient_labelled.patient_id"]
    assert (
        client.put(ROOT + "/reports/" + report["id"], json={"name": "Renamed", "configuration": config}).status_code
        == 200
    )
    assert client.delete(ROOT + "/reports/" + report["id"]).status_code == 200


def test_direct_patient_series_relationship_without_study(data_exports):
    client, worker = data_exports
    relation = ["patient_labelled", "patient_id", "image_series_labelled", "patient_id"]
    assert relation in client.get(ROOT + "/catalog").json()["relationships"]
    database.records(
        "INSERT INTO image_series_labelled (patient_id, studyinstanceuid, seriesinstanceuid) "
        "VALUES ('P-0001', 'missing-study', 'series-without-study')"
    )
    database.records("INSERT INTO series_dicom_tags (seriesinstanceuid) VALUES ('series-without-study')")
    config = {
        "dataset": "crisp2",
        "builder": {
            "table": "image_series_labelled",
            "joins": ["patient_labelled"],
            "columns": ["image_series_labelled.seriesinstanceuid", "patient_labelled.patient_id"],
            "sort": [{"column": "image_series_labelled.seriesinstanceuid", "direction": "asc"}],
        },
    }
    expected = [["1.2.3.4.5.6", "P-0001"], ["series-without-study", "P-0001"]]
    for base, target in [("image_series_labelled", "patient_labelled"), ("patient_labelled", "image_series_labelled")]:
        config["builder"].update(table=base, joins=[target])
        response = client.post(ROOT + "/preview", json=config)
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == expected
        assert "image_study_labelled" not in response.json()["sql"]
    choices = client.get(
        ROOT + "/values", params={"column": "series_dicom_tags.seriesinstanceuid", "dataset": "crisp2"}
    )
    assert "series-without-study" in choices.json()["values"]
    job = client.post(ROOT + "/exports", json={**config, "name": "Test export", "format": "csv"}).json()
    assert run_job(worker, job)["row_count"] == 2


def test_adding_study_to_direct_patient_series_join_uses_series_study_uid(data_exports):
    client, _ = data_exports
    database.records(
        "INSERT INTO image_study_labelled (patient_id, studyinstanceuid) VALUES ('P-0001', 'unrelated-study')"
    )
    for base, joins in [
        ("image_series_labelled", ["patient_labelled", "image_study_labelled"]),
        ("patient_labelled", ["image_series_labelled", "image_study_labelled"]),
        ("patient_labelled", ["image_study_labelled", "image_series_labelled"]),
    ]:
        config = {
            "builder": {
                "table": base,
                "joins": joins,
                "columns": ["image_series_labelled.seriesinstanceuid", "image_study_labelled.studyinstanceuid"],
                "filters": {"column": "image_series_labelled.seriesinstanceuid", "op": "eq", "value": "1.2.3.4.5.6"},
            }
        }
        response = client.post(ROOT + "/preview", json=config)
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [["1.2.3.4.5.6", "1.2.3.4.5"]]


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_export_fidelity_history_and_download_audit(data_exports, format):
    client, worker = data_exports
    query = "SELECT '00123' AS id, '=2+2' AS formula, NULL AS missing, ARRAY['a','b'] AS tags, 1234567890123456789::numeric AS precise"
    job = submit(client, query, format)
    result = run_job(worker, job)
    assert result["status"] == "completed", result["error"]
    assert result["row_count"] == 1
    response = client.get(ROOT + f"/exports/{job['id']}/download")
    assert response.status_code == 200
    if format == "csv":
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert rows[1] == ["00123", "'=2+2", "\\N", '["a", "b"]', "1234567890123456789"]
    else:
        with ZipFile(io.BytesIO(response.content)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
            assert "00123" in sheet and "=2+2" in sheet and "<f>" not in sheet
            assert "1234567890123456789" in sheet
    detail = client.get(ROOT + f"/exports/{job['id']}").json()
    assert detail["downloads"][0]["username"] == TEST_USER
    assert detail["configuration"]["serialization"]["version"] == 1
    assert detail["equivalent_sql"]


def test_cancel_queue_limits_and_expiration(data_exports):
    client, worker = data_exports
    worker.settings = replace(worker.settings, queue_limit=1)
    job = submit(client)
    assert (
        client.post(ROOT + "/exports", json={"name": "Queue test", "mode": "sql", "sql": "SELECT 1"}).status_code == 429
    )
    assert client.post(ROOT + f"/exports/{job['id']}/cancel").status_code == 200
    assert client.get(ROOT + f"/exports/{job['id']}").json()["status"] == "cancelled"
    job = submit(client)
    run_job(worker, job)
    database.records("UPDATE data_exports_jobs SET expires_at=now()-interval '1 second' WHERE id=%s", (job["id"],))
    worker.cleanup()
    assert not (worker.root / job["id"]).exists()
    assert client.get(ROOT + f"/exports/{job['id']}/download").status_code == 410
    assert client.get(ROOT + f"/exports/{job['id']}").json()["status"] == "expired"


def test_output_directory_failure_marks_export_failed(data_exports, monkeypatch):
    client, worker = data_exports
    job = submit(client)

    def fail_mkdir(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(type(worker.root), "mkdir", fail_mkdir)
    result = run_job(worker, job)
    assert result["status"] == "failed"
    assert result["finished_at"] is not None
    assert not (worker.root / job["id"]).exists()


def test_disk_limit_and_audit_failure(data_exports, monkeypatch):
    client, worker = data_exports
    worker.settings = replace(worker.settings, artifact_bytes=1)
    result = run_job(worker, submit(client))
    assert result["status"] == "failed"
    assert "storage limit" in result["error"]
    assert not (worker.root / str(result["id"])).exists()
    worker.settings = replace(worker.settings, artifact_bytes=1024**3)
    job = submit(client)
    run_job(worker, job)
    real = database.records

    def fail_audit(query, *args, **kwargs):
        if query.startswith("INSERT INTO data_exports_downloads"):
            raise RuntimeError("audit unavailable")
        return real(query, *args, **kwargs)

    monkeypatch.setattr(database, "records", fail_audit)
    assert client.get(ROOT + f"/exports/{job['id']}/download").status_code == 500


def test_restart_recovery_and_single_owner(data_exports):
    client, worker = data_exports
    job = submit(client)
    directory = worker.root / job["id"]
    directory.mkdir()
    (directory / "partial.csv").write_text("partial")
    worker.start()
    try:
        other = ExportWorker(worker.settings)
        with pytest.raises(RuntimeError, match="Only one"):
            other.start()
        assert client.get(ROOT + f"/exports/{job['id']}").json()["status"] == "failed"
        assert not directory.exists()
    finally:
        worker.stop()
        worker.owner = None


def test_excel_multiple_sheets_and_oversized_cell(tmp_path, monkeypatch):
    import data_exports.exports as exports

    monkeypatch.setattr(exports, "SHEET_ROWS", 3)
    path = tmp_path / "test.xlsx"
    ExportWorker.write_excel(path, tmp_path, ["id"], [[("001",), ("002",), ("003",)]])
    with ZipFile(path) as archive:
        assert "xl/worksheets/sheet2.xml" in archive.namelist()
    with pytest.raises(ValueError, match="cell limit"):
        ExportWorker.write_excel(path, tmp_path, ["text"], [[("x" * 32768,)]])


def test_large_export_streams_with_bounded_memory(data_exports):
    import tracemalloc

    client, worker = data_exports
    values = "(VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9))"
    query = "SELECT '000001' AS id FROM " + " CROSS JOIN ".join(f"{values} AS t{i}(n)" for i in range(6))
    job = submit(client, query)
    tracemalloc.start()
    try:
        result = run_job(worker, job)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["status"] == "completed", result["error"]
    assert result["row_count"] == 1_000_000
    assert peak < 30 * 1024**2


def test_polling_does_not_extend_login(data_exports):
    client, _ = data_exports
    response = client.get(ROOT + "/exports", headers={"X-Data-Exports-Poll": "1"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert "set-cookie" in client.get(ROOT + "/exports").headers


def test_preview_invalid_filters_and_literal_percent(data_exports):
    client, worker = data_exports
    assert client.post(ROOT + "/preview", json={"mode": "sql", "sql": "SELECT '100%' AS value"}).json()["rows"] == [
        ["100%"]
    ]
    job = submit(client, "SELECT '100%' AS value")
    assert run_job(worker, job)["status"] == "completed"
    malformed = {
        "mode": "builder",
        "builder": {
            "table": "patient_labelled",
            "columns": ["patient_labelled.patient_id"],
            "filters": {"rules": "not-a-list"},
        },
    }
    assert client.post(ROOT + "/preview", json=malformed).status_code == 422


def test_worker_cancels_running_job_and_rechecks_export_access(data_exports, monkeypatch):
    client, worker = data_exports
    job = submit(client)
    database.records("UPDATE data_exports_jobs SET cancel_requested=true WHERE id=%s", (job["id"],))
    assert run_job(worker, job)["status"] == "cancelled"
    job = submit(client)
    job["username"] = USER_LVO
    result = run_job(worker, job)
    assert result["status"] == "failed"
    assert "staff or admin access" in result["error"]


def test_private_spool_rejects_existing_unrelated_content(tmp_path):
    (tmp_path / "important.txt").write_text("keep")
    worker = ExportWorker(Settings(enabled=True, spool_dir=str(tmp_path)))
    with pytest.raises(RuntimeError, match="empty dedicated"):
        worker.start()
    assert (tmp_path / "important.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "dtype,value,expected",
    [
        ("date", "2026-09-14", "2026-09-14"),
        ("timestamp without time zone", "2026-09-14T14:30", "2026-09-14 14:30:00"),
        ("timestamp(6) without time zone", "2026-09-14 14:30:00.123456", "2026-09-14 14:30:00.123456"),
        ("timestamp with time zone", "2026-09-14T14:30", "2026-09-14T14:30:00+00:00"),
        ("timestamp with time zone", "2026-09-14 14:30:00-07:00", "2026-09-14T21:30:00+00:00"),
    ],
)
def test_temporal_conditions_are_unambiguous(dtype, value, expected):
    config = {
        "table": "image_study_labelled",
        "columns": ["image_study_labelled.acquisitiondatetime"],
        "filters": {"column": "image_study_labelled.acquisitiondatetime", "op": "ge", "value": value},
    }
    cat = [{"name": "image_study_labelled", "columns": [{"name": "acquisitiondatetime", "type": dtype}]}]
    _, params = build_query(config, cat)
    assert params == [expected]


@pytest.mark.parametrize(
    "dtype,value",
    [
        ("date", "09/14/2026"),
        ("date", "2026-02-30"),
        ("timestamp without time zone", "2026-09-14"),
        ("timestamp without time zone", "2026-09-14 14:30:00Z"),
        ("timestamp with time zone", "2026-02-30T14:30:00"),
        ("timestamp with time zone", ""),
    ],
)
def test_temporal_conditions_reject_ambiguous_or_invalid_values(dtype, value):
    from data_exports.query import temporal_value

    with pytest.raises(ValueError, match="date|timestamp|timezone"):
        temporal_value(value, dtype)


def test_catalog_instruments_are_scoped_to_real_labelled_columns(data_exports):
    client, _ = data_exports
    for level, instrument in [("patient", "Intake"), ("study", "Follow-up"), ("series", None)]:
        response = client.post(
            "/api/label-definitions",
            json={
                "name": f"data_exports_instrument_{level}",
                "level": level,
                "datatype": "select" if level == "series" else "text",
                "instrument": instrument,
                "description": f"Description of the {level} label.\nSecond line.",
            },
        )
        assert response.status_code == 201, response.text
    tables = {table["name"]: table for table in client.get(ROOT + "/catalog").json()["tables"]}
    assert set(tables) == {"patient_labelled", "image_study_labelled", "image_series_labelled", "series_dicom_tags"}
    for table, level, instrument in [
        ("patient_labelled", "patient", "Intake"),
        ("image_study_labelled", "study", "Follow-up"),
        ("image_series_labelled", "series", None),
    ]:
        column = next(c for c in tables[table]["columns"] if c["name"] == f"label_data_exports_instrument_{level}")
        assert column["instrument"] == instrument
        assert column["label_level"] == level
        assert column["label_name"] == f"data_exports_instrument_{level}"
        assert column["label_datatype"] == ("select" if level == "series" else "text")
        assert column["label_description"] == f"Description of the {level} label.\nSecond line."
        assert all("instrument" not in c for c in tables[table]["columns"] if not c["name"].startswith("label_"))
    for table in ["patient", "image_study", "image_series", "label_definitions", "annotations"]:
        assert client.post(ROOT + "/preview", json={"mode": "sql", "sql": f"SELECT * FROM {table}"}).status_code == 422
    with database.reader() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("SELECT * FROM public.image_study")


def test_timestamp_preview_and_export_share_validated_value(data_exports):
    client, worker = data_exports
    config = {
        "mode": "builder",
        "builder": {
            "table": "image_study_labelled",
            "columns": ["image_study_labelled.studyinstanceuid"],
            "filters": {"column": "image_study_labelled.acquisitiondatetime", "op": "ge", "value": "2025-01-01T00:00"},
        },
    }
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [["1.2.3.4.5"]]
    job = client.post(ROOT + "/exports", json={**config, "name": "Test export", "format": "csv"}).json()
    assert job["parameters"] == ["2025-01-01 00:00:00"]
    assert run_job(worker, job)["row_count"] == 1
    config["builder"]["filters"]["value"] = "01/01/2025"
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 422
    assert "YYYY-MM-DD HH:mm:ss" in response.json()["detail"]


def test_reader_grant_sync_removes_old_tables_without_rotating_password(data_exports, monkeypatch):
    import os
    import runpy
    from pathlib import Path

    client, _ = data_exports
    role = os.environ["DATA_EXPORTS_DB_USER"]
    script = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "manage_data_exports_db.py"
    commands = runpy.run_path(str(script))
    conn = database.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("COMMENT ON ROLE {} IS %s").format(sql.Identifier(role)), (commands["MARKER"],))
            cur.execute(sql.SQL("GRANT SELECT ON public.image_study TO {}").format(sql.Identifier(role)))
            cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname=%s", (role,))
            password_before = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    commands["provision"](rotate=False)
    conn = database.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rolpassword=%s FROM pg_authid WHERE rolname=%s", (password_before, role))
            assert cur.fetchone()[0] is True
            cur.execute("SELECT has_table_privilege(%s, 'public.image_study', 'SELECT')", (role,))
            assert cur.fetchone()[0] is False
    finally:
        conn.close()
    assert len(client.get(ROOT + "/catalog").json()["tables"]) == 4


@pytest.fixture()
def staff_data_exports(data_exports):
    from tests.conftest import USER_CRISP

    client, worker = data_exports
    database.records("UPDATE users SET is_staff=true WHERE username=%s", (USER_CRISP,))
    login_as(client, USER_CRISP)
    try:
        yield client, worker, USER_CRISP
    finally:
        database.records(
            "UPDATE users SET is_staff=false, allowed_datasets=ARRAY['crisp2'] WHERE username=%s", (USER_CRISP,)
        )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT patient_id FROM patient_labelled ORDER BY patient_id",
        "WITH p AS (SELECT * FROM patient_labelled) SELECT patient_id FROM p",
        "SELECT p.patient_id FROM patient_labelled p JOIN image_study_labelled s USING (patient_id)",
        "SELECT public.patient_labelled.patient_id FROM public.patient_labelled",
        "SELECT patient_id FROM patient_labelled UNION SELECT patient_id FROM image_study_labelled",
        "SELECT patient_id FROM patient_labelled WHERE patient_id IN (SELECT patient_id FROM image_study_labelled)",
    ],
)
def test_staff_queries_scope_every_table(staff_data_exports, query):
    client, _, user = staff_data_exports
    assert client.get("/api/me").json()["is_staff"] is True
    assert client.get("/api/admin/users").status_code == 403
    assert client.get(ROOT + "/catalog").json()["datasets"] == ["crisp2"]
    response = client.post(ROOT + "/preview", json={"mode": "sql", "sql": query, "authorized_datasets": None})
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [["P-0001"]]
    assert client.get(ROOT + "/values", params={"column": "patient_labelled.patient_id"}).json()["values"] == ["P-0001"]
    assert client.post(ROOT + "/preview", json={"mode": "sql", "sql": query, "dataset": "lvo"}).status_code == 403
    assert (
        client.get(ROOT + "/values", params={"column": "patient_labelled.patient_id", "dataset": "lvo"}).status_code
        == 403
    )
    database.records("UPDATE users SET allowed_datasets='{}' WHERE username=%s", (user,))
    assert client.post(ROOT + "/preview", json={"mode": "sql", "sql": query}).json()["rows"] == []


def test_staff_scope_uses_canonical_patient_membership(staff_data_exports):
    client, _, _ = staff_data_exports
    # Simulate a labelled mirror whose cohort data has not yet refreshed.
    database.records("UPDATE patient_labelled SET dataset=ARRAY['crisp2'] WHERE patient_id='P-0002'")
    response = client.post(
        ROOT + "/preview", json={"builder": {"table": "patient_labelled", "columns": ["patient_labelled.patient_id"]}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [["P-0001"]]


def test_staff_reports_and_artifacts_are_private(staff_data_exports):
    client, worker, user = staff_data_exports
    config = {"mode": "sql", "sql": "SELECT patient_id FROM patient_labelled ORDER BY patient_id"}
    report = client.post(ROOT + "/reports", json={"name": "My cohort", "configuration": config}).json()
    job = submit(client, config["sql"])
    assert job["configuration"]["authorized_datasets"] == ["crisp2"]
    assert run_job(worker, job)["status"] == "completed"
    response = client.get(ROOT + f"/exports/{job['id']}/download")
    assert response.status_code == 200
    assert "P-0001" in response.text and "P-0002" not in response.text
    database.records("UPDATE users SET is_staff=true WHERE username=%s", (USER_LVO,))
    try:
        login_as(client, USER_LVO)
        assert client.get(ROOT + "/reports").json() == []
        assert client.get(ROOT + "/exports").json() == []
        for method, suffix in [("GET", ""), ("GET", "/download"), ("POST", "/cancel")]:
            assert client.request(method, ROOT + f"/exports/{job['id']}" + suffix).status_code == 404
        for method in ["PUT", "DELETE"]:
            assert (
                client.request(
                    method, ROOT + f"/reports/{report['id']}", json={"name": "Changed", "configuration": config}
                ).status_code
                == 404
            )
        login_as(client, TEST_USER)
        assert len(client.get(ROOT + "/reports").json()) == 1
        assert client.get(ROOT + f"/exports/{job['id']}/download").status_code == 200
        login_as(client, user)
        database.records("UPDATE users SET allowed_datasets='{}' WHERE username=%s", (user,))
        assert client.get(ROOT + f"/exports/{job['id']}/download").status_code == 403
    finally:
        database.records("UPDATE users SET is_staff=false WHERE username=%s", (USER_LVO,))


@pytest.mark.parametrize("revocation", ["is_staff=false", "allowed_datasets='{}'"])
def test_queued_staff_export_rechecks_permissions(staff_data_exports, revocation):
    client, worker, user = staff_data_exports
    job = submit(client)
    database.records(f"UPDATE users SET {revocation} WHERE username=%s", (user,))
    assert run_job(worker, job)["status"] == "failed"
    assert not (worker.root / job["id"] / "export.csv").exists()


@pytest.mark.parametrize("name", [None, "", "   ", "\t\n", "\u2003", "x" * 121])
def test_export_requires_a_nonblank_name(data_exports, name):
    client, _ = data_exports
    body = {"mode": "sql", "sql": "SELECT 1"}
    if name is not None:
        body["name"] = name
    assert client.post(ROOT + "/exports", json=body).status_code == 422
    assert database.records("SELECT count(*) AS n FROM data_exports_jobs", one=True)["n"] == 0


def test_named_export_edit_preserves_original_and_uses_current_scope(staff_data_exports):
    client, worker, _ = staff_data_exports
    body = {
        "name": "  CTA³ 测试  ",
        "mode": "sql",
        "sql": "SELECT patient_id FROM patient_labelled ORDER BY patient_id",
    }
    original = client.post(ROOT + "/exports", json=body).json()
    assert original["name"] == "CTA³ 测试"
    assert run_job(worker, original)["status"] == "completed"
    snapshot = client.get(ROOT + f"/exports/{original['id']}").json()
    response = client.get(ROOT + f"/exports/{original['id']}/download")
    assert response.status_code == 200
    assert "%E6%B5%8B%E8%AF%95" in response.headers["content-disposition"]
    revised = {
        **body,
        "name": "Revised CTA",
        "sql": "SELECT count(*) AS patients FROM patient_labelled",
        "source_export_id": original["id"],
    }
    response = client.post(ROOT + "/exports", json=revised)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["id"] != original["id"]
    assert job["configuration"]["source_export_id"] == original["id"]
    assert job["configuration"]["authorized_datasets"] == ["crisp2"]
    assert run_job(worker, job)["status"] == "completed"
    unchanged = client.get(ROOT + f"/exports/{original['id']}").json()
    for field in ("name", "sql", "configuration", "created_at", "status"):
        assert unchanged[field] == snapshot[field]
    assert {r["name"] for r in client.get(ROOT + "/exports").json()} == {"CTA³ 测试", "Revised CTA"}
    login_as(client, TEST_USER)
    other = submit(client)
    from tests.conftest import USER_CRISP

    login_as(client, USER_CRISP)
    assert client.post(ROOT + "/exports", json={**revised, "source_export_id": other["id"]}).status_code == 404
    assert client.post(ROOT + "/exports", json={**revised, "source_export_id": str(uuid4())}).status_code == 404


@pytest.mark.parametrize("table", ["patient", "label_definitions"])
def test_reader_requires_internal_metadata_grants(data_exports, table):
    import os

    role = os.environ["DATA_EXPORTS_DB_USER"]
    conn = database.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("REVOKE SELECT ON public.{} FROM {}").format(sql.Identifier(table), sql.Identifier(role))
            )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="metadata grants"):
        database.check_role()


def test_preview_compiles_from_one_catalog_snapshot(data_exports, monkeypatch):
    client, _ = data_exports
    original = database.catalog
    calls = []

    def catalog_once():
        calls.append(True)
        return original()

    monkeypatch.setattr(database, "catalog", catalog_once)
    response = client.post(
        ROOT + "/preview",
        json={
            "dataset": "crisp2",
            "builder": {
                "table": "patient_labelled",
                "columns": ["patient_labelled.patient_id"],
                "filters": {"column": "patient_labelled.patient_id", "op": "eq", "value": "P-0001"},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert calls == [True]
    assert response.json()["rows"] == [["P-0001"]]
    assert "crisp2" not in response.json()["base_sql"]
    assert "crisp2" in response.json()["sql"]
