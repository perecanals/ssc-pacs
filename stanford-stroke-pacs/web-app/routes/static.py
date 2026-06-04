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
    return _serve_index()
