"""Tests for label definition CRUD."""

import pytest


@pytest.fixture()
def _cleanup_labels(db_conn):
    """Remove label definitions created during the test."""
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM label_definitions WHERE created_by = 'testuser'")
    db_conn.commit()


@pytest.mark.usefixtures("_cleanup_labels")
class TestLabelDefinitions:
    def test_create_label_definition(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "test_bool_label", "level": "series", "datatype": "bool"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test_bool_label"
        assert data["datatype"] == "bool"
        assert data["level"] == "series"

    def test_list_label_definitions(self, logged_in_client):
        logged_in_client.post(
            "/api/label-definitions",
            json={"name": "list_test_label", "level": "patient", "datatype": "text"},
        )
        resp = logged_in_client.get("/api/label-definitions")
        assert resp.status_code == 200
        names = [d["name"] for d in resp.json()]
        assert "list_test_label" in names

    def test_list_label_definitions_filtered_by_level(self, logged_in_client):
        logged_in_client.post(
            "/api/label-definitions",
            json={"name": "level_filter_test", "level": "study", "datatype": "bool"},
        )
        resp = logged_in_client.get("/api/label-definitions", params={"level": "study"})
        assert resp.status_code == 200
        names = [d["name"] for d in resp.json()]
        assert "level_filter_test" in names

    def test_duplicate_name_returns_409(self, logged_in_client):
        logged_in_client.post(
            "/api/label-definitions",
            json={"name": "dup_test", "level": "series", "datatype": "bool"},
        )
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "dup_test", "level": "series", "datatype": "bool"},
        )
        assert resp.status_code == 409

    def test_invalid_name_returns_400(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "1_starts_with_digit", "level": "series", "datatype": "bool"},
        )
        assert resp.status_code == 400

    def test_invalid_datatype_returns_400(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "valid_name", "level": "series", "datatype": "float"},
        )
        assert resp.status_code == 400

    def test_create_select_label_with_options(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "select_test",
                "level": "series",
                "datatype": "select",
                "options": ["opt_a", "opt_b", "opt_c"],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["options"] == ["opt_a", "opt_b", "opt_c"]
