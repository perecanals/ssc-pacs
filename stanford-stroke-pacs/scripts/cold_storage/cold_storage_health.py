#!/usr/bin/env python3
"""Cold-storage health probe — surfaces the failure modes WS 05 hardens against.

Prints (and optionally emits JSON for) the operational signals that
indicate whether `cache_state` and the on-disk `legacy_dicom_root` are
in agreement:

  * Stuck-warming rows: status='warming' with `warming_started_at`
    older than `WARMING_TIMEOUT_MINUTES` (the watchdog should have
    cleared these on the next warm — if a row is here, no warm has
    been attempted since the timeout).
  * Orphaned `*.warming` directories on disk: temp dirs left by a
    crashed extraction. Should normally be zero.
  * Free disk space on the legacy_dicom_root mount.
  * Distribution of `last_accessed_at` across hot rows (eviction
    pressure indicator).

Exit code is non-zero if any "critical" condition is met:
  * any stuck-warming row, OR
  * any orphaned `.warming` dir, OR
  * free disk space below `--min-free-bytes` (default = 5 GiB).

Use --json to get machine-parseable output for monitoring tools.
Use --quiet to suppress human output.

The script is read-only — it does not clear stuck rows or delete
orphan dirs. See `documentation/cold_storage/runbook.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from config import (  # noqa: E402
    LEGACY_DICOM_ROOT,
    STORAGE_MODE,
    WARMING_TIMEOUT_MINUTES,
)
from db import DB_CONFIG, get_conn  # noqa: E402

DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB


def _connect():
    if not DB_CONFIG["user"] or not DB_CONFIG["password"]:
        raise SystemExit("DB_USER and DB_PASSWORD must be set in stanford-stroke-pacs/.env")
    return psycopg2.connect(**DB_CONFIG)


def _stuck_warming_rows(conn) -> list[dict[str, Any]]:
    """Rows in 'warming' past the watchdog timeout."""
    timeout_minutes = float(WARMING_TIMEOUT_MINUTES)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT studyinstanceuid, warming_started_at,
                   EXTRACT(EPOCH FROM (now() - warming_started_at))::bigint AS age_seconds,
                   error_message
            FROM cache_state
            WHERE status = 'warming'
              AND (warming_started_at IS NULL
                   OR warming_started_at < (now() - (%s * interval '1 minute')))
            ORDER BY warming_started_at NULLS FIRST
            """,
            (timeout_minutes,),
        )
        return [dict(r) for r in cur.fetchall()]


def _orphan_warming_dirs(legacy_root: Path) -> list[str]:
    """Find on-disk `*.warming` siblings under legacy_root.

    Walks the legacy tree and collects directories whose name ends with
    `.warming`. Returns the absolute paths so an operator can investigate
    or `rm -rf` them. (A single warm in flight will normally have one,
    so a small non-zero count immediately after a known-active warm is
    not necessarily critical — the cron timer of 15min smooths that.)
    """
    if not legacy_root.exists():
        return []
    out: list[str] = []
    # rglob is fine here; the legacy tree is bounded and we only match dirs.
    for p in legacy_root.rglob("*.warming"):
        try:
            if p.is_dir():
                out.append(str(p))
        except OSError:
            # Permission/race during the walk — skip.
            continue
    return out


def _disk_free(legacy_root: Path) -> dict[str, int]:
    p = legacy_root if legacy_root.exists() else legacy_root.parent
    usage = shutil.disk_usage(p)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def _last_accessed_distribution(conn) -> dict[str, int]:
    """Bucket hot rows by how long ago they were last accessed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'hot' AND last_accessed_at >= now() - interval '1 hour')   AS last_1h,
              COUNT(*) FILTER (WHERE status = 'hot' AND last_accessed_at >= now() - interval '24 hours'
                               AND last_accessed_at <  now() - interval '1 hour')                        AS last_24h,
              COUNT(*) FILTER (WHERE status = 'hot' AND last_accessed_at >= now() - interval '7 days'
                               AND last_accessed_at <  now() - interval '24 hours')                      AS last_7d,
              COUNT(*) FILTER (WHERE status = 'hot' AND (last_accessed_at IS NULL
                               OR last_accessed_at < now() - interval '7 days'))                         AS older_or_null,
              COUNT(*) FILTER (WHERE status = 'cold')                                                    AS cold,
              COUNT(*) FILTER (WHERE status = 'error')                                                   AS error,
              COUNT(*)                                                                                   AS total
            FROM cache_state
            """
        )
        row = cur.fetchone()
    return {
        "hot_last_1h": int(row[0] or 0),
        "hot_last_24h": int(row[1] or 0),
        "hot_last_7d": int(row[2] or 0),
        "hot_older_or_null": int(row[3] or 0),
        "cold": int(row[4] or 0),
        "error": int(row[5] or 0),
        "total": int(row[6] or 0),
    }


