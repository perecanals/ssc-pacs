"""Prometheus metrics for the companion service.

Metric catalogue (see `documentation/operations/observability.md`):

  * `http_requests_total{method, path_template, status}`
  * `http_request_duration_seconds{method, path_template}`
  * `cold_storage_warm_total{result}`
  * `cold_storage_evict_total{result}`
  * `cold_storage_warming_rows` (gauge, refreshed on scrape)
  * `cold_storage_disk_free_bytes` (gauge, refreshed on scrape)

No labels carry PHI or UIDs — `path_template` is the matched FastAPI
route template (e.g. `/api/studies/{studyinstanceuid}/warm`), not the
concrete path.
"""

from __future__ import annotations

import shutil

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests by method, matched path template, and status code.",
    labelnames=("method", "path_template", "status"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds by method and matched path template.",
    labelnames=("method", "path_template"),
    # Buckets tuned for a mix of fast JSON endpoints and the occasional
    # warm/evict / zip-stream request.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

cold_storage_warm_total = Counter(
    "cold_storage_warm_total",
    "Cold-storage warm attempts, labelled by result.",
    labelnames=("result",),
    registry=REGISTRY,
)

cold_storage_evict_total = Counter(
    "cold_storage_evict_total",
    "Cold-storage evict attempts, labelled by result.",
    labelnames=("result",),
    registry=REGISTRY,
)

cold_storage_warming_rows = Gauge(
    "cold_storage_warming_rows",
    "Number of cache_state rows currently in status='warming'.",
    registry=REGISTRY,
)

cold_storage_disk_free_bytes = Gauge(
    "cold_storage_disk_free_bytes",
    "Free bytes on the filesystem holding the legacy DICOM root.",
    registry=REGISTRY,
)


def refresh_cold_storage_gauges(get_conn, legacy_dicom_root) -> None:
    """Refresh the scrape-time gauges.

    Called from the /metrics handler so the values reflect current state
    rather than whatever was last written by the warm/evict code path.
    Exceptions here must never prevent /metrics from serving — we swallow
    them and leave the last-known value in place.
    """
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM cache_state WHERE status = 'warming'"
                )
                (warming,) = cur.fetchone()
                cold_storage_warming_rows.set(int(warming or 0))
        finally:
            conn.close()
    except Exception:
        pass

    try:
        # shutil.disk_usage walks up to the nearest existing ancestor so
        # this works even when legacy_dicom_root is transiently empty.
        p = legacy_dicom_root
        while not p.exists():
            if p.parent == p:
                break
            p = p.parent
        cold_storage_disk_free_bytes.set(shutil.disk_usage(p).free)
    except Exception:
        pass


__all__ = [
    "REGISTRY",
    "http_requests_total",
    "http_request_duration_seconds",
    "cold_storage_warm_total",
    "cold_storage_evict_total",
    "cold_storage_warming_rows",
    "cold_storage_disk_free_bytes",
    "refresh_cold_storage_gauges",
]
