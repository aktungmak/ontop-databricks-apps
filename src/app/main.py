"""Ontop VKG Databricks App — FastAPI entrypoint."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans

from config import Settings
from mcp_server import McpRuntime, configure as configure_mcp, mcp
from obo import get_user_token
from ontology_store import OntologyStore
from ontop_manager import OntopProcessManager
from routes.autogenerate import router as autogenerate_router
from routes.mapping import router as mapping_router
from routes.uc import router as uc_router
from sparql_execute import (
    SparqlExecuteError,
    SparqlExecuteSuccess,
    execute_sparql_query,
)

logging.basicConfig(
    level=logging.INFO,
    format="[APP] %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

settings = Settings.from_env()
ontop_manager = OntopProcessManager(settings)

# Streamable HTTP at mount path /mcp (internal path="/" avoids /mcp/mcp).
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def ontop_lifespan(app: FastAPI):
    """Prepare Ontop + load ontology cache; start Ontop; tear down on shutdown."""
    client = WorkspaceClient()
    app.state.settings = settings
    app.state.sp_client = client
    app.state.ontop_manager = ontop_manager

    logger.info("Preparing Ontop from volume %s", settings.mappings_volume_path)
    ontop_manager.prepare(client)
    ontop_manager.write_jdbc_properties()

    ontology_store = OntologyStore.load(ontop_manager.ontology_path)
    app.state.ontology_store = ontology_store
    logger.info(
        "Ontology store available=%s path=%s",
        ontology_store.is_available(),
        ontop_manager.ontology_path,
    )

    ontop_manager.start()
    http_client = httpx.AsyncClient(timeout=120.0)
    app.state.http_client = http_client

    configure_mcp(
        McpRuntime(
            ontology_store=ontology_store,
            ontop_manager=ontop_manager,
            settings=settings,
            http_client=http_client,
        )
    )
    logger.info("App initialisation complete")
    try:
        yield
    finally:
        ontop_manager.stop()
        await http_client.aclose()


app = FastAPI(
    title="Ontop VKG",
    lifespan=combine_lifespans(ontop_lifespan, mcp_app.lifespan),
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/mcp", mcp_app)
app.include_router(mapping_router, prefix="/api/mapping", tags=["mapping"])
app.include_router(uc_router, prefix="/api/uc", tags=["uc"])
app.include_router(
    autogenerate_router, prefix="/api/autogenerate", tags=["autogenerate"]
)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/yasgui", status_code=302)


@app.get("/yasgui")
async def yasgui() -> Response:
    html = (STATIC_DIR / "yasgui" / "index.html").read_text()
    return Response(content=html, media_type="text/html")


@app.get("/mapper")
async def mapper() -> Response:
    html = (STATIC_DIR / "mapper" / "index.html").read_text()
    return Response(content=html, media_type="text/html")


@app.get("/health")
async def health(request: Request) -> dict:
    return {
        "status": "ok" if ontop_manager.is_running else "degraded",
        "ontop_running": ontop_manager.is_running,
        "ontology_loaded": request.app.state.ontology_store.is_available(),
    }


@app.api_route("/sparql", methods=["GET", "POST", "OPTIONS"])
async def sparql(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204)

    try:
        token = get_user_token(request)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return Response(
            content=detail, status_code=exc.status_code, media_type="text/plain"
        )

    body = await request.body()
    result = await execute_sparql_query(
        body,
        token,
        settings,
        request.app.state.http_client,
        ontop_manager,
        method=request.method,
        query_string=request.url.query,
        headers=dict(request.headers),
    )

    if isinstance(result, SparqlExecuteError):
        return Response(
            content=result.message,
            status_code=result.status_code,
            media_type="text/plain",
        )

    assert isinstance(result, SparqlExecuteSuccess)
    return Response(
        content=json.dumps(result.data),
        media_type="application/sparql-results+json",
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
