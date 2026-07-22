"""Ontop VKG Databricks App — FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from databricks.sdk import WorkspaceClient
from databricks.sql.exc import RequestError
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import Settings
from obo import get_user_token, get_workspace_host
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

_NATIVE_LINE = re.compile(
    r"^\s*NATIVE\s*\[[^\]]*\]\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CONSTRUCT_TYPES = re.compile(
    r"CONSTRUCT\s*\[[^\]]*\]\s*\[([^\]]+)\]",
    re.IGNORECASE,
)
_VAR_RDF_TYPE = re.compile(
    r"(\w+)/RDF\((?:[^(),]|\([^()]*\))+,(IRI|xsd:[\w]+|https?://[^)]+)\)",
    re.IGNORECASE,
)
_XSD_NS = "http://www.w3.org/2001/XMLSchema#"

settings = Settings.from_env()
ontop_manager = OntopProcessManager(settings)
http_client: httpx.AsyncClient | None = None


def extract_native_sql(reformulate_output: str) -> str:
    """Return executable SQL from Ontop reformulate output (5.5 IQ tree or plain SQL)."""
    text = reformulate_output.strip()
    match = _NATIVE_LINE.search(text)
    if match:
        return text[match.end() :].lstrip("\n").strip() or text
    return text


def extract_variable_types(reformulate_output: str) -> dict[str, str]:
    """Parse projected-variable RDF types from Ontop 5.5 IQ-tree CONSTRUCT metadata.

    Ontop 5.6 (PR #933) will expose a better native-consumption API; this parser
    targets the 5.5 reformulate output shape and will need updating then.
    """
    match = _CONSTRUCT_TYPES.search(reformulate_output)
    if not match:
        return {}
    return {m.group(1): m.group(2) for m in _VAR_RDF_TYPE.finditer(match.group(1))}


def _binding_for_type(ontop_type: str, sval: str) -> dict[str, str]:
    if ontop_type.upper() == "IRI":
        return {"type": "uri", "value": sval}
    if ontop_type.startswith("xsd:"):
        return {
            "type": "literal",
            "value": sval,
            "datatype": f"{_XSD_NS}{ontop_type[4:]}",
        }
    if ontop_type.startswith("http://") or ontop_type.startswith("https://"):
        return {"type": "literal", "value": sval, "datatype": ontop_type}
    return {"type": "literal", "value": sval}


def run_sql(
    sql: str, token: str, app_settings: Settings
) -> tuple[list[str], list[tuple]]:
    from databricks import sql as dbsql

    try:
        with dbsql.connect(
            server_hostname=get_workspace_host(),
            http_path=app_settings.warehouse_http_path,
            access_token=token,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                columns = (
                    [desc[0] for desc in cursor.description]
                    if cursor.description
                    else []
                )
                rows = cursor.fetchall()
    except RequestError as exc:
        original = (exc.context or {}).get("original-exception")
        message = str(original).strip() if original else str(exc)
        raise RuntimeError(message) from exc

    return columns, rows


def to_sparql_json(
    columns: list[str],
    rows: list[tuple],
    var_types: dict[str, str] | None = None,
) -> dict:
    types = var_types or {}
    bindings: list[dict[str, dict[str, str]]] = []
    for row in rows:
        binding: dict[str, dict[str, str]] = {}
        for col, val in zip(columns, row, strict=False):
            if val is None:
                continue
            sval = str(val)
            if col in types:
                binding[col] = _binding_for_type(types[col], sval)
            else:
                binding[col] = {"type": "literal", "value": sval}
        bindings.append(binding)
    return {"head": {"vars": columns}, "results": {"bindings": bindings}}


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
async def health() -> dict:
    return {
        "status": "ok" if ontop_manager.is_running else "degraded",
        "ontop_running": ontop_manager.is_running,
    }


@app.api_route("/sparql", methods=["GET", "POST", "OPTIONS"])
async def sparql(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204)

    if not ontop_manager.is_running:
        return Response(
            content="Ontop is not running",
            status_code=503,
            media_type="text/plain",
        )

    try:
        token = get_user_token(request)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return Response(
            content=detail, status_code=exc.status_code, media_type="text/plain"
        )

    target = f"http://127.0.0.1:{settings.ontop_internal_port}/ontop/reformulate"
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
        logger.exception(
            "Failed to reformulate %s request at %s", request.method, target
        )
        return Response(
            content="Failed to reach Ontop reformulate endpoint",
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
            content=upstream.text,
            status_code=upstream.status_code,
            media_type="text/plain",
        )

    var_types = extract_variable_types(upstream.text)
    sql = extract_native_sql(upstream.text)
    try:
        columns, rows = await asyncio.to_thread(run_sql, sql, token, settings)
    except RuntimeError as exc:
        logger.exception("Databricks SQL execution failed")
        return Response(content=str(exc), status_code=502, media_type="text/plain")

    return Response(
        content=json.dumps(to_sparql_json(columns, rows, var_types)),
        media_type="application/sparql-results+json",
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
