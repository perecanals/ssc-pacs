"""One bounded export worker, disk-backed artifacts, durable audit and restart recovery."""

import csv
import datetime
import json
import logging
import math
import os
import shutil
import threading
import time
from pathlib import Path
from uuid import UUID

import psycopg2
import xlsxwriter

from auth import can_user_export
from data_exports.access import artifact_in_scope
from data_exports.database import query_connection, records
from dataset_access import fetch_user_scope
from db import get_conn

logger = logging.getLogger(__name__)
SHEET_ROWS = 1_048_576
SERIALIZATION = {
    "version": 1,
    "null": "\\N",
    "dates": "ISO-8601 UTC",
    "arrays_json": True,
    "csv_formula_escape": "apostrophe",
    "xlsx_identifiers": "text",
}


def text_value(value):
    if value is None:
        return "\\N"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def csv_value(value):
    text = text_value(value)
    # Escape even when whitespace precedes a spreadsheet formula marker.
    if isinstance(value, str) and text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


class Cancelled(Exception):
    pass


class ExportWorker:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.spool_dir)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.connection = None
        self.active_id = None
        self.thread = None
        self.owner = None

    def start(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise RuntimeError("Data Exports spool must be owned by the app user with mode 0700")
        marker = self.root / ".ssc-data-exports-spool"
        if not marker.exists():
            if any(self.root.iterdir()):
                raise RuntimeError("Data Exports spool must be an empty dedicated directory on first use")
            marker.write_text("SSC Data Exports temporary artifacts\n")
        self.owner = get_conn()
        with self.owner.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(782341, 21)")
            if not cur.fetchone()[0]:
                self.owner.close()
                self.owner = None
                raise RuntimeError("Only one Data Exports app process may own the export worker")
        self.owner.commit()
        records(
            "UPDATE data_exports_jobs SET status='failed', finished_at=now(), error='Interrupted by app restart' "
            "WHERE status IN ('queued','running')"
        )
        self.cleanup()
        self.thread = threading.Thread(target=self.loop, name="data-export", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            if self.connection:
                self.connection.cancel()
        if self.thread:
            self.thread.join()
        if self.owner:
            with self.owner.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(782341, 21)")
            self.owner.commit()
            self.owner.close()
            self.owner = None

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.cleanup()
                job = records(
                    "UPDATE data_exports_jobs SET status='running', started_at=now() "
                    "WHERE id=(SELECT id FROM data_exports_jobs WHERE status='queued' "
                    "ORDER BY created_at LIMIT 1) RETURNING *",
                    one=True,
                )
                if job:
                    self.run(job)
                    continue
            except Exception:
                logger.error("Data Exports worker operation failed", exc_info=False)
            self.stop_event.wait(2)

    def cancel(self, job_id):
        with self.lock:
            if self.active_id == str(job_id) and self.connection:
                self.connection.cancel()

    def cleanup(self):
        expired = records(
            "UPDATE data_exports_jobs SET status='expired' WHERE status='completed' AND expires_at<=now() RETURNING id"
        )
        for job in expired:
            shutil.rmtree(self.root / str(job["id"]), ignore_errors=True)
        live = {
            str(r["id"]) for r in records("SELECT id FROM data_exports_jobs WHERE status IN ('running','completed')")
        }
        for entry in self.root.iterdir():
            try:
                UUID(entry.name)
            except ValueError:
                continue
            if entry.is_dir() and not entry.is_symlink() and entry.name not in live:
                shutil.rmtree(entry)

    def disk_guard(self, directory):
        total = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        if size > self.settings.artifact_bytes or total > self.settings.spool_bytes:
            raise ValueError("Export storage limit reached; reduce the export or use CSV")
        if shutil.disk_usage(self.root).free < self.settings.reserve_bytes:
            raise ValueError("Insufficient free disk space for this export")

    def run(self, job):
        job_id = str(job["id"])
        directory = self.root / job_id
        path = directory / ("export." + job["format"])
        conn = None
        timer = None
        count = 0
        started = time.monotonic()
        try:
            directory.mkdir(mode=0o700)
            if not can_user_export(job["username"]):
                raise ValueError("Requesting user no longer has staff or admin access")
            if not artifact_in_scope(job, fetch_user_scope(job["username"])):
                raise ValueError("Dataset access changed; create a new export")
            self.disk_guard(directory)
            conn = query_connection(self.settings.timeout_seconds)
            with self.lock:
                self.connection, self.active_id = conn, job_id
            timer = threading.Timer(self.settings.timeout_seconds, self.cancel, args=(job_id,))
            timer.start()
            with conn.cursor(name="data_exports_export") as cur:
                cur.itersize = 1000
                # Immutable SQL was validated at submission. Parameters are
                # passed only for the visual builder (raw SQL may contain %).
                cur.execute(job["sql"], job["parameters"] or None)
                batch = cur.fetchmany(1000)
                headers = [c.name for c in cur.description]
                if len(headers) > 16384 and job["format"] == "xlsx":
                    raise ValueError("Excel supports at most 16,384 columns; use CSV")

                def batches():
                    nonlocal batch, count
                    while batch:
                        state = records(
                            "SELECT cancel_requested FROM data_exports_jobs WHERE id=%s", (job_id,), one=True
                        )
                        if self.stop_event.is_set() or state["cancel_requested"]:
                            raise Cancelled()
                        if time.monotonic() - started > self.settings.timeout_seconds:
                            raise ValueError("Export exceeded its execution deadline")
                        yield batch
                        count += len(batch)
                        self.disk_guard(directory)
                        records("UPDATE data_exports_jobs SET row_count=%s WHERE id=%s", (count, job_id))
                        batch = cur.fetchmany(1000)

                if job["format"] == "csv":
                    with path.open("w", encoding="utf-8-sig", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerow([csv_value(h) for h in headers])
                        for rows in batches():
                            writer.writerows([csv_value(v) for v in row] for row in rows)
                            handle.flush()
                else:
                    self.write_excel(path, directory, headers, batches())
            self.disk_guard(directory)
            if time.monotonic() - started > self.settings.timeout_seconds:
                raise ValueError("Export exceeded its execution deadline")
            completed = records(
                "UPDATE data_exports_jobs SET status='completed', finished_at=now(), "
                "expires_at=now() + %s * interval '1 hour', file_size=%s, row_count=%s "
                "WHERE id=%s AND NOT cancel_requested RETURNING id",
                (self.settings.retention_hours, path.stat().st_size, count, job_id),
                one=True,
            )
            if not completed:
                raise Cancelled()
        except Exception as exc:
            shutil.rmtree(directory, ignore_errors=True)
            state = records("SELECT cancel_requested FROM data_exports_jobs WHERE id=%s", (job_id,), one=True)
            cancelled = isinstance(exc, Cancelled) or state["cancel_requested"]
            if cancelled:
                message = "Cancelled"
            elif isinstance(exc, ValueError):
                message = str(exc)
            elif isinstance(exc, psycopg2.errors.QueryCanceled):
                message = "Query cancelled or execution deadline exceeded"
            else:
                message = "Export failed; check configuration and query compatibility"
            records(
                "UPDATE data_exports_jobs SET status=%s, finished_at=now(), error=%s, row_count=%s WHERE id=%s",
                ("cancelled" if cancelled else "failed", message, count, job_id),
            )
        finally:
            if timer:
                timer.cancel()
                timer.join()
            with self.lock:
                self.connection = self.active_id = None
            if conn:
                conn.rollback()
                conn.close()

    @staticmethod
    def write_excel(path, directory, headers, batches):
        with xlsxwriter.Workbook(
            str(path),
            {"constant_memory": True, "tmpdir": str(directory), "strings_to_formulas": False, "strings_to_urls": False},
        ) as book:
            # Empty results still get a worksheet with a header row.
            sheet = book.add_worksheet("Export 1")
            for col, header in enumerate(headers):
                if sheet.write_string(0, col, header) != 0:
                    raise ValueError("Excel header limit exceeded; use CSV")
            sheet.freeze_panes(1, 0)
            index = 1
            sheets = 1
            for batch in batches:
                for row in batch:
                    if index >= SHEET_ROWS:
                        sheets += 1
                        sheet = book.add_worksheet(f"Export {sheets}")
                        sheet.write_row(0, 0, headers)
                        sheet.freeze_panes(1, 0)
                        index = 1
                    for col, value in enumerate(row):
                        if isinstance(value, bool):
                            result = sheet.write_boolean(index, col, value)
                        elif isinstance(value, (int, float)) and math.isfinite(value) and len(str(value)) <= 15:
                            result = sheet.write_number(index, col, value)
                        else:
                            # Strings (including IDs), high-precision numbers,
                            # NULLs and JSON stay text, never formulas.
                            result = sheet.write_string(index, col, text_value(value))
                        if result != 0:
                            raise ValueError("Excel cell limit exceeded; use CSV")
                    index += 1
