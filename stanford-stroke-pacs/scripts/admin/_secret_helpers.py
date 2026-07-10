"""Shared secret-handling helpers for the admin scripts.

Centralizes the mechanics that ``manage_users.py`` and the credential-rotation
scripts (``rotate_service_account.py``, ``rotate_db_password.py``) all need:
hidden password prompting, strong password generation, atomic rewrites of a
single ``.env`` variable, and the ``orthanc_users.json`` read/write helpers.

Keeping these in one place means the secret-writing logic (and its edge-case
handling) lives in exactly one spot.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"
ORTHANC_USERS_FILE = REPO_ROOT / "orthanc_users.json"


# -- Password input ------------------------------------------------------------

def prompt_password() -> str:
    """Hidden password prompt with confirmation and minimum-length check."""
    while True:
        p1 = getpass.getpass("Password: ")
        if len(p1) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            continue
        p2 = getpass.getpass("Confirm password: ")
        if p1 == p2:
            return p1
        print("Passwords do not match. Try again.", file=sys.stderr)


def generate_password(nbytes: int = 24) -> str:
    """Return a strong URL-safe random password.

    ``token_urlsafe`` yields only ``[A-Za-z0-9_-]``, so the value is free of
    quotes/backslashes and safe to store inside ``.env`` single-quoting.
    """
    return secrets.token_urlsafe(nbytes)


# -- .env single-variable rewrite ----------------------------------------------

def rewrite_env_var(env_file: Path, name: str, value: str) -> None:
    """Rewrite ``NAME='value'`` in *env_file* (idempotent; appends if absent).

    Uses a replacement *function* rather than a replacement string so a
    backslash or other regex-significant character in *value* is written
    literally instead of being interpreted as a backreference/escape.
    """
    if "'" in value:
        print(
            f"Warning: value for {name} contains a single quote; "
            "it may not round-trip through .env single-quoting.",
            file=sys.stderr,
        )
    text = env_file.read_text()
    new_text, count = re.subn(
        rf"^{re.escape(name)}=.*$",
        lambda _m: f"{name}='{value}'",
        text,
        flags=re.MULTILINE,
    )
    if count == 0:
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += f"{name}='{value}'\n"
    env_file.write_text(new_text)


# -- Orthanc users JSON --------------------------------------------------------

def orthanc_admin_user() -> str:
    return os.getenv("ORTHANC_ADMIN_USER", "admin")


def load_orthanc_users() -> dict[str, str]:
    if ORTHANC_USERS_FILE.exists():
        data = json.loads(ORTHANC_USERS_FILE.read_text())
        return data.get("RegisteredUsers", {})
    return {}


def save_orthanc_users(users: dict[str, str]) -> None:
    ORTHANC_USERS_FILE.write_text(
        json.dumps({"RegisteredUsers": users}, indent=2) + "\n"
    )
    os.chmod(ORTHANC_USERS_FILE, 0o600)


def upsert_orthanc_user(username: str, password: str) -> None:
    users = load_orthanc_users()
    users[username] = password
    save_orthanc_users(users)


def remove_orthanc_user(username: str) -> None:
    users = load_orthanc_users()
    if users.pop(username, None) is not None:
        save_orthanc_users(users)
