"""Export capabilities and dataset grants, independent of the query UI."""

from fastapi import Depends

from auth import require_staff
from dataset_access import fetch_user_scope


def get_access(user: str = Depends(require_staff)):
    scope = fetch_user_scope(user)
    return {"user": user, "scope": None if scope is None else sorted(scope)}


def artifact_in_scope(job, scope):
    if scope is None:
        return True
    original = job["configuration"].get("authorized_datasets")
    return isinstance(original, list) and set(original).issubset(scope)
