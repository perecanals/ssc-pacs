"""Data Explorer permission boundaries, query semantics and export lifecycle."""
import csv
import io
import secrets
from dataclasses import replace
from uuid import uuid4
from zipfile import ZipFile

import psycopg2
import pytest
from psycopg2 import sql

from data_explorer import database
from data_explorer.exports import ExportWorker
from data_explorer.policy import TABLES
from data_explorer.query import build_query, validate_sql
from data_explorer.settings import Settings
from tests.conftest import TEST_USER, USER_LVO, login_as

ROOT = "/api/data-explorer"
ID = "00000000-0000-0000-0000-000000000001"
ENDPOINTS = [("GET", "/capabilities"), ("GET", "/catalog"), ("POST", "/preview"),
             ("GET", "/reports"), ("POST", "/reports"), ("PUT", f"/reports/{ID}"),
             ("DELETE", f"/reports/{ID}"), ("GET", "/exports"), ("POST", "/exports"),
             ("GET", f"/exports/{ID}"), ("GET", f"/exports/{ID}/download"),
             ("POST", f"/exports/{ID}/cancel")]


@pytest.mark.parametrize("query", [
    "DELETE FROM patient", "SELECT 1; SELECT 2", "SELECT * INTO x FROM patient",
    "SELECT * FROM patient FOR UPDATE", "WITH x AS (DELETE FROM patient RETURNING *) SELECT * FROM x",
    "SELECT pg_read_file('/etc/passwd')", "SELECT set_config('transaction_read_only','off',true)",
    "SELECT public.count(*) FROM patient", "SELECT * FROM users", "SELECT * FROM pg_catalog.pg_roles",
    "SELECT * FROM pg_catalog.patient", "SELECT * FROM clinical_data", "SELECT * FROM explorer_exports", "COPY patient TO STDOUT",
    "SELECT 'users'::regclass", "SELECT * FROM patient TABLESAMPLE SYSTEM(1)",
    "WITH x AS (WITH pg_roles AS (SELECT 1) SELECT 1) SELECT * FROM pg_roles",
    "WITH x AS (SELECT * FROM pg_roles), pg_roles AS (SELECT 1) SELECT * FROM x",
    "SELECT * FROM patient UNION ALL SELECT * FROM users", "SELECT pg_sleep(1)",
    "SELECT nextval('x')", "SELECT lo_export(1,'/tmp/x')", "SELECT 1 OPERATOR(public.+) 2",
    "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
])
def test_rejects_unsafe_sql(query):
    with pytest.raises(ValueError):
        validate_sql(query, set(TABLES))


@pytest.mark.parametrize("query", [
    "SELECT * FROM patient", "SELECT count(*) FROM patient",
    "SELECT patient_id FROM patient WHERE patient_id IN ('a','b')",
    "WITH p AS (SELECT * FROM patient) SELECT * FROM p",
    "SELECT lower(patient_id), row_number() OVER (ORDER BY patient_id) FROM patient",
    "SELECT dataset FROM patient WHERE dataset @> ARRAY['a']",
    "SELECT patient_id, count(*) FROM patient GROUP BY patient_id HAVING count(*) > 0",
    "SELECT CASE WHEN patient_id IS NULL THEN 'empty' ELSE patient_id END FROM patient",
])
def test_supported_sql(query):
    assert "ONLY public.patient" in validate_sql(query, set(TABLES))


def test_builder_binds_literals_and_checks_identifiers():
    cat = [{"name": "patient", "columns": [{"name": "patient_id", "type": "text"}]}]
    config = {"table": "patient", "columns": ["patient.patient_id"],
              "filters": {"column": "patient.patient_id", "op": "eq", "value": "';DROP TABLE patient;--"}}
    query, params = build_query(config, cat)
    assert "DROP" not in query
    assert params == ["';DROP TABLE patient;--"]
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


@pytest.fixture()
def explorer(logged_in_client, seeded_db, monkeypatch, tmp_path):
    name = "test_explorer_" + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    admin = psycopg2.connect(**seeded_db)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(name)), (password,))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name)))
        cur.execute("SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' AND relname=ANY(%s)", (list(TABLES),))
        for (table,) in cur.fetchall():
            cur.execute(sql.SQL("GRANT SELECT ON public.{} TO {}").format(sql.Identifier(table), sql.Identifier(name)))
    monkeypatch.setenv("EXPLORER_DB_USER", name)
    monkeypatch.setenv("EXPLORER_DB_PASSWORD", password)
    settings = Settings(enabled=True, spool_dir=str(tmp_path / "spool"), reserve_bytes=1)
    worker = ExportWorker(settings)
    worker.root.mkdir(mode=0o700)
    (worker.root / ".ssc-explorer-spool").write_text("SSC Data Explorer temporary artifacts\n")
    import app
    monkeypatch.setattr(app.app.state, "explorer_settings", settings)
    monkeypatch.setattr(app.app.state, "explorer_worker", worker)
    monkeypatch.setattr(app.app.state, "explorer_error", None)
    with admin.cursor() as cur:
        cur.execute("TRUNCATE explorer_downloads, explorer_exports, explorer_reports")
    yield logged_in_client, worker
    worker.stop()
    with admin.cursor() as cur:
        cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
        cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
    admin.close()


def submit(client, query="SELECT * FROM patient ORDER BY patient_id", format="csv"):
    response = client.post(ROOT + "/exports", json={"mode": "sql", "sql": query, "format": format})
    assert response.status_code == 202, response.text
    return response.json()


def run_job(worker, job):
    database.records("UPDATE explorer_exports SET status='running', started_at=now() WHERE id=%s", (job["id"],))
    worker.run(job)
    return database.records("SELECT * FROM explorer_exports WHERE id=%s", (job["id"],), one=True)


