"""Tests for the unified build_label_filter_sql() function.

Verifies that the single replacement produces identical SQL to the four
original helpers for every (entity_level, label_level) combination.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure companion/ is importable.
_COMPANION_DIR = Path(__file__).resolve().parent.parent
if str(_COMPANION_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPANION_DIR))

from common import VALID_LEVELS, build_label_filter_sql  # noqa: E402

# ---------------------------------------------------------------------------
# Reference SQL — the exact output the old helpers would produce.
# Each key is (entity_level, label_level).
# ---------------------------------------------------------------------------

# _label_filter_sql equivalents (operator="IN", no value_predicate)
_EXPECTED_EXISTS = {
    ("patient", "patient"): "{expr} IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)",
    ("patient", "study"): "{expr} IN (SELECT patient_id FROM image_study WHERE studyinstanceuid IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s))",
    ("patient", "series"): "{expr} IN (SELECT patient_id FROM image_series WHERE seriesinstanceuid IN (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s))",
    ("study", "patient"): "st.patient_id IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)",
    ("study", "study"): "{expr} IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s)",
    ("study", "series"): "{expr} IN (SELECT studyinstanceuid FROM image_series WHERE seriesinstanceuid IN (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s))",
    ("series", "patient"): "s.patient_id IN (SELECT patient_id FROM annotations WHERE level = 'patient' AND label = %s)",
    ("series", "study"): "s.studyinstanceuid IN (SELECT studyinstanceuid FROM annotations WHERE level = 'study' AND label = %s)",
    ("series", "series"): "{expr} IN (SELECT seriesinstanceuid FROM annotations WHERE level = 'series' AND label = %s)",
}

# _label_bool_filter_sql with exists=False (operator="NOT IN")
_EXPECTED_NOT_EXISTS = {
    k: v.replace(" IN (", " NOT IN (", 1)
    for k, v in _EXPECTED_EXISTS.items()
}

# _label_value_filter_sql (value_predicate="AND LOWER(COALESCE(value, '')) LIKE LOWER(%s)")
_VP_VALUE = "AND LOWER(COALESCE(value, '')) LIKE LOWER(%s)"
_EXPECTED_VALUE = {
    k: v.replace("AND label = %s)", f"AND label = %s {_VP_VALUE})")
    for k, v in _EXPECTED_EXISTS.items()
}

# _label_select_values_filter_sql (value_predicate="AND COALESCE(value, '') = ANY(%s)")
_VP_SELECT = "AND COALESCE(value, '') = ANY(%s)"
_EXPECTED_SELECT = {
    k: v.replace("AND label = %s)", f"AND label = %s {_VP_SELECT})")
    for k, v in _EXPECTED_EXISTS.items()
}


_ENTITY_EXPR = {
    "patient": "p.study_id",
    "study": "st.studyinstanceuid",
    "series": "s.seriesinstanceuid",
}


def _fmt(template: str, entity_level: str) -> str:
    return template.replace("{expr}", _ENTITY_EXPR[entity_level])


class TestBuildLabelFilterSQL:
    """Parametric tests covering all 9 (entity, label) level combos x 4 modes."""

    def test_exists_mode(self):
        for (el, ll), expected_template in _EXPECTED_EXISTS.items():
            expr = _ENTITY_EXPR[el]
            result = build_label_filter_sql(el, ll, expr)
            expected = _fmt(expected_template, el)
            assert result == expected, f"exists ({el},{ll}): {result!r} != {expected!r}"

    def test_not_exists_mode(self):
        for (el, ll), expected_template in _EXPECTED_NOT_EXISTS.items():
            expr = _ENTITY_EXPR[el]
            result = build_label_filter_sql(el, ll, expr, operator="NOT IN")
            expected = _fmt(expected_template, el)
            assert result == expected, f"not_exists ({el},{ll}): {result!r} != {expected!r}"

    def test_value_mode(self):
        for (el, ll), expected_template in _EXPECTED_VALUE.items():
            expr = _ENTITY_EXPR[el]
            result = build_label_filter_sql(el, ll, expr, value_predicate=_VP_VALUE)
            expected = _fmt(expected_template, el)
            assert result == expected, f"value ({el},{ll}): {result!r} != {expected!r}"

    def test_select_values_mode(self):
        for (el, ll), expected_template in _EXPECTED_SELECT.items():
            expr = _ENTITY_EXPR[el]
            result = build_label_filter_sql(el, ll, expr, value_predicate=_VP_SELECT)
            expected = _fmt(expected_template, el)
            assert result == expected, f"select ({el},{ll}): {result!r} != {expected!r}"

    def test_label_level_none_defaults_to_entity(self):
        """When label_level is None, it defaults to entity_level."""
        for el in VALID_LEVELS:
            expr = _ENTITY_EXPR[el]
            result_none = build_label_filter_sql(el, None, expr)
            result_same = build_label_filter_sql(el, el, expr)
            assert result_none == result_same, f"None vs same for {el}"

    def test_label_level_invalid_defaults_to_entity(self):
        """An invalid label_level falls back to entity_level."""
        for el in VALID_LEVELS:
            expr = _ENTITY_EXPR[el]
            result_bad = build_label_filter_sql(el, "bogus", expr)
            result_same = build_label_filter_sql(el, el, expr)
            assert result_bad == result_same, f"bogus vs same for {el}"
