"""Tests for label definition CRUD."""

import pytest

from tests.conftest import USER_LVO, login_as


@pytest.fixture()
def _cleanup_labels(db_conn):
    """Remove label definitions (and their vocabulary) created during the test."""
    yield
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM label_value_options WHERE label IN "
            "(SELECT name FROM label_definitions WHERE created_by = 'testuser')"
        )
        cur.execute("DELETE FROM annotations WHERE created_by = 'testuser'")
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
        # The detail must name the *label name* as the culprit — it renders in
        # the modal, where a vague message reads as an option-value error.
        assert resp.json()["detail"].startswith("Label name")

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

    def test_create_label_with_instrument(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "with_instrument",
                "level": "series",
                "datatype": "bool",
                "instrument": "Functional outcome",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["instrument"] == "Functional outcome"

    def test_create_label_blank_instrument_stored_as_null(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "blank_instrument",
                "level": "series",
                "datatype": "bool",
                "instrument": "   ",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["instrument"] is None

    def test_list_returns_instrument_field(self, logged_in_client):
        logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "lists_instr",
                "level": "series",
                "datatype": "bool",
                "instrument": "Demographics",
            },
        )
        resp = logged_in_client.get("/api/label-definitions")
        assert resp.status_code == 200
        match = next(d for d in resp.json() if d["name"] == "lists_instr")
        assert match["instrument"] == "Demographics"

    def test_patch_updates_instrument(self, logged_in_client):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "patch_target",
                "level": "series",
                "datatype": "bool",
            },
        )
        label_id = create.json()["id"]
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"instrument": "Imaging quality"},
        )
        assert resp.status_code == 200
        assert resp.json()["instrument"] == "Imaging quality"

    def test_patch_only_updates_supplied_fields(self, logged_in_client):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "patch_partial",
                "level": "series",
                "datatype": "bool",
                "description": "original",
                "instrument": "first",
            },
        )
        label_id = create.json()["id"]
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"instrument": "second"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["instrument"] == "second"
        assert body["description"] == "original"

    def test_patch_with_no_editable_fields_returns_400(self, logged_in_client):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "patch_empty", "level": "series", "datatype": "bool"},
        )
        label_id = create.json()["id"]
        resp = logged_in_client.patch(f"/api/label-definitions/{label_id}", json={})
        assert resp.status_code == 400

    def test_patch_unknown_label_returns_404(self, logged_in_client):
        resp = logged_in_client.patch(
            "/api/label-definitions/999999999",
            json={"instrument": "x"},
        )
        assert resp.status_code == 404

    def test_create_select_label_seeds_value_options(self, logged_in_client):
        """Curated options are seeded into the vocabulary and served via /values."""
        logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "seed_vocab",
                "level": "patient",
                "datatype": "select",
                "options": ["beta", "alpha"],
            },
        )
        resp = logged_in_client.get("/api/labels/seed_vocab/values")
        assert resp.status_code == 200
        assert resp.json() == ["alpha", "beta"]

    def test_inline_value_reaches_values_and_definitions(self, logged_in_client):
        """A new value typed inline shows up in /values and the effective options."""
        logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "inline_vocab",
                "level": "patient",
                "datatype": "select",
                "options": ["preset"],
            },
        )
        # Annotate a patient with a brand-new value.
        resp = logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "P-0001",
                "label": "inline_vocab",
                "value": "freshly_created",
            },
        )
        assert resp.status_code == 201

        # Inline dropdown source.
        values = logged_in_client.get("/api/labels/inline_vocab/values").json()
        assert values == ["freshly_created", "preset"]

        # Column-filter source (effective options on the label definition).
        defs = logged_in_client.get("/api/label-definitions").json()
        match = next(d for d in defs if d["name"] == "inline_vocab")
        assert match["options"] == ["freshly_created", "preset"]

    def test_numeric_values_sort_naturally(self, logged_in_client):
        """Score-style vocabularies sort numerically (0, 1, …, 10 — not
        0, 1, 10, 2, …), with non-numeric strings first in naive order."""
        logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "aspects_vocab",
                "level": "study",
                "datatype": "select",
                "options": ["10", "2", "0", "unknown", "1"],
            },
        )
        expected = ["unknown", "0", "1", "2", "10"]

        # Inline dropdown source.
        values = logged_in_client.get("/api/labels/aspects_vocab/values").json()
        assert values == expected

        # Column-filter / sidebar source (effective options on the definition).
        defs = logged_in_client.get("/api/label-definitions").json()
        match = next(d for d in defs if d["name"] == "aspects_vocab")
        assert match["options"] == expected

    def test_non_select_value_not_recorded(self, logged_in_client):
        """Text-label values are not added to the select vocabulary."""
        logged_in_client.post(
            "/api/label-definitions",
            json={"name": "free_text", "level": "patient", "datatype": "text"},
        )
        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "P-0001",
                "label": "free_text",
                "value": "some prose",
            },
        )
        assert logged_in_client.get("/api/labels/free_text/values").json() == []

    def test_patch_options_updates_definition_and_vocabulary(self, logged_in_client):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "opts_patch",
                "level": "series",
                "datatype": "select",
                "options": ["a", "b"],
            },
        )
        label_id = create.json()["id"]
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": ["a", "c"]},
        )
        assert resp.status_code == 200
        assert resp.json()["options"] == ["a", "c"]
        # Vocabulary reconciled: added value present, removed value pruned.
        assert logged_in_client.get("/api/labels/opts_patch/values").json() == ["a", "c"]
        defs = logged_in_client.get("/api/label-definitions").json()
        match = next(d for d in defs if d["name"] == "opts_patch")
        assert match["options"] == ["a", "c"]

    def test_patch_options_prunes_inline_values(self, logged_in_client):
        """The submitted list is authoritative over the merged vocabulary, so it
        can also remove values that only ever existed in label_value_options."""
        logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "opts_inline",
                "level": "patient",
                "datatype": "select",
                "options": ["preset"],
            },
        )
        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "P-0001",
                "label": "opts_inline",
                "value": "typed",
            },
        )
        assert logged_in_client.get("/api/labels/opts_inline/values").json() == [
            "preset",
            "typed",
        ]
        defs = logged_in_client.get("/api/label-definitions").json()
        label_id = next(d for d in defs if d["name"] == "opts_inline")["id"]
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": ["preset"]},
        )
        assert resp.status_code == 200
        assert logged_in_client.get("/api/labels/opts_inline/values").json() == ["preset"]

    def test_patch_options_removing_in_use_value_keeps_annotation(
        self, logged_in_client, db_conn
    ):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "opts_inuse",
                "level": "patient",
                "datatype": "select",
                "options": ["keep", "drop"],
            },
        )
        label_id = create.json()["id"]
        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "P-0001",
                "label": "opts_inuse",
                "value": "drop",
            },
        )
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": ["keep"]},
        )
        assert resp.status_code == 200
        assert logged_in_client.get("/api/labels/opts_inuse/values").json() == ["keep"]
        # The annotation itself is untouched — removal only curates the pick list.
        with db_conn.cursor() as cur:
            cur.execute("SELECT value FROM annotations WHERE label = 'opts_inuse'")
            assert [r[0] for r in cur.fetchall()] == ["drop"]

    def test_patch_options_trims_and_dedupes(self, logged_in_client):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "opts_norm", "level": "series", "datatype": "select"},
        )
        label_id = create.json()["id"]
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": [" a ", "a", "", "b"]},
        )
        assert resp.status_code == 200
        assert resp.json()["options"] == ["a", "b"]

    def test_patch_options_on_non_select_returns_400(self, logged_in_client):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "opts_bool", "level": "series", "datatype": "bool"},
        )
        label_id = create.json()["id"]
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": ["a"]},
        )
        assert resp.status_code == 400

    def test_patch_options_blocked_by_policy_no_admin_bypass(self, logged_in_client):
        """edit_policy 'nobody' locks option editing even for the admin owner —
        same no-bypass rule as value writes."""
        create = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "opts_locked", "level": "series", "datatype": "select"},
        )
        label_id = create.json()["id"]
        assert (
            logged_in_client.patch(
                f"/api/label-definitions/{label_id}",
                json={"edit_policy": "nobody", "edit_users": []},
            ).status_code
            == 200
        )
        resp = logged_in_client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": ["a"]},
        )
        assert resp.status_code == 403

    def test_patch_options_users_policy_gates_by_membership(
        self, logged_in_client, client
    ):
        create = logged_in_client.post(
            "/api/label-definitions",
            json={"name": "opts_users", "level": "series", "datatype": "select"},
        )
        label_id = create.json()["id"]
        assert (
            logged_in_client.patch(
                f"/api/label-definitions/{label_id}",
                json={"edit_policy": "users", "edit_users": [USER_LVO]},
            ).status_code
            == 200
        )
        # The admin owner is not listed → 403 (no bypass) …
        assert (
            logged_in_client.patch(
                f"/api/label-definitions/{label_id}",
                json={"options": ["a"]},
            ).status_code
            == 403
        )
        # … while the listed user may edit the vocabulary.
        login_as(client, USER_LVO)
        resp = client.patch(
            f"/api/label-definitions/{label_id}",
            json={"options": ["a"]},
        )
        assert resp.status_code == 200
        assert resp.json()["options"] == ["a"]

    def test_value_usage_counts(self, logged_in_client):
        logged_in_client.post(
            "/api/label-definitions",
            json={
                "name": "usage_lbl",
                "level": "patient",
                "datatype": "select",
                "options": ["u1", "u2"],
            },
        )
        logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "P-0001",
                "label": "usage_lbl",
                "value": "u1",
            },
        )
        resp = logged_in_client.get("/api/labels/usage_lbl/value-usage")
        assert resp.status_code == 200
        assert resp.json() == {"u1": 1}

    def test_instruments_endpoint_returns_distinct_with_counts(self, logged_in_client):
        for name, instr in [
            ("instr_a1", "Alpha"),
            ("instr_a2", "Alpha"),
            ("instr_b1", "Beta"),
        ]:
            logged_in_client.post(
                "/api/label-definitions",
                json={
                    "name": name,
                    "level": "series",
                    "datatype": "bool",
                    "instrument": instr,
                },
            )
        resp = logged_in_client.get("/api/instruments")
        assert resp.status_code == 200
        rows = {r["name"]: r["count"] for r in resp.json()}
        assert rows.get("Alpha", 0) >= 2
        assert rows.get("Beta", 0) >= 1
