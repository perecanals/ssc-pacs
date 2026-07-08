"""Thin wrapper around Orthanc REST API calls used by the web app.

Single source of truth for the Orthanc service-account credentials
(ORTHANC_URL / ORTHANC_USER / ORTHANC_PASS) — reconciliation.py and
routes/proxy.py import them from here.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from db import require_env  # importing db loads .env

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = require_env("ORTHANC_ADMIN_USER")
ORTHANC_PASS = require_env("ORTHANC_ADMIN_PASSWORD")


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