def test_role_cannot_write_or_read_sensitive_tables(explorer):
    database.check_role()
    for query in ["UPDATE public.patient SET patient_id=patient_id", "SELECT * FROM public.users", "SELECT * FROM public.explorer_exports"]:
        with database.reader() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg2.Error):
                cur.execute(query)
    with database.reader() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM public.patient")


def test_catalog_preview_join_and_shared_report(explorer):
    client, _ = explorer
    catalog = client.get(ROOT + "/catalog").json()
    names = {t["name"] for t in catalog["tables"]}
    assert "patient" in names
    assert not names & {"users", "clinical_data", "explorer_reports", "series_cache_state"}
    config = {"mode": "builder", "builder": {"table": "patient", "joins": ["image_study", "image_series"],
        "columns": ["patient.patient_id", "image_series.seriesinstanceuid"],
        "filters": {"op": "and", "rules": [{"column": "patient.dataset", "op": "contains", "value": "crisp2"}]},
        "sort": [{"column": "patient.patient_id", "direction": "asc"}]}}
    response = client.post(ROOT + "/preview", json=config)
    assert response.status_code == 200, response.text
    assert response.json()["rows"][0] == ["P-0001", "1.2.3.4.5.6"]
    assert "'crisp2'" in response.json()["sql"]
    report = client.post(ROOT + "/reports", json={"name": "My cohort", "configuration": config}).json()
    assert report["created_by"] == TEST_USER
    assert client.get(ROOT + "/reports").json()[0]["name"] == "My cohort"
    config["builder"]["columns"] = ["patient.patient_id"]
    assert client.put(ROOT + "/reports/" + report["id"], json={"name": "Renamed", "configuration": config}).status_code == 200
    assert client.delete(ROOT + "/reports/" + report["id"]).status_code == 200


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_export_fidelity_history_and_download_audit(explorer, format):
    client, worker = explorer
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


def test_cancel_queue_limits_and_expiration(explorer):
    client, worker = explorer
    worker.settings = replace(worker.settings, queue_limit=1)
    job = submit(client)
    assert client.post(ROOT + "/exports", json={"mode": "sql", "sql": "SELECT 1"}).status_code == 429
    assert client.post(ROOT + f"/exports/{job['id']}/cancel").status_code == 200
    assert client.get(ROOT + f"/exports/{job['id']}").json()["status"] == "cancelled"
    job = submit(client)
    run_job(worker, job)
    database.records("UPDATE explorer_exports SET expires_at=now()-interval '1 second' WHERE id=%s", (job["id"],))
    worker.cleanup()
    assert not (worker.root / job["id"]).exists()
    assert client.get(ROOT + f"/exports/{job['id']}/download").status_code == 410
    assert client.get(ROOT + f"/exports/{job['id']}").json()["status"] == "expired"


def test_disk_limit_and_audit_failure(explorer, monkeypatch):
    client, worker = explorer
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
        if query.startswith("INSERT INTO explorer_downloads"):
            raise RuntimeError("audit unavailable")
        return real(query, *args, **kwargs)
    monkeypatch.setattr(database, "records", fail_audit)
    assert client.get(ROOT + f"/exports/{job['id']}/download").status_code == 500


def test_restart_recovery_and_single_owner(explorer):
    client, worker = explorer
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
    import data_explorer.exports as exports
    monkeypatch.setattr(exports, "SHEET_ROWS", 3)
    path = tmp_path / "test.xlsx"
    ExportWorker.write_excel(path, tmp_path, ["id"], [[("001",), ("002",), ("003",)]])
    with ZipFile(path) as archive:
        assert "xl/worksheets/sheet2.xml" in archive.namelist()
    with pytest.raises(ValueError, match="cell limit"):
        ExportWorker.write_excel(path, tmp_path, ["text"], [[("x" * 32768,)]])


def test_large_export_streams_with_bounded_memory(explorer):
    import tracemalloc
    client, worker = explorer
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


def test_polling_does_not_extend_login(explorer):
    client, _ = explorer
    response = client.get(ROOT + "/exports", headers={"X-Explorer-Poll": "1"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert "set-cookie" in client.get(ROOT + "/exports").headers


def test_preview_invalid_filters_and_literal_percent(explorer):
    client, worker = explorer
    assert client.post(ROOT + "/preview", json={"mode": "sql", "sql": "SELECT '100%' AS value"}).json()["rows"] == [["100%"]]
    job = submit(client, "SELECT '100%' AS value")
    assert run_job(worker, job)["status"] == "completed"
    malformed = {"mode": "builder", "builder": {"table": "patient", "columns": ["patient.patient_id"], "filters": {"rules": "not-a-list"}}}
    assert client.post(ROOT + "/preview", json=malformed).status_code == 422


def test_worker_cancels_running_job_and_rechecks_admin(explorer, monkeypatch):
    client, worker = explorer
    job = submit(client)
    database.records("UPDATE explorer_exports SET cancel_requested=true WHERE id=%s", (job["id"],))
    assert run_job(worker, job)["status"] == "cancelled"
    job = submit(client)
    job["username"] = USER_LVO
    result = run_job(worker, job)
    assert result["status"] == "failed"
    assert "administrator access" in result["error"]


def test_private_spool_rejects_existing_unrelated_content(tmp_path):
    (tmp_path / "important.txt").write_text("keep")
    worker = ExportWorker(Settings(enabled=True, spool_dir=str(tmp_path)))
    with pytest.raises(RuntimeError, match="empty dedicated"):
        worker.start()
    assert (tmp_path / "important.txt").read_text() == "keep"
