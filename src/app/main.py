"""Ontop VKG Databricks App — FastAPI entrypoint."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import Settings
from ontop_manager import OntopProcessManager
from routes.autogenerate import router as autogenerate_router
from routes.mapping import router as mapping_router
from routes.uc import router as uc_router

logging.basicConfig(
    level=logging.INFO,
    format="[APP] %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

settings = Settings.from_env()
ontop_manager = OntopProcessManager(settings)
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    client = WorkspaceClient()
    app.state.settings = settings
    app.state.sp_client = client
    logger.info("Preparing Ontop from volume %s", settings.mappings_volume_path)
    ontop_manager.prepare(client)
    ontop_manager.write_jdbc_properties()
    ontop_manager.start()
    http_client = httpx.AsyncClient(timeout=120.0)
    logger.info("Uvicorn running on 0.0.0.0:%s", settings.app_port)
    yield
    ontop_manager.stop()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="Ontop VKG", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(mapping_router, prefix="/api/mapping", tags=["mapping"])
app.include_router(uc_router, prefix="/api/uc", tags=["uc"])
app.include_router(autogenerate_router, prefix="/api/autogenerate", tags=["autogenerate"])


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
async def health() -> dict:
    return {
        "status": "ok" if ontop_manager.is_running else "degraded",
        "ontop_running": ontop_manager.is_running,
    }


@app.api_route("/sparql", methods=["GET", "POST", "OPTIONS"])
async def sparql_proxy(request: Request) -> Response:
    if not ontop_manager.is_running:
        return Response(
            content="Ontop is not running",
            status_code=503,
            media_type="text/plain",
        )

    target = f"http://127.0.0.1:{settings.ontop_internal_port}/sparql"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "connection"}
    }
    body = await request.body()

    assert http_client is not None
    try:
        upstream = await http_client.request(
            request.method,
            target,
            headers=headers,
            content=body if body else None,
        )
    except httpx.RequestError:
        logger.exception("Failed to proxy %s to Ontop at %s", request.method, target)
        return Response(
            content="Failed to reach Ontop SPARQL endpoint",
            status_code=502,
            media_type="text/plain",
        )

    if upstream.status_code >= 400:
        logger.error(
            "Ontop returned %s for %s %s: %s",
            upstream.status_code,
            request.method,
            target,
            upstream.text[:500] if upstream.text else "",
        )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
