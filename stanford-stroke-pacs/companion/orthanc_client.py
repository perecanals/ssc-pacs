"""Thin wrapper around Orthanc REST API calls used by the companion."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

from db import _require_env  # noqa: E402

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = _require_env("ORTHANC_ADMIN_USER")
ORTHANC_PASS = _require_env("ORTHANC_ADMIN_PASSWORD")


def orthanc_lookup(studyinstanceuid: str, *, timeout: int = 5) -> list[dict[str, Any]]:
    """POST /tools/lookup — resolve a DICOM UID to Orthanc internal IDs."""
    resp = requests.post(
        f"{ORTHANC_URL}/tools/lookup",
        data=studyinstanceuid,
        auth=(ORTHANC_USER, ORTHANC_PASS),
        timeout=timeout,
    )
    if resp.status_code != 200:
        return []
    return resp.json()


def orthanc_system_check(*, timeout: int = 3) -> tuple[str, str | None]:
    """GET /system — returns ``("ok", None)`` or ``("error", detail)``."""
    try:
        resp = requests.get(
            f"{ORTHANC_URL}/system",
            auth=(ORTHANC_USER, ORTHANC_PASS),
            timeout=timeout,
        )
        if resp.status_code == 200:
            return "ok", None
        return f"http_{resp.status_code}", None
    except Exception as e:
        return "error", str(e)[:200]
