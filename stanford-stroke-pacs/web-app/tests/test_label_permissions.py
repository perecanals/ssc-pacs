"""Per-label edit permissions (label_definitions.edit_policy / edit_users).

The enforcement matrix. These drive the API directly and never the UI: the
frontend gate is cosmetic, the server is the boundary. The key assertions are
the ones that pin the *no-admin-bypass* decision — a locked label 403s an admin
exactly as it 403s a rater.
"""

import pytest

from tests.conftest import TEST_USER, USER_LVO, login_as

# USER_LVO is granted the 'lvo' dataset, so P-0001 is in scope for them — any
# 403 here is the label policy, never dataset scoping (which 404s).
PATIENT = "P-0001"


def _set_policy(client, label_id, policy, users=None):
    return client.put(
        f"/api/admin/label-definitions/{label_id}/permissions",
        json={"edit_policy": policy, "edit_users": users or []},
    )


def _post_value(client, label, value="x"):
    return client.post(
        "/api/annotations",
        json={"level": "patient", "patient_id": PATIENT, "label": label, "value": value},
    )


@pytest.fixture()
def label(logged_in_client):
    """A throwaway patient-level text label, unrestricted by default.

    Created via the API so the default really is exercised. Torn down through
    the DB (there is no DELETE endpoint for label definitions).
    """
    resp = logged_in_client.post(
        "/api/label-definitions",
        json={"name": "perm_probe", "level": "patient", "datatype": "text"},
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


class TestDefaultIsUnrestricted:
    def test_new_label_defaults_to_everyone(self, label):
        assert label["edit_policy"] == "everyone"
        assert label["edit_users"] == []

    def test_any_user_may_edit(self, label, client):
        login_as(client, USER_LVO)
        assert _post_value(client, label["name"]).status_code == 201

    def test_undefined_label_still_editable(self, client):
        """Annotations for labels with no definition row are already allowed;
        this feature restricts, it must never newly forbid."""
        login_as(client, USER_LVO)
        resp = _post_value(client, "label_with_no_definition")
        assert resp.status_code == 201


class TestPolicyNobody:
    def test_rater_gets_403(self, label, logged_in_client, client):
        assert _set_policy(logged_in_client, label["id"], "nobody").status_code == 200
        login_as(client, USER_LVO)
        assert _post_value(client, label["name"]).status_code == 403

    def test_admin_also_gets_403(self, label, logged_in_client):
        """No admin bypass, by design: 'nobody' means nobody. An admin who must
        correct a value changes the policy first — deliberate and audited."""
        assert _set_policy(logged_in_client, label["id"], "nobody").status_code == 200
        assert _post_value(logged_in_client, label["name"]).status_code == 403

    def test_existing_value_is_not_clobbered(self, label, logged_in_client, client):
        login_as(client, USER_LVO)
        assert _post_value(client, label["name"], "original").status_code == 201
        login_as(logged_in_client, TEST_USER)
        _set_policy(logged_in_client, label["id"], "nobody")
        login_as(client, USER_LVO)
        assert _post_value(client, label["name"], "clobbered").status_code == 403
        # The stored value survived the refused write.
        login_as(client, USER_LVO)
        rows = client.get("/api/patients", params={"patient_id": PATIENT}).json()["items"]
        anns = rows[0].get("annotations") or []
        stored = [a for a in anns if a["label"] == label["name"]]
        assert stored and stored[0]["value"] == "original"

    def test_delete_is_gated_too(self, label, logged_in_client, client):
        """Clearing a value is a write."""
        login_as(client, USER_LVO)
        ann_id = _post_value(client, label["name"]).json()["id"]
        login_as(logged_in_client, TEST_USER)
        _set_policy(logged_in_client, label["id"], "nobody")
        login_as(client, USER_LVO)
        assert client.delete(f"/api/annotations/{ann_id}").status_code == 403


class TestPolicyUsers:
    def test_listed_user_may_edit(self, label, logged_in_client, client):
        assert _set_policy(
            logged_in_client, label["id"], "users", [USER_LVO]
        ).status_code == 200
        login_as(client, USER_LVO)
        assert _post_value(client, label["name"]).status_code == 201

    def test_unlisted_user_gets_403(self, label, logged_in_client, client):
        assert _set_policy(
            logged_in_client, label["id"], "users", [USER_LVO]
        ).status_code == 200
        login_as(client, "user_crisp")
        assert _post_value(client, label["name"]).status_code == 403

    def test_unlisted_admin_gets_403(self, label, logged_in_client):
        """Consistent with 'nobody': being an admin is not an implicit grant."""
        assert _set_policy(
            logged_in_client, label["id"], "users", [USER_LVO]
        ).status_code == 200
        assert _post_value(logged_in_client, label["name"]).status_code == 403


class TestSelectVocabularyIsGated:
    def test_protected_select_label_rejects_novel_value(self, logged_in_client, client):
        """Posting a novel value to a select label extends the shared
        vocabulary — the gate must close that path too, not just the write."""
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "perm_probe_sel",
                "level": "patient",
                "datatype": "select",
                "options": ["a"],
            },
        )
        label_id, name = resp.json()["id"], resp.json()["name"]
        try:
            _set_policy(logged_in_client, label_id, "nobody")
            login_as(client, USER_LVO)
            assert _post_value(client, name, "invented").status_code == 403
            login_as(client, TEST_USER)
            values = client.get(f"/api/labels/{name}/values").json()
            assert "invented" not in values
        finally:
            from db import get_conn

            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM annotations WHERE label = %s", (name,))
                    cur.execute("DELETE FROM label_value_options WHERE label = %s", (name,))
                    cur.execute("DELETE FROM label_definitions WHERE id = %s", (label_id,))
                conn.commit()
            finally:
                conn.close()


class TestWhoMayChangeThePolicy:
    def _patch(self, client, label_id, **body):
        return client.patch(f"/api/label-definitions/{label_id}", json=body)

    def test_owner_may(self, label, logged_in_client):
        # The fixture label was created by TEST_USER (logged_in_client).
        assert label["created_by"] == TEST_USER
        resp = self._patch(logged_in_client, label["id"], edit_policy="nobody")
        assert resp.status_code == 200
        assert resp.json()["edit_policy"] == "nobody"

    def test_non_owner_non_admin_may_not(self, label, logged_in_client, client):
        """Otherwise the protection is self-defeating: anyone could unlock,
        then edit."""
        _set_policy(logged_in_client, label["id"], "users", [USER_LVO])
        login_as(client, USER_LVO)
        # Listed as an editor, but that grants value edits — not control.
        assert self._patch(client, label["id"], edit_policy="everyone").status_code == 403

    def test_description_still_editable_by_anyone(self, label, client):
        """This change must not tighten the pre-existing description/instrument
        behavior."""
        login_as(client, USER_LVO)
        assert self._patch(client, label["id"], description="hi").status_code == 200
