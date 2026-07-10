#!/usr/bin/env python3
"""Rotate / verify the Orthanc service-account password.

The Orthanc service account is the credential the Web App's reverse proxy (and
host-local scripts) use for Basic auth against Orthanc on :8042. It lives in two
places that must stay in sync:

  1. ``ORTHANC_ADMIN_PASSWORD`` in ``.env`` (read by the Web App and scripts).
  2. The matching entry in ``orthanc_users.json`` (Orthanc's own auth store).

``rotate`` rewrites both atomically; ``check`` verifies they still agree.

Usage:
    python scripts/admin/rotate_service_account.py rotate [--generate]
    python scripts/admin/rotate_service_account.py check

The password never appears on the command line: ``rotate`` prompts for it
(hidden), or with ``--generate`` mints a strong random one and prints it once.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"
load_dotenv(ENV_FILE)

from _secret_helpers import (  # noqa: E402
    ORTHANC_USERS_FILE,
    generate_password,
    load_orthanc_users,
    orthanc_admin_user,
    prompt_password,
    rewrite_env_var,
    upsert_orthanc_user,
)


def cmd_rotate(args: argparse.Namespace) -> None:
    """Rotate the service-account password in .env + orthanc_users.json."""
    username = orthanc_admin_user()
    print(f"Rotating Orthanc service-account password for user '{username}'.")

    if args.generate:
        password = generate_password()
    else:
        password = prompt_password()

    rewrite_env_var(ENV_FILE, "ORTHANC_ADMIN_PASSWORD", password)
    upsert_orthanc_user(username, password)

    print(f"Service-account '{username}' rotated (.env + orthanc_users.json).")
    if args.generate:
        print(f"Generated password: {password}")
        print("Store it now — it will not be shown again.")
    print("Restart both services to pick up the new password:")
    print("  docker restart ssc-orthanc")
    print("  sudo systemctl restart ssc-web-app")


def cmd_check(_args: argparse.Namespace) -> None:
    """Verify .env and orthanc_users.json agree on the service-account password.

    The Web App proxy authenticates to Orthanc with ORTHANC_ADMIN_PASSWORD from
    .env; Orthanc accepts it because orthanc_users.json holds the same value.
    These two are the one config pair not enforced by code at runtime, so a
    manual edit to either file can silently break OHIF/DICOMweb. This is the
    detector. Exits non-zero on mismatch (usable from a healthcheck).
    """
    username = orthanc_admin_user()
    env_pw = os.getenv("ORTHANC_ADMIN_PASSWORD")
    json_pw = load_orthanc_users().get(username)

    problems = []
    if not env_pw:
        problems.append(f"ORTHANC_ADMIN_PASSWORD is unset/empty in {ENV_FILE}")
    if json_pw is None:
        problems.append(f"no entry for '{username}' in {ORTHANC_USERS_FILE}")
    if env_pw and json_pw is not None and env_pw != json_pw:
        problems.append(
            "ORTHANC_ADMIN_PASSWORD in .env does not match orthanc_users.json "
            f"for '{username}'"
        )

    if problems:
        print(f"Service-account '{username}': OUT OF SYNC", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "Fix with:  python scripts/admin/rotate_service_account.py rotate",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Service-account '{username}': .env and orthanc_users.json are in sync.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_rotate = sub.add_parser(
        "rotate",
        help="Rotate the service-account password (.env + orthanc_users.json)",
    )
    p_rotate.add_argument(
        "--generate", action="store_true",
        help="Mint a strong random password and print it once instead of prompting",
    )

    sub.add_parser(
        "check",
        help="Verify .env and orthanc_users.json agree on the password",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"rotate": cmd_rotate, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    main()
