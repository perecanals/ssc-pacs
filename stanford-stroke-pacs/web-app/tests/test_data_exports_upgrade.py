"""Preserve host credentials, generated files and audit rows during the rename."""

import runpy
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

STACK = Path(__file__).resolve().parents[2]
upgrade = runpy.run_path(str(STACK / "scripts/admin/migrate_data_exports.py"))
revision = runpy.run_path(str(STACK / "alembic/versions/0024_data_exports_naming.py"))


@pytest.fixture
def host_files(tmp_path):
    spool = tmp_path / "data-explorer"
    spool.mkdir(mode=0o700)
    (spool / ".ssc-explorer-spool").write_text("SSC Data Explorer temporary artifacts\n")
    job = spool / str(uuid4())
    job.mkdir()
    (job / "export.csv").write_bytes(b"patient_id\r\n001\r\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[other]\nvalue = "unchanged"\n\n[data-explorer]\nenabled = true\nspool_dir = "{spool}"\nretention_hours = 72\n'
    )
    env = tmp_path / ".env"
    env.write_text(
        "UNRELATED='preserve this'\nEXPLORER_DB_USER='test-reader'\nEXPLORER_DB_PASSWORD='synthetic value with spaces ^³'\n"
    )
    env.chmod(0o600)
    return config, env, spool, job.name


class Cursor:
    def __init__(self, *, active=False, marker=None):
        self.active = active
        self.marker = marker or upgrade["OLD_ROLE_MARKER"]
        self.result = None
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if isinstance(query, str) and "pg_try_advisory_lock" in query:
            self.result = (not self.active,)
        elif isinstance(query, str) and "shobj_description" in query:
            self.result = (self.marker,)

    def fetchone(self):
        return self.result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Connection:
    def __init__(self, **kwargs):
        self.cur = Cursor(**kwargs)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_host_upgrade_preserves_files_credentials_and_is_repeatable(host_files):
    config, env, source, job_id = host_files
    before_env = env.read_text()
    before_config = config.read_text()
    files, old, target, role = upgrade["plan"](config, env)
    assert role == "test-reader" and old == source and target == source.with_name("data-exports")
    assert config.read_text() == before_config and env.read_text() == before_env
    assert source.exists() and not target.exists()
    conn = Connection()
    upgrade["execute"](config, env, conn)
    assert conn.committed and conn.closed
    assert not source.exists()
    assert (target / job_id / "export.csv").read_bytes() == b"patient_id\r\n001\r\n"
    assert (target / ".ssc-data-exports-spool").is_file()
    assert not (target / ".ssc-explorer-spool").exists()
    assert env.read_text() == before_env.replace("EXPLORER_DB_", "DATA_EXPORTS_DB_")
    assert env.stat().st_mode & 0o777 == 0o600
    assert "retention_hours = 72" in config.read_text()
    assert "[data-exports]" in config.read_text() and str(target) in config.read_text()
    again = (config.read_bytes(), env.read_bytes())
    upgrade["execute"](config, env, Connection(marker=upgrade["NEW_ROLE_MARKER"]))
    assert (config.read_bytes(), env.read_bytes()) == again


@pytest.mark.parametrize("reason", ["worker", "role", "directory", "section", "credentials"])
def test_host_upgrade_rejects_conflicts_without_changes(host_files, reason):
    config, env, source, _ = host_files
    if reason == "directory":
        source.with_name("data-exports").mkdir()
    elif reason == "section":
        config.write_text(config.read_text() + "\n[data-exports]\nenabled = false\n")
    elif reason == "credentials":
        env.write_text(env.read_text() + "DATA_EXPORTS_DB_USER='another-reader'\n")
    before = (config.read_bytes(), env.read_bytes())
    conn = Connection(active=reason == "worker", marker="unrelated role" if reason == "role" else None)
    with pytest.raises(ValueError):
        upgrade["execute"](config, env, conn)
    assert (config.read_bytes(), env.read_bytes()) == before
    assert (source / ".ssc-explorer-spool").exists()
    assert conn.rolled_back and conn.closed


