"""Autogenerate scope: the tables Alembic does not manage.

Used by ``alembic/env.py``'s ``include_object``; kept here (importable without
running migrations) so the rules are unit-tested.
"""

from __future__ import annotations

# Tables owned outside the web app's migration scope. Listed by name only —
# they all live in the `public` schema. See workstream 04 §2.
UPSTREAM_TABLES = frozenset({
    "clinical_data",
    "image_series",
    "image_study",
    # Pre-0020 name of clinical_data, kept so --autogenerate against a
    # not-yet-migrated DB doesn't draft a DROP of the clinical side-table.
    "lvo_clinical_data",
    "patient",
})

# Tables created/maintained by labelled_table_sync.py at runtime based on
# label_definitions. Their shape changes with annotations, not with code, so
# Alembic must not try to manage them. (snapshot_* retired in revision 0013.)
LABELLED_TABLES = frozenset({
    "image_series_labelled",
    "image_study_labelled",
    "patient_labelled",
})

EXCLUDED_TABLES = UPSTREAM_TABLES | LABELLED_TABLES


# Per-dataset clinical tables (Alembic 0027) are uploaded by
# scripts/admin/manage_datasets.py import-clinical, which enforces this suffix.
CLINICAL_TABLE_SUFFIX = "_clinical_data"


def _excluded(name) -> bool:
    return bool(name) and (name in EXCLUDED_TABLES or name.endswith(CLINICAL_TABLE_SUFFIX))


def include_object(object_, name, type_, reflected, compare_to):
    """Filter callback for --autogenerate.

    Skip tables we don't own and any indexes/constraints attached to them,
    so autogen drafts only touch web-app-owned tables.
    """
    if type_ == "table" and _excluded(name):
        return False
    if type_ in ("index", "unique_constraint", "foreign_key_constraint"):
        table_name = getattr(object_, "table", None)
        table_name = getattr(table_name, "name", None) if table_name else None
        if _excluded(table_name):
            return False
    return True
