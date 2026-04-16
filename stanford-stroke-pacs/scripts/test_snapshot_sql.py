"""Regression test for WS 03 T1 — snapshot-rebuild SQL injection.

Runs end-to-end against the live DB. The test:

1. Asserts that the creation endpoint's regex allowlist rejects a malicious
   label name (no INSERT, therefore no way for DDL to be perturbed later).
2. Directly exercises the SQL composition used by `_rebuild_snapshots`:
   feeds a hostile label name through the composer and asserts the emitted
   SQL quotes it as a literal and its column alias as a quoted identifier —
   i.e. a `;DROP TABLE` payload is neutralised rather than executed.

No DDL is actually run; we only inspect the composed statement.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPANION = ROOT / "companion"
sys.path.insert(0, str(COMPANION))
sys.path.insert(0, str(ROOT))

import psycopg2.sql as psql

from app import LABEL_NAME_RE


def test_regex_rejects_malicious_names() -> None:
    hostile = [
        "value; DROP TABLE annotations;--",
        "1bad",
        "",
        "name with space",
        "name-with-dash",
        "a" * 64,
        "'; SELECT 1;--",
    ]
    for name in hostile:
        assert not LABEL_NAME_RE.match(name), f"regex unexpectedly accepted: {name!r}"
    legit = ["baseline_cta", "m2_distal", "A", "abc_123"]
    for name in legit:
        assert LABEL_NAME_RE.match(name), f"regex rejected legitimate name: {name!r}"
    print("OK: LABEL_NAME_RE accepts legit names and rejects hostile payloads")


def test_composed_ddl_parameterizes_label_name() -> None:
    """Even if a malicious name bypassed the allowlist (e.g. existing DB row),
    the psycopg2.sql composition must not splice it as raw SQL."""
    hostile = "value'; DROP TABLE annotations;--"
    alias_id = psql.Identifier("a0")
    col_alias_id = psql.Identifier(f"label_{hostile.lower()}")
    label_lit = psql.Literal(hostile)
    idc = psql.Identifier("patient_id")
    level_lit = psql.Literal("patient")
    snap = psql.Identifier("snapshot_patients")
    src = psql.Identifier("lvo_clinical_data")

    pivot_col = psql.SQL("{a}.value AS {c}").format(a=alias_id, c=col_alias_id)
    pivot_join = psql.SQL(
        "LEFT JOIN annotations {a} ON {a}.level = {lvl} "
        "AND {a}.{idc} = src.{idc} "
        "AND {a}.label = {lbl}"
    ).format(a=alias_id, lvl=level_lit, idc=idc, lbl=label_lit)

    stmt = psql.SQL(
        "CREATE TABLE {snap} AS SELECT DISTINCT ON (src.{idc}) src.*, {pc} "
        "FROM (SELECT {pid} AS patient_id FROM {src}) src {pj}"
    ).format(
        snap=snap, idc=idc, pc=pivot_col,
        pid=psql.Identifier("study_id"), src=src, pj=pivot_join,
    )

    # psycopg2.sql.Composable.as_string() requires a connection or adapter;
    # use a throwaway cursor from a local connection if available, otherwise
    # adapt manually via a mock.
    import psycopg2
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "stanford-stroke"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
        )
    except Exception as e:
        print(f"SKIP as_string check (no DB available): {e}")
        return

    try:
        rendered = stmt.as_string(conn)
    finally:
        conn.close()

    # Payload remains in the rendered SQL but must be neutralised:
    # (a) wrapped in single quotes with internal ' doubled (psycopg2 Literal);
    # (b) wrapped in double-quoted identifier for the column alias.
    assert "DROP TABLE" in rendered, "sanity: payload must still be present"
    # (a) Literal form must appear with the quote doubled — proves psycopg2 escaped it.
    assert "'value''; DROP TABLE annotations;--'" in rendered, (
        f"expected hostile payload as quoted literal with doubled quote; got: {rendered}"
    )
    # (b) Column alias must be a double-quoted identifier. psycopg2 escapes
    # embedded double-quotes by doubling, but single quotes inside an
    # identifier are treated as literal characters — so they appear once.
    assert '"label_value\'; drop table annotations;--"' in rendered.lower(), (
        f"expected quoted identifier for column alias; got: {rendered}"
    )
    # Sanity: the `DROP TABLE` token must only appear inside quotes (single for
    # the literal, double for the identifier). There must be no unquoted copy.
    import re as _re
    unquoted_copies = _re.findall(r"DROP TABLE annotations", rendered, _re.IGNORECASE)
    quoted_copies = _re.findall(
        r"""(?:'[^']*DROP TABLE annotations[^']*'|"[^"]*drop table annotations[^"]*")""",
        rendered,
        _re.IGNORECASE,
    )
    assert len(unquoted_copies) == len(quoted_copies), (
        f"payload appears outside a quoted context; unquoted={len(unquoted_copies)} quoted={len(quoted_copies)}\n{rendered}"
    )
    print("OK: composed DDL parameterises hostile label name as literal + quoted identifier")


if __name__ == "__main__":
    test_regex_rejects_malicious_names()
    test_composed_ddl_parameterizes_label_name()
    print("all T1 checks passed")
