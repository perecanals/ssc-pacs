"""scripts/admin/manage_datasets.py: the dataset registry (Alembic 0026)."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from db import get_conn

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "manage_datasets.py"


@pytest.fixture()
def mod(client):
    return runpy.run_path(str(_SCRIPT))


def _q(sql, params=()):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


@pytest.fixture()
def saved_references():
    """A saved session filter naming crisp2 (user_crisp's grant is seeded)."""
    _q("INSERT INTO user_preferences (username, level, prefs) VALUES "
       "('user_crisp', '_global', %s) ON CONFLICT (username, level) DO UPDATE "
       "SET prefs = EXCLUDED.prefs",
       (json.dumps({"session": {"filters": {"dataset": "crisp2"}}}),))
    yield
    _q("DELETE FROM user_preferences WHERE username = 'user_crisp' AND level = '_global'")


def test_add_validates_slug_and_uniqueness(mod):
    conn = get_conn()
    try:
        for slug, name in (("Bad Slug", "X"), ("lvo", "other"), ("fresh", "lvo")):
            with pytest.raises(SystemExit):
                mod["_add"](conn, slug, name)
    finally:
        conn.close()


@pytest.mark.usefixtures("saved_references")
def test_rename_dry_run_then_execute_then_back(mod):
    conn = get_conn()
    try:
        mod["_rename"](conn, "crisp2", "CRISP2 renamed", execute=False)
    finally:
        conn.close()
    assert _q("SELECT name FROM dataset WHERE slug = 'crisp2'") == [("crisp2",)]

    def check(name):
        assert _q("SELECT dataset FROM patient WHERE patient_key = 'crisp2__P-0001'") == [(name,)]
        assert _q("SELECT allowed_datasets FROM users WHERE username = 'user_crisp'") == [([name],)]
        assert _q("SELECT prefs #>> '{session,filters,dataset}' FROM user_preferences "
                  "WHERE username = 'user_crisp' AND level = '_global'") == [(name,)]
        assert _q("SELECT dataset FROM patient_labelled "
                  "WHERE patient_key = 'crisp2__P-0001'") == [(name,)]

    for name in ("CRISP2 renamed", "crisp2"):
        conn = get_conn()
        try:
            mod["_rename"](conn, "crisp2", name, execute=True)
        finally:
            conn.close()
        check(name)
        # The slug, and so every key, never changes.
        assert _q("SELECT slug FROM dataset WHERE name = %s", (name,)) == [("crisp2",)]