def test_host_upgrade_restores_files_if_database_commit_fails(host_files):
    config, env, source, job_id = host_files
    before = (config.read_bytes(), env.read_bytes())
    conn = Connection()

    def fail():
        raise OSError("Synthetic database failure")

    conn.commit = fail
    with pytest.raises(OSError):
        upgrade["execute"](config, env, conn)
    assert (config.read_bytes(), env.read_bytes()) == before
    assert (source / job_id / "export.csv").is_file()
    assert (source / ".ssc-explorer-spool").exists()
    assert not source.with_name("data-exports").exists()
    assert conn.rolled_back and conn.closed


def test_schema_rename_preserves_records_constraints_and_sequence(test_db):
    engine = create_engine("postgresql+psycopg2://", creator=lambda: psycopg2.connect(**test_db), poolclass=NullPool)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                revision["downgrade"]()
                job_id, report_id = str(uuid4()), str(uuid4())
                conn.execute(
                    text(
                        "INSERT INTO explorer_exports (id, name, username, configuration, sql, parameters, format, status) VALUES (:id, 'Preserved export', 'synthetic', '{\"dataset\":\"test\"}', 'SELECT 1', '[]', 'csv', 'completed')"
                    ),
                    {"id": job_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO explorer_reports (id, name, configuration, created_by, updated_by) VALUES (:id, 'Preserved report', '{}', 'synthetic', 'synthetic')"
                    ),
                    {"id": report_id},
                )
                audit_id = conn.execute(
                    text("INSERT INTO explorer_downloads (export_id, username) VALUES (:id, 'synthetic') RETURNING id"),
                    {"id": job_id},
                ).scalar_one()
                before = conn.execute(
                    text("SELECT row_to_json(j) FROM explorer_exports j WHERE id=:id"), {"id": job_id}
                ).scalar_one()
                revision["upgrade"]()
                assert (
                    conn.execute(
                        text("SELECT row_to_json(j) FROM data_exports_jobs j WHERE id=:id"), {"id": job_id}
                    ).scalar_one()
                    == before
                )
                assert (
                    conn.execute(
                        text("SELECT name FROM data_exports_reports WHERE id=:id"), {"id": report_id}
                    ).scalar_one()
                    == "Preserved report"
                )
                assert (
                    conn.execute(
                        text("SELECT export_id::text FROM data_exports_downloads WHERE id=:id"), {"id": audit_id}
                    ).scalar_one()
                    == job_id
                )
                assert (
                    conn.execute(
                        text(
                            "INSERT INTO data_exports_downloads (export_id, username) VALUES (:id, 'synthetic') RETURNING id"
                        ),
                        {"id": job_id},
                    ).scalar_one()
                    > audit_id
                )
                assert not conn.execute(
                    text(
                        "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relname LIKE 'explorer_%'"
                    )
                ).all()
                for table, _, name in revision["CONSTRAINTS"]:
                    assert (
                        conn.execute(
                            text(
                                "SELECT count(*) FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname=:name"
                            ),
                            {"table": table, "name": name},
                        ).scalar_one()
                        == 1
                    )
                revision["downgrade"]()
                assert (
                    conn.execute(text("SELECT name FROM explorer_exports WHERE id=:id"), {"id": job_id}).scalar_one()
                    == "Preserved export"
                )
        finally:
            transaction.rollback()
    engine.dispose()


def test_schema_rename_refuses_an_active_worker(test_db):
    holder = psycopg2.connect(**test_db)
    engine = create_engine("postgresql+psycopg2://", creator=lambda: psycopg2.connect(**test_db), poolclass=NullPool)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(782341, 21)")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="Stop the Data Exports worker"):
                revision["downgrade"]()
            assert conn.execute(text("SELECT to_regclass('public.data_exports_jobs')")).scalar_one() is not None
    finally:
        holder.close()
        engine.dispose()
