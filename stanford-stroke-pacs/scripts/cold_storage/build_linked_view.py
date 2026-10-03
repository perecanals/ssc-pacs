#!/usr/bin/env python3
"""Rebuild the linked view: symlinks to imaging an enrollment shares but does not own.

    <linked_view_root>/<slug>/<patient_id>/<StudyUID> -> <cold_archive_root>/<owner's study dir>

A generated convenience view (web-app/linked_view.py), never a source of truth:
idempotent, removes stale and dangling links, leaves non-link files alone.
Configured by config.toml [storage].linked_view_root; unset = disabled.
Run automatically after link_patients.py / split_merged_patients.py /
move_to_dataset_layout.py apply.

Usage:
    python scripts/cold_storage/build_linked_view.py           # dry run
    python scripts/cold_storage/build_linked_view.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "web-app"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Write the links (default: dry run)")
    args = ap.parse_args()

    import linked_view
    from db import get_conn

    from config import COLD_ARCHIVE_ROOT, DICOM_DATA_ROOT, LINKED_VIEW_ROOT

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            result = linked_view.rebuild(cur, LINKED_VIEW_ROOT, DICOM_DATA_ROOT,
                                         COLD_ARCHIVE_ROOT, apply=args.apply)
    finally:
        conn.close()
    print(("" if args.apply else "DRY RUN — ") + linked_view.summary(result))
    for kind in ("created", "replaced", "removed", "foreign"):
        for item in result[kind][:5]:
            print(f"  {kind}: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
