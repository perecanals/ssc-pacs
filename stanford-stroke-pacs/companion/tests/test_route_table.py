"""Snapshot test: verify the route table is unchanged after the refactor."""

from __future__ import annotations

# FastAPI auto-generated routes to ignore (docs, openapi spec).
_FRAMEWORK_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def _collect_routes(app):
    """Return a sorted set of (methods, path) from the FastAPI app."""
    routes = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods and path not in _FRAMEWORK_PATHS:
            for m in sorted(methods):
                routes.add((m, path))
    return routes


# The expected route table — every (METHOD, path) the app must expose.
EXPECTED_ROUTES = {
    ("POST", "/api/login"),
    ("POST", "/api/logout"),
    ("GET", "/api/me"),
    ("GET", "/api/preferences/{level}"),
    ("PUT", "/api/preferences/{level}"),
    ("GET", "/api/patients"),
    ("GET", "/api/patients/{patient_id}/studies"),
    ("GET", "/api/study-import-labels"),
    ("GET", "/api/studies"),
    ("GET", "/api/studies/{studyinstanceuid}/series"),
    ("GET", "/api/series"),
    ("GET", "/api/ohif-link/{studyinstanceuid}"),
    ("GET", "/api/series/{seriesinstanceuid}/dicom-zip"),
    ("GET", "/api/storage-mode"),
    ("POST", "/api/studies/{studyinstanceuid}/warm"),
    ("POST", "/api/studies/{studyinstanceuid}/evict"),
    ("GET", "/api/studies/{studyinstanceuid}/cache-status"),
    ("GET", "/api/series/{seriesinstanceuid}/annotations"),
    ("POST", "/api/annotations"),
    ("DELETE", "/api/annotations/{annotation_id}"),
    ("GET", "/api/annotations/{annotation_id}/history"),
    ("GET", "/api/labels"),
    ("GET", "/api/labels/summary"),
    ("GET", "/api/labels/{label_name}/values"),
    ("GET", "/api/label-definitions"),
    ("POST", "/api/label-definitions"),
    ("GET", "/healthz"),
    ("GET", "/metrics"),
    ("GET", "/api/admin/reconciliation/latest"),
    ("POST", "/api/snapshots/refresh"),
    ("POST", "/api/labelled-tables/refresh"),
    ("GET", "/{full_path:path}"),
}


def test_route_table(client):
    """The full route table must match the pre-refactor snapshot."""
    import app as app_mod

    actual = _collect_routes(app_mod.app)
    missing = EXPECTED_ROUTES - actual
    extra = actual - EXPECTED_ROUTES
    assert not missing, f"Missing routes: {missing}"
    assert not extra, f"Extra routes: {extra}"
