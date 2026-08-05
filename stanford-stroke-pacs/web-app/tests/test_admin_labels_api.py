"""Admin label-edit-permissions + label-deletion API.

GET /api/admin/label-definitions, PUT …/{id}/permissions, GET
…/{id}/deletion-plan, DELETE …/{id}. Mirrors test_admin_users_api.py — the same
admin gate, unknown-id 404, unknown-name 422, and a roundtrip that proves the
change takes effect through another user's session rather than merely
persisting.
"""

import pytest

from tests.conftest import TEST_USER, USER_LVO, login_as

PATIENT = "P-0001"


@pytest.fixture()
def label(logged_in_client):
    resp = logged_in_client.post(
        "/api/label-definitions",
        json={"name": "admin_perm_probe", "level": "patient", "datatype": "text"},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    yield row
    from db import get_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM annotations WHERE label = %s", (row["name"],))
            cur.execute("DELETE FROM label_definitions WHERE id = %s", (row["id"],))
        conn.commit()
    finally:
        conn.close()


def _put(client, label_id, policy, users=None):
    return client.put(
        f"/api/admin/label-definitions/{label_id}/permissions",
        json={"edit_policy": policy, "edit_users": users or []},
    )


class TestListLabelDefinitions:
    def test_requires_admin(self, client):
        login_as(client, USER_LVO)
        assert client.get("/api/admin/label-definitions").status_code == 403

    def test_requires_login(self, client):
        assert client.get("/api/admin/label-definitions").status_code == 401

    def test_lists_with_owner_and_policy(self, label, logged_in_client):
        rows = logged_in_client.get("/api/admin/label-definitions").json()
        by_name = {r["name"]: r for r in rows}
        assert by_name[label["name"]]["created_by"] == TEST_USER
        assert by_name[label["name"]]["edit_policy"] == "everyone"
        assert by_name[label["name"]]["edit_users"] == []


class TestSetLabelPermissions:
    def test_requires_admin(self, label, client):
        login_as(client, USER_LVO)
        assert _put(client, label["id"], "nobody").status_code == 403

    def test_requires_login(self, client):
        # No `label` fixture here on purpose: it authenticates through the same
        # underlying client, which would defeat the anonymous check. Auth is
        # resolved before the id is looked up, so any id proves the point.
        assert _put(client, 1, "nobody").status_code == 401

    def test_unknown_label_404(self, logged_in_client):
        assert _put(logged_in_client, 99999999, "nobody").status_code == 404

    def test_unknown_user_422(self, label, logged_in_client):
        resp = _put(logged_in_client, label["id"], "users", ["nope_not_a_user"])
        assert resp.status_code == 422
        assert "nope_not_a_user" in resp.json()["detail"]

    def test_users_policy_needs_a_username(self, label, logged_in_client):
        """An empty list is indistinguishable from 'nobody'; say so rather than
        storing a policy that silently means something else."""
        resp = _put(logged_in_client, label["id"], "users", [])
        assert resp.status_code == 422

    def test_bad_policy_400(self, label, logged_in_client):
        assert _put(logged_in_client, label["id"], "sometimes").status_code == 400

    def test_users_cleared_when_policy_is_not_users(self, label, logged_in_client):
        """A stale list must not survive to silently reactivate on a later flip."""
        assert _put(logged_in_client, label["id"], "users", [USER_LVO]).status_code == 200
        row = _put(logged_in_client, label["id"], "everyone", [USER_LVO]).json()
        assert row["edit_policy"] == "everyone"
        assert row["edit_users"] == []

    def test_lock_unlock_roundtrip_takes_effect(self, label, logged_in_client, client):
        """The proof: the setting changes what another user's session can do."""
        name = label["name"]
        login_as(client, USER_LVO)
        assert client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": PATIENT, "label": name, "value": "a"},
        ).status_code == 201

        login_as(logged_in_client, TEST_USER)
        assert _put(logged_in_client, label["id"], "nobody").status_code == 200

        login_as(client, USER_LVO)
        assert client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": PATIENT, "label": name, "value": "b"},
        ).status_code == 403

        login_as(logged_in_client, TEST_USER)
        assert _put(logged_in_client, label["id"], "everyone").status_code == 200

        login_as(client, USER_LVO)
        assert client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": PATIENT, "label": name, "value": "c"},
        ).status_code == 201


class TestDeleteLabelDefinition:
    def test_requires_admin(self, label, client):
        login_as(client, USER_LVO)
        assert (
            client.delete(f"/api/admin/label-definitions/{label['id']}").status_code
            == 403
        )
        assert (
            client.get(
                f"/api/admin/label-definitions/{label['id']}/deletion-plan"
            ).status_code
            == 403
        )

    def test_requires_login(self, client):
        assert client.delete("/api/admin/label-definitions/1").status_code == 401

    def test_unknown_label_404(self, logged_in_client):
        assert (
            logged_in_client.delete(
                "/api/admin/label-definitions/99999999"
            ).status_code
            == 404
        )
        assert (
            logged_in_client.get(
                "/api/admin/label-definitions/99999999/deletion-plan"
            ).status_code
            == 404
        )

    def test_plan_reports_annotation_count(self, label, logged_in_client):
        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": PATIENT,
                "label": label["name"],
                "value": "x",
            },
        )
        plan = logged_in_client.get(
            f"/api/admin/label-definitions/{label['id']}/deletion-plan"
        ).json()
        assert plan["name"] == label["name"]
        assert plan["level"] == "patient"
        assert plan["n_annotations"] == 1
        assert plan["labelled_table"] == "patient_labelled"

    def test_delete_removes_definition_annotations_and_vocabulary(
        self, logged_in_client, db_conn
    ):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "doomed_label",
                "level": "patient",
                "datatype": "select",
                "options": ["a", "b"],
            },
        )
        assert create.status_code == 201, create.text
        label_id = create.json()["id"]
        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": PATIENT,
                "label": "doomed_label",
                "value": "a",
            },
        )

        resp = logged_in_client.delete(f"/api/admin/label-definitions/{label_id}")
        assert resp.status_code == 200
        assert resp.json()["n_annotations_deleted"] == 1

        names = [
            r["name"]
            for r in logged_in_client.get("/api/admin/label-definitions").json()
        ]
        assert "doomed_label" not in names
        assert logged_in_client.get("/api/labels/doomed_label/values").json() == []
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM annotations WHERE label = 'doomed_label'")
            assert cur.fetchone()[0] == 0
            # The labelled-mirror column is gone too.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'patient_labelled' AND column_name = 'doomed_label'"
            )
            assert cur.fetchone() is None
