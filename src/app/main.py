"""Ontop VKG Databricks App — FastAPI entrypoint."""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
import uvicorn
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
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
MCP_PATH = "/mcp"

# /health probes the reformulate endpoint with this query and timeout so a wedged
# (unresponsive-but-alive) Ontop reports unhealthy. Mapping-agnostic; overridable.
HEALTH_QUERY = os.environ.get("VKG_HEALTH_QUERY", "ASK {}")
HEALTH_REFORMULATE_TIMEOUT = float(os.environ.get("VKG_HEALTH_TIMEOUT", "8"))

settings = Settings.from_env()
ontop_manager = OntopProcessManager(settings)

# Streamable HTTP endpoint at /mcp. The app is mounted at "/" after all FastAPI
# routes so those routes take precedence without triggering a /mcp redirect.
# Stateless: Databricks MCP clients do not resend the mcp-session-id header, so a
# session-bound transport rejects every request after initialize.
mcp_app = mcp.http_app(path=MCP_PATH, stateless_http=True)


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
async def health(request: Request) -> Response:
    """Readiness check that exercises the reformulate endpoint.

    Process liveness alone is misleading: an alive-but-unresponsive Ontop (a stalled
    JVM, a full stdout sink, a deadlock) still passes a ``poll()`` check. Probe
    reformulate with a short timeout; a timeout means the endpoint is wedged -> 503,
    so the platform can restart or route around this instance.
    """
    running = ontop_manager.is_running
    ontology = request.app.state.ontology_store.is_available()
    reformulate_responsive = False
    reformulate_status: int | None = None
    detail: str | None = None
    if not running:
        detail = "ontop process not running"
    else:
        target = (
            f"http://127.0.0.1:{settings.ontop_internal_port}/ontop/reformulate"
            "?forNativeConsumption=true"
        )
        try:
            resp = await request.app.state.http_client.post(
                target,
                headers={"content-type": "application/x-www-form-urlencoded"},
                content=urlencode({"query": HEALTH_QUERY}).encode("utf-8"),
                timeout=HEALTH_REFORMULATE_TIMEOUT,
            )
            reformulate_responsive = True  # any HTTP reply proves it is not wedged
            reformulate_status = resp.status_code
        except httpx.RequestError as exc:
            detail = f"reformulate unresponsive ({type(exc).__name__}); endpoint may be wedged"

    healthy = running and ontology and reformulate_responsive
    payload = {
        "status": "ok" if healthy else "degraded",
        "ontop_running": running,
        "ontology_loaded": ontology,
        "reformulate_responsive": reformulate_responsive,
        "reformulate_status": reformulate_status,
    }
    if detail:
        payload["detail"] = detail
    return JSONResponse(payload, status_code=200 if healthy else 503)


@app.api_route("/sparql", methods=["GET", "POST", "OPTIONS"])
async def sparql(request: Request) -> Response:
    """SPARQL Protocol endpoint: GET ``?query=`` or POST form field ``query``."""
    if request.method == "OPTIONS":
        return Response(status_code=204)

    try:
        token = get_user_token(request)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return Response(
            content=detail, status_code=exc.status_code, media_type="text/plain"
        )

    query = request.query_params.get("query")
    if query is None and request.method == "POST":
        form = await request.form()
        value = form.get("query")
        query = value if isinstance(value, str) else None
    if not query:
        return Response(
            content="Missing required SPARQL request parameter 'query'",
            status_code=400,
            media_type="text/plain",
        )

    result = await execute_sparql_query(
        query,
        token,
        settings,
        request.app.state.http_client,
        ontop_manager,
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


# Keep this catch-all mount last so the FastAPI routes above retain precedence.
app.mount("/", mcp_app)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
