"""Staff promotion is atomic and never changes dataset/password/Orthanc access."""

import argparse
import runpy
from pathlib import Path

import psycopg2
import pytest

from data_exports import database
from tests.conftest import TEST_USER, USER_CRISP, USER_LVO


@pytest.fixture()
def staff_command(client, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/admin/manage_users.py"
    monkeypatch.syspath_prepend(str(script.parent))
    module = runpy.run_path(str(script))
    return module["cmd_set_staff"]


def test_assign_staff_preserves_identity_and_dataset_grants(staff_command):
    users = [USER_CRISP, USER_LVO]
    before = database.records(
        "SELECT username, password_hash, allowed_datasets FROM users WHERE username=ANY(%s) ORDER BY username", (users,)
    )
    try:
        staff_command(argparse.Namespace(usernames=users, remove=False))
        assert all(
            row["is_staff"] and not row["is_admin"]
            for row in database.records("SELECT is_admin, is_staff FROM users WHERE username=ANY(%s)", (users,))
        )
        after = database.records(
            "SELECT username, password_hash, allowed_datasets FROM users WHERE username=ANY(%s) ORDER BY username",
            (users,),
        )
        # Do not include hashes in assertion output.
        identities_unchanged = before == after
        assert identities_unchanged, "Identity or dataset grants changed unexpectedly"
        staff_command(argparse.Namespace(usernames=users, remove=True))
        assert not any(
            row["is_staff"] for row in database.records("SELECT is_staff FROM users WHERE username=ANY(%s)", (users,))
        )
    finally:
        database.records("UPDATE users SET is_staff=false WHERE username=ANY(%s)", (users,))


@pytest.mark.parametrize("invalid", ["unknown-staff-account", TEST_USER])
def test_staff_assignment_is_all_or_nothing(staff_command, invalid):
    with pytest.raises(SystemExit):
        staff_command(argparse.Namespace(usernames=[USER_CRISP, invalid], remove=False))
    assert (
        database.records("SELECT is_staff FROM users WHERE username=%s", (USER_CRISP,), one=True)["is_staff"] is False
    )


def test_staff_and_admin_are_distinct_roles(client):
    with pytest.raises(psycopg2.errors.CheckViolation):
        database.records("UPDATE users SET is_staff=true WHERE username=%s", (TEST_USER,))
