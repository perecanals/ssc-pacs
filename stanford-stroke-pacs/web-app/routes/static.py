"""SPA fallback — serve React app for all non-API routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

DIST_DIR = Path(__file__).resolve().parent.parent / "dist"


def _serve_index():
    index = DIST_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=503, detail="Frontend not built — run npm run build")


@router.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    # Unknown API paths must not masquerade as the SPA shell (200 + HTML).
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return _serve_index()
