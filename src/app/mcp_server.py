"""TBox Toolbox — FastMCP tools for Ontop VKG discovery, OBQC, and SPARQL execute."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from config import Settings
from obo import MISSING_USER_TOKEN, token_from_headers
from obqc import check_sparql as obqc_check_sparql
from ontology_store import OntologyStore
from ontop_manager import OntopProcessManager
from sparql_execute import SparqlExecuteError, execute_sparql_query

mcp = FastMCP("TBox Toolbox")

_ONTOLOGY_MISSING_MESSAGE = (
    "SPARQL ontology checks cannot run since the ontology is not loaded."
)


class McpAuthError(Exception):
    """Auth failure suitable for MCP tool error payloads (not FastAPI HTTPException)."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_mcp_user_token() -> str:
    """Return ``x-forwarded-access-token`` from the active MCP HTTP request.

    Uses the same token extraction as :func:`obo.get_user_token`. Raises
    :class:`McpAuthError` when the header is missing (MCP tools should not
    raise FastAPI ``HTTPException``).
    """

    token = token_from_headers(get_http_headers())
    if not token:
        raise McpAuthError(MISSING_USER_TOKEN, status_code=401)
    return token


@dataclass
class McpRuntime:
    """Shared dependencies for MCP tools linked to the app's lifespan."""

    ontology_store: OntologyStore
    ontop_manager: OntopProcessManager
    settings: Settings
    http_client: httpx.AsyncClient


_runtime: McpRuntime | None = None


def configure(runtime: McpRuntime) -> None:
    """Bind shared app dependencies for MCP tools."""
    global _runtime
    _runtime = runtime


def _require_runtime() -> McpRuntime:
    if _runtime is None:
        raise RuntimeError("MCP runtime is not configured")
    return _runtime


@mcp.tool
def health() -> dict[str, Any]:
    """Report Ontop process status and whether the TBox ontology cache is loaded.

    Discovery and ``check_sparql`` need a loaded ontology; ``execute_sparql`` needs
    Ontop running (and a user token from Databricks Apps).
    """
    runtime = _require_runtime()
    ontop_running = runtime.ontop_manager.is_running
    ontology_loaded = runtime.ontology_store.is_available()
    return {
        "ontop_running": ontop_running,
        "ontology_loaded": ontology_loaded,
        "status": "ok" if ontop_running else "degraded",
    }


@mcp.tool
def search_ontology(query: str, limit: int = 10) -> str:
    """Fuzzy-search ontology terms by label/comment and return matching Turtle.

    Prefer this (and ``describe_iri``) before drafting SPARQL.
    """
    return _require_runtime().ontology_store.search(query, limit=limit)


@mcp.tool
def describe_iri(iri: str) -> str:
    """Describe one ontology term as a focused Turtle neighborhood.

    ``iri`` must be a full IRI (e.g. ``http://example.org/tpch/placedBy``).
    Prefixed names and bare local names are not accepted — use
    ``search_ontology`` first if you only have a label or local name.
    Uses the cached TBox only.
    """
    return _require_runtime().ontology_store.describe(iri)


@mcp.tool
def check_sparql(query: str) -> dict[str, Any]:
    """Run Ontology-Based Query Check (OBQC) against the cached TBox.

    Stateless RDFS consistency checks (domain/range/property). Prefer calling
    this before ``execute_sparql`` and rewrite using violation messages. Does
    not hit the Virtual Knowledge Graph. If the ontology is not loaded, returns
    ``ontology_available: false``.
    """
    store = _require_runtime().ontology_store
    if not store.is_available() or store.graph is None:
        return {
            "ok": False,
            "ontology_available": False,
            "message": _ONTOLOGY_MISSING_MESSAGE,
        }
    result = obqc_check_sparql(query, store.graph)
    return {"ontology_available": True, **result}


@mcp.tool
async def execute_sparql(query: str) -> dict[str, Any]:
    """Execute a SPARQL query against the Virtual Knowledge Graph returning
    results in SPARQL JSON format.

    Prefer ``check_sparql`` first. On ANY failure — missing auth, reformulation
    error, or a warehouse SQL/permission error — this raises a ``ToolError`` so the
    MCP result is flagged ``isError`` and cannot be mistaken for a result. A
    successful query with zero matches is NOT an error: it returns normally with an
    empty ``bindings`` array.

    Full-native reformulation has limits (e.g. some OPTIONAL/BIND shapes,
    property paths, SERVICE, Update). Consider limiting the size of results
    to keep the context clean.

    Do not nest OPTIONAL inside OPTIONAL: a variable bound only in the inner
    block has no inferable type ("could not infer the unique type of its
    variable X"). Keep OPTIONAL blocks as siblings at one level, merging the
    inner triple patterns into the outer block where the data permits.

    Avoid GROUP_CONCAT: it maps to Spark ``listagg``, which fails on the
    warehouse (``AttributeReference cannot be cast to SortOrder``). To show
    the members of a group, either add the variable to GROUP BY for one row
    per member, or run a second query. SUM, COUNT, COUNT(DISTINCT), MIN and
    MAX over numbers and strings are safe.
    """
    runtime = _require_runtime()

    try:
        token = get_mcp_user_token()
    except McpAuthError as exc:
        raise ToolError(f"({exc.status_code}): {exc.message}") from exc

    result = await execute_sparql_query(
        query,
        token,
        runtime.settings,
        runtime.http_client,
        runtime.ontop_manager,
    )

    if isinstance(result, SparqlExecuteError):
        raise ToolError(f"({result.status_code}): {result.message}")

    return result.data
