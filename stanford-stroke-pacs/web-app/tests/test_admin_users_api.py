"""Admin user-dataset-permissions API (GET /api/admin/users, PUT …/datasets)."""

from tests.conftest import TEST_USER, USER_CRISP, USER_LVO, login_as


class TestListUsers:
    def test_requires_admin(self, client):
        login_as(client, USER_LVO)
        assert client.get("/api/admin/users").status_code == 403

    def test_requires_login(self, client):
        assert client.get("/api/admin/users").status_code == 401

    def test_lists_users_with_grants(self, logged_in_client):
        resp = logged_in_client.get("/api/admin/users")
        assert resp.status_code == 200
        by_name = {u["username"]: u for u in resp.json()}
        assert by_name[TEST_USER]["is_admin"] is True
        assert by_name[USER_CRISP]["allowed_datasets"] == ["crisp2"]
        assert by_name[USER_LVO]["allowed_datasets"] == ["lvo"]


class TestSetDatasets:
    def _put(self, client, username, datasets):
        return client.put(
            f"/api/admin/users/{username}/datasets",
            json={"datasets": datasets},
        )

    def test_requires_admin(self, client):
        login_as(client, USER_LVO)
        assert self._put(client, USER_CRISP, ["lvo"]).status_code == 403

    def test_unknown_user_404(self, logged_in_client):
        assert self._put(logged_in_client, "nobody", ["lvo"]).status_code == 404

    def test_unknown_dataset_422(self, logged_in_client):
        resp = self._put(logged_in_client, USER_CRISP, ["typo_cohort"])
        assert resp.status_code == 422
        assert "typo_cohort" in resp.json()["detail"]

    def test_grant_and_revoke_roundtrip(self, logged_in_client, client):
        # Grant lvo on top of crisp2 …
        resp = self._put(logged_in_client, USER_CRISP, ["crisp2", "lvo"])
        assert resp.status_code == 200
        assert resp.json()["allowed_datasets"] == ["crisp2", "lvo"]
        by_name = {u["username"]: u for u in logged_in_client.get("/api/admin/users").json()}
        assert by_name[USER_CRISP]["allowed_datasets"] == ["crisp2", "lvo"]

        # … the user now sees P-0002 and the change shows in /api/me …
        login_as(client, USER_CRISP)
        ids = {it["patient_id"] for it in client.get("/api/patients").json()["items"]}
        assert "P-0002" in ids
        assert client.get("/api/me").json()["allowed_datasets"] == ["crisp2", "lvo"]

        # … then restore the fixture state and confirm the revoke applies.
        login_as(client, TEST_USER)
        assert self._put(client, USER_CRISP, ["crisp2"]).status_code == 200
        login_as(client, USER_CRISP)
        ids = {it["patient_id"] for it in client.get("/api/patients").json()["items"]}
        assert "P-0002" not in ids