def collect(min_free_bytes: int) -> dict[str, Any]:
    legacy = Path(LEGACY_DICOM_ROOT)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "storage_mode": STORAGE_MODE,
        "warming_timeout_minutes": float(WARMING_TIMEOUT_MINUTES),
        "legacy_dicom_root": str(legacy),
        "stuck_warming": [],
        "orphan_warming_dirs": [],
        "disk": {},
        "cache_state_distribution": {},
        "critical": False,
        "critical_reasons": [],
    }

    if STORAGE_MODE != "cold_path_cache":
        report["note"] = (
            f"Storage mode is '{STORAGE_MODE}' — cold-storage checks are not "
            "applicable. Reporting structural data only."
        )

    conn = _connect()
    try:
        report["stuck_warming"] = _stuck_warming_rows(conn)
        report["cache_state_distribution"] = _last_accessed_distribution(conn)
    finally:
        conn.close()

    report["orphan_warming_dirs"] = _orphan_warming_dirs(legacy)
    report["disk"] = _disk_free(legacy)
    report["disk"]["min_free_bytes_threshold"] = int(min_free_bytes)

    reasons: list[str] = []
    if report["stuck_warming"]:
        reasons.append(f"stuck_warming_rows={len(report['stuck_warming'])}")
    if report["orphan_warming_dirs"]:
        reasons.append(f"orphan_warming_dirs={len(report['orphan_warming_dirs'])}")
    if report["disk"]["free"] < min_free_bytes:
        reasons.append(
            f"disk_free_below_threshold(free={report['disk']['free']},threshold={min_free_bytes})"
        )
    report["critical"] = bool(reasons)
    report["critical_reasons"] = reasons
    return report


def _human(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"Cold-storage health report ({report['generated_at']})")
    lines.append(f"  storage_mode             : {report['storage_mode']}")
    lines.append(f"  legacy_dicom_root        : {report['legacy_dicom_root']}")
    lines.append(f"  warming_timeout_minutes  : {report['warming_timeout_minutes']}")

    disk = report["disk"]
    free_gib = disk["free"] / (1024 ** 3)
    total_gib = disk["total"] / (1024 ** 3)
    threshold_gib = disk["min_free_bytes_threshold"] / (1024 ** 3)
    lines.append(
        f"  disk free                : {free_gib:.2f} GiB / {total_gib:.2f} GiB "
        f"(threshold {threshold_gib:.2f} GiB)"
    )

    dist = report["cache_state_distribution"]
    lines.append(
        "  cache_state              : total={total} hot_1h={hot_last_1h} "
        "hot_24h={hot_last_24h} hot_7d={hot_last_7d} hot_older={hot_older_or_null} "
        "cold={cold} error={error}".format(**dist)
    )

    stuck = report["stuck_warming"]
    lines.append(f"  stuck_warming_rows       : {len(stuck)}")
    for r in stuck[:10]:
        lines.append(
            f"      - {r['studyinstanceuid']}  age={r['age_seconds']}s  err={r.get('error_message')}"
        )
    if len(stuck) > 10:
        lines.append(f"      … {len(stuck) - 10} more")

    orphans = report["orphan_warming_dirs"]
    lines.append(f"  orphan_warming_dirs      : {len(orphans)}")
    for p in orphans[:10]:
        lines.append(f"      - {p}")
    if len(orphans) > 10:
        lines.append(f"      … {len(orphans) - 10} more")

    if report["critical"]:
        lines.append("CRITICAL: " + "; ".join(report["critical_reasons"]))
    else:
        lines.append("OK")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of human text")
    ap.add_argument("--quiet", action="store_true", help="suppress human output (exit code only)")
    ap.add_argument(
        "--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES,
        help=f"disk-free critical threshold in bytes (default {DEFAULT_MIN_FREE_BYTES})",
    )
    args = ap.parse_args()

    report = collect(args.min_free_bytes)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif not args.quiet:
        print(_human(report))

    return 1 if report["critical"] else 0


if __name__ == "__main__":
    sys.exit(main())
