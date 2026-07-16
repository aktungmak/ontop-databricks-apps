"""Unity Catalog metadata APIs (on-behalf-of user token)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TypeVar

from databricks.sdk.errors import DatabricksError
from fastapi import APIRouter, HTTPException, Query, Request

from obo import get_obo_client

logger = logging.getLogger(__name__)
router = APIRouter()

T = TypeVar("T")


def _sdk_error_to_http(exc: DatabricksError) -> HTTPException:
    message = str(exc).strip()
    if message in ("", "None"):
        message = "Databricks API request failed"
    status = getattr(exc, "status_code", None) or 502
    if status == 401:
        message = (
            "Databricks rejected the user token. Re-open the app in Databricks "
            "and approve user authorization scopes."
        )
    elif status == 403:
        message = (
            "Access denied listing Unity Catalog metadata. Check your UC grants "
            "and that the app has catalog.*:read user_api_scopes."
        )
    elif status == 404:
        message = f"Unity Catalog object not found: {message}"
    return HTTPException(status_code=status, detail=message)


async def _run_sdk(fn: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(fn)
    except HTTPException:
        raise
    except DatabricksError as exc:
        logger.warning("Unity Catalog API error: %s", exc)
        raise _sdk_error_to_http(exc) from exc
    except Exception as exc:
        logger.exception("Unexpected Unity Catalog API error")
        raise HTTPException(
            status_code=500,
            detail=str(exc) or "Unity Catalog request failed",
        ) from exc


@router.get("/catalogs")
async def list_catalogs(request: Request) -> dict[str, list[str]]:
    client = get_obo_client(request)

    def _list() -> list[str]:
        return sorted(c.name for c in client.catalogs.list() if c.name)

    names = await _run_sdk(_list)
    return {"catalogs": names}


@router.get("/schemas")
async def list_schemas(
    request: Request,
    catalog: str = Query(..., min_length=1),
) -> dict[str, list[str]]:
    client = get_obo_client(request)

    def _list() -> list[str]:
        return sorted(
            s.name for s in client.schemas.list(catalog_name=catalog) if s.name
        )

    names = await _run_sdk(_list)
    return {"schemas": names}


@router.get("/tables")
async def list_tables(
    request: Request,
    catalog: str = Query(..., min_length=1),
    schema: str = Query(..., min_length=1),
) -> dict[str, list[str]]:
    client = get_obo_client(request)

    def _list() -> list[str]:
        return sorted(
            t.name
            for t in client.tables.list(catalog_name=catalog, schema_name=schema)
            if t.name
        )

    names = await _run_sdk(_list)
    return {"tables": names}


@router.get("/columns")
async def list_columns(
    request: Request,
    catalog: str = Query(..., min_length=1),
    schema: str = Query(..., min_length=1),
    table: str = Query(..., min_length=1),
) -> dict[str, list[dict[str, str | None]]]:
    client = get_obo_client(request)
    full_name = f"{catalog}.{schema}.{table}"

    def _list() -> list[dict[str, str | None]]:
        info = client.tables.get(full_name)
        columns: list[dict[str, str | None]] = []
        for col in info.columns or []:
            entry: dict[str, str | None] = {
                "name": col.name,
                "type": col.type_name or col.type_text or "unknown",
            }
            if col.comment:
                entry["comment"] = col.comment
            columns.append(entry)
        columns.sort(key=lambda c: c["name"] or "")
        return columns

    columns = await _run_sdk(_list)
    return {"columns": columns}
