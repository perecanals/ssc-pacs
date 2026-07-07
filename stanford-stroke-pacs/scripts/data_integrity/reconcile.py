#!/usr/bin/env python3
"""Two-DB reconciliation CLI.

Compares ``image_series`` (stanford-stroke DB) against the Orthanc index,
checks that referenced paths exist on disk, and emits a human-readable
summary or machine-readable JSON report.

Usage:
    python scripts/reconcile.py               # human-readable summary
    python scripts/reconcile.py --json         # write JSON report and print path
    python scripts/reconcile.py --json --quiet # JSON only, no stdout
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG, get_conn  # noqa: E402
from metrics import update_reconciliation_metrics  # noqa: E402
from reconciliation import diff_image_series_vs_orthanc, snapshot_summary  # noqa: E402

REPORTS_DIR = REPO_ROOT.parent / "maintenance" / "reconciliation-reports"
MAX_REPORTS = 30  # rotate: keep only the most recent N reports


def _rotate_reports() -> None:
    """Delete old reports beyond MAX_REPORTS (oldest first)."""
    if not REPORTS_DIR.is_dir():
        return
    reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda p: p.name)
    while len(reports) > MAX_REPORTS:
        reports.pop(0).unlink()


def _print_human(report: dict) -> None:
    s = report.get("summary", {})
    print("=== Two-DB Reconciliation Report ===")
    print(f"  Timestamp:                 {report.get('timestamp', '?')}")
    print(f"  Duration:                  {report.get('duration_seconds', '?')}s")
    print(f"  Series in DB:              {s.get('db_series_count', '?')}")
    print(f"  Series in Orthanc:         {s.get('orthanc_series_count', '?')}")
    print(f"  Matched:                   {s.get('matched', '?')}")
    print(f"  Coverage:                  {s.get('coverage_percent', '?')}%")
    print()
    print("--- Mismatches ---")
    print(f"  In DB, not in Orthanc:     {s.get('in_db_not_in_orthanc', 0)}")
    print(f"  In Orthanc, not in DB:     {s.get('in_orthanc_not_in_db', 0)}")
    print(f"  dicom_archive_path missing:{s.get('dicom_archive_missing', 0)}")
    total = s.get("total_mismatches", 0)
    print(f"  Total mismatches:          {total}")

    # Show first few entries per category
    for cat, label in [
        ("in_db_not_in_orthanc", "In DB, not in Orthanc"),
        ("in_orthanc_not_in_db", "In Orthanc, not in DB"),
        ("dicom_archive_missing", "dicom_archive_path missing on disk"),
    ]:
        items = report.get("mismatches", {}).get(cat, [])
        if items:
            print(f"\n--- {label} (first 20 of {len(items)}) ---")
            for entry in items[:20]:
                uid = entry.get("seriesinstanceuid", "?")
                pid = entry.get("patient_id", "?")
                print(f"  {pid}  {uid}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="Write JSON report to maintenance/reconciliation-reports/")
    ap.add_argument("--quiet", action="store_true", help="Suppress stdout (useful in cron)")
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    conn = get_conn()
    try:
        report = diff_image_series_vs_orthanc(conn)
    finally:
        conn.close()

    # Update Prometheus gauges (written to the shared registry; the
    # web-app /metrics endpoint will pick them up on next scrape).
    summary = snapshot_summary(report)
    update_reconciliation_metrics(summary, report.get("duration_seconds", 0))

    if args.json:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H%M%S")
        out_path = REPORTS_DIR / f"{ts}.json"
        out_path.write_text(json.dumps(report, indent=2, default=str))
        _rotate_reports()
        if not args.quiet:
            print(f"Report written to {out_path}")
        return 0

    if not args.quiet:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
