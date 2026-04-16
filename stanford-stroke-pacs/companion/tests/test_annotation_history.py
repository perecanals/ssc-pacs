"""Tests for the annotation audit trail (trigger + history endpoint)."""

from __future__ import annotations

import psycopg2.extras
import pytest


@pytest.fixture(autouse=True)
def _cleanup(db_conn):
    """Remove test annotations and history rows after each test."""
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM annotations_history WHERE entity_id LIKE 'HIST-TEST%%'")
        cur.execute("DELETE FROM annotations WHERE patient_id = 'HIST-TEST'")
    db_conn.commit()


def _history_rows(db_conn, annotation_id: int) -> list[dict]:
    """Fetch history rows for an annotation, newest first."""
    with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM annotations_history "
            "WHERE annotation_id = %s ORDER BY history_id",
            (annotation_id,),
        )
        return cur.fetchall()


class TestTriggerCapture:
    """Verify the PL/pgSQL trigger records INSERT / UPDATE / DELETE."""

    def test_insert_creates_history_row(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'alice'")
            cur.execute(
                "INSERT INTO annotations (level, patient_id, label, value, created_by) "
                "VALUES ('patient', 'HIST-TEST', 'flag_a', 'v1', 'alice') RETURNING id",
            )
            ann_id = cur.fetchone()[0]
        db_conn.commit()

        rows = _history_rows(db_conn, ann_id)
        assert len(rows) == 1
        h = rows[0]
        assert h["operation"] == "I"
        assert h["operation_by"] == "alice"
        assert h["value_before"] is None
        assert h["value_after"] == "v1"
        assert h["entity_id"] == "HIST-TEST"
        assert h["label"] == "flag_a"

    def test_update_records_before_and_after(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'bob'")
            cur.execute(
                "INSERT INTO annotations (level, patient_id, label, value, created_by) "
                "VALUES ('patient', 'HIST-TEST', 'flag_b', 'old', 'bob') RETURNING id",
            )
            ann_id = cur.fetchone()[0]
        db_conn.commit()

        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'carol'")
            cur.execute(
                "UPDATE annotations SET value = 'new', created_by = 'carol' WHERE id = %s",
                (ann_id,),
            )
        db_conn.commit()

        rows = _history_rows(db_conn, ann_id)
        assert len(rows) == 2
        u = rows[1]
        assert u["operation"] == "U"
        assert u["operation_by"] == "carol"
        assert u["value_before"] == "old"
        assert u["value_after"] == "new"

    def test_delete_records_last_value(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'dave'")
            cur.execute(
                "INSERT INTO annotations (level, patient_id, label, value, created_by) "
                "VALUES ('patient', 'HIST-TEST', 'flag_c', 'gone', 'dave') RETURNING id",
            )
            ann_id = cur.fetchone()[0]
        db_conn.commit()

        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'eve'")
            cur.execute("DELETE FROM annotations WHERE id = %s", (ann_id,))
        db_conn.commit()

        rows = _history_rows(db_conn, ann_id)
        assert len(rows) == 2  # I + D
        d = rows[1]
        assert d["operation"] == "D"
        assert d["operation_by"] == "eve"
        assert d["value_before"] == "gone"
        assert d["value_after"] is None

    def test_upsert_fires_update_on_conflict(self, db_conn):
        """INSERT ... ON CONFLICT DO UPDATE fires UPDATE trigger on conflict."""
        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'frank'")
            cur.execute(
                "INSERT INTO annotations (level, patient_id, label, value, created_by) "
                "VALUES ('patient', 'HIST-TEST', 'flag_d', 'first', 'frank') RETURNING id",
            )
            ann_id = cur.fetchone()[0]
        db_conn.commit()

        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = 'grace'")
            cur.execute(
                "INSERT INTO annotations (level, patient_id, label, value, created_by) "
                "VALUES ('patient', 'HIST-TEST', 'flag_d', 'second', 'grace') "
                "ON CONFLICT (patient_id, label) WHERE level = 'patient' "
                "DO UPDATE SET value = EXCLUDED.value, created_by = EXCLUDED.created_by",
            )
        db_conn.commit()

        rows = _history_rows(db_conn, ann_id)
        assert len(rows) == 2  # I + U
        u = rows[1]
        assert u["operation"] == "U"
        assert u["operation_by"] == "grace"
        assert u["value_before"] == "first"
        assert u["value_after"] == "second"

    def test_fallback_to_system_when_no_user_set(self, db_conn):
        """Without SET LOCAL, operation_by defaults to 'system'."""
        with db_conn.cursor() as cur:
            # Do NOT set app.audit_user — should default to 'system'
            cur.execute(
                "INSERT INTO annotations (level, patient_id, label, value, created_by) "
                "VALUES ('patient', 'HIST-TEST', 'flag_e', 'x', 'anon') RETURNING id",
            )
            ann_id = cur.fetchone()[0]
        db_conn.commit()

        rows = _history_rows(db_conn, ann_id)
        assert len(rows) == 1
        assert rows[0]["operation_by"] == "system"


class TestHistoryEndpoint:
    """Verify GET /api/annotations/{id}/history."""

    def test_admin_can_view_history(self, logged_in_client, db_conn):
        # Create an annotation via the API (trigger fires).
        resp = logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "HIST-TEST",
                "label": "api_hist",
                "value": "v1",
            },
        )
        assert resp.status_code == 201
        ann_id = resp.json()["id"]

        # Fetch history.
        resp = logged_in_client.get(f"/api/annotations/{ann_id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["operation"] == "I"
        assert data[0]["value_after"] == "v1"

    def test_non_admin_gets_403(self, client, db_conn):
        """Non-admin users should be denied access."""
        # Create a non-admin user.
        import bcrypt

        pw_hash = bcrypt.hashpw(b"pass", bcrypt.gensalt()).decode()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) "
                "VALUES ('hist_viewer', %s, false) ON CONFLICT DO NOTHING",
                (pw_hash,),
            )
        db_conn.commit()

        # Log in as the non-admin.
        client.post("/api/login", json={"username": "hist_viewer", "password": "pass"})
        resp = client.get("/api/annotations/1/history")
        assert resp.status_code == 403

        # Clean up.
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = 'hist_viewer'")
        db_conn.commit()

    def test_history_sorted_newest_first(self, logged_in_client, db_conn):
        """History should be returned in descending time order."""
        # Create then update an annotation.
        resp = logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "HIST-TEST",
                "label": "sort_test",
                "value": "a",
            },
        )
        ann_id = resp.json()["id"]

        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "HIST-TEST",
                "label": "sort_test",
                "value": "b",
            },
        )

        resp = logged_in_client.get(f"/api/annotations/{ann_id}/history")
        data = resp.json()
        assert len(data) >= 2
        # Newest first.
        assert data[0]["operation"] == "U"
        assert data[1]["operation"] == "I"
