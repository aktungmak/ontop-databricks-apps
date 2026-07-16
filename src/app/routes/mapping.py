"""Live mapping read (service principal — READ_VOLUME only)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from volume_files import download_volume_file

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/live")
async def get_live_mapping(request: Request) -> dict[str, str]:
    """Read the deployed mapping.ttl from the UC volume (SP credentials)."""
    settings = request.app.state.settings
    client = request.app.state.sp_client
    volume_base = settings.mappings_volume_path.rstrip("/")
    remote_path = f"{volume_base}/{settings.mapping_file}"
    dest = settings.work_dir / "live-mapping" / settings.mapping_file

    try:
        download_volume_file(client, remote_path, dest)
        turtle = dest.read_text(encoding="utf-8")
    except Exception as exc:
        logger.exception("Failed to read live mapping from %s", remote_path)
        raise HTTPException(
            status_code=502,
            detail=f"Could not read live mapping: {exc}",
        ) from exc

    return {"turtle": turtle, "filename": settings.mapping_file}
