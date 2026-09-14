"""TBox Toolbox — FastMCP tools for Ontop VKG discovery, OBQC, and SPARQL execute."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from actions.models import ConfirmActionRequest, PrepareActionRequest
from actions.service import ActionError, ActionService
from config import Settings
from obo import MISSING_USER_TOKEN, token_from_headers
from ontology_store import OntologyStore
from ontop_manager import OntopProcessManager
from sparql_execute import SparqlExecuteError, execute_sparql_query

mcp = FastMCP(
    "TBox Toolbox",
    instructions=(
        "Start with discovery: use search_ontology and describe_iri to find the classes and "
        "properties relevant to your query, then build SPARQL only from IRIs those tools return. "
        "If search_ontology returns `# No matches`, do not invent a query against guessed terms. "
        "Instead, search again with different words, or stop and say the term is not in the ontology. "
        "Fully-unbound triple patterns like `?s ?p ?o` are rejected, at least one compnent must be bound. "
        "Use check_sparql before execute_sparql to ensure the query is valid."
    ),
)

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

    Raises :class:`McpAuthError` when the header is missing (MCP tools should
    not raise FastAPI ``HTTPException``).
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
    action_service: ActionService | None = None


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

    Discovery and ``check_sparql`` need a loaded ontology.
    ``execute_sparql`` only needs Ontop running (and a user token from Databricks Apps).
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

    Prefer this (and ``describe_iri``) before drafting SPARQL. If the result is
    ``# No matches``, do not fabricate SPARQL against guessed terms — search again
    with different words, or stop.
    Only build queries from IRIs returned here.
    """
    return _require_runtime().ontology_store.search(query, limit=limit)


@mcp.tool
def describe_iri(iri: str) -> str:
    """Describe one ontology term as a focused Turtle neighborhood.

    ``iri`` must be a full IRI (e.g. ``http://example.org/tpch/placedBy``).
    Prefixed names and bare local names are not accepted.
    Use ``search_ontology`` first if you only have a label or local name.
    """
    return _require_runtime().ontology_store.describe(iri)


@mcp.tool
def check_sparql(query: str) -> dict[str, Any]:
    """Run Ontology-Based Query Check (OBQC) against the cached TBox.

    Low-latency stateless RDFS consistency checks (domain/range/property).
    Prefer calling this before ``execute_sparql`` and rewrite using violation messages.
    """
    store = _require_runtime().ontology_store
    checker = store.obqc_checker
    if not store.is_available() or checker is None:
        return {
            "ok": False,
            "ontology_available": False,
            "message": _ONTOLOGY_MISSING_MESSAGE,
        }
    result = checker.check(query)
    return {"ontology_available": True, **result}


@mcp.tool
async def execute_sparql(query: str) -> dict[str, Any]:
    """Execute a SPARQL query against the Virtual Knowledge Graph returning
    results in SPARQL JSON format.

    Prefer ``check_sparql`` first. On ANY failure the MCP result is flagged ``isError``.
    A successful query with zero matches is NOT an error: it returns normally with an
    empty ``bindings`` array.

    A fully-unbound triple pattern ``?s ?p ?o`` will be rejected.

    Full-native reformulation has limits (e.g. some OPTIONAL/BIND shapes,
    property paths, SERVICE, Update).

    Consider limiting the size of results to keep the context clean.

    Do not nest OPTIONAL inside OPTIONAL: a variable bound only in the inner
    block has no inferable type ("could not infer the unique type of its
    variable X"). Keep OPTIONAL blocks as siblings at one level, merging the
    inner triple patterns into the outer block where the data permits.

    Avoid GROUP_CONCAT.  To show the members of a group, either add the variable
    to GROUP BY for one row per member, or run a second query.
    SUM, COUNT, COUNT(DISTINCT), MIN and MAX over numbers and strings are safe.
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


@mcp.tool
def list_actions(
    class_iri: str | None = None,
    subject_iri: str | None = None,
    property_iri: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """List the action catalog and writable VKG properties."""
    runtime = _require_runtime()
    if runtime.action_service is None:
        return {"available": False, "actions": [], "properties": []}
    return runtime.action_service.list_actions(
        class_iri=class_iri,
        subject_iri=subject_iri,
        property_iri=property_iri,
        kind=kind,
    )


@mcp.tool
def describe_action(action_iri: str) -> dict[str, Any] | None:
    """Describe one action from the active VKG action catalog."""
    runtime = _require_runtime()
    if runtime.action_service is None:
        return None
    return runtime.action_service.describe_action(action_iri)


@mcp.tool
async def prepare_action(
    action_iri: str,
    subject_iri: str,
    params: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any] | str:
    """Prepare an action under the MCP caller's forwarded Databricks token."""
    runtime = _require_runtime()
    try:
        token = get_mcp_user_token()
        if runtime.action_service is None:
            return "Error (503): action service is unavailable"
        result = await runtime.action_service.prepare(
            PrepareActionRequest(
                action_iri=action_iri,
                subject_iri=subject_iri,
                params=params,
                idempotency_key=idempotency_key,
            ),
            token,
        )
    except (McpAuthError, ActionError) as error:
        return f"Error ({error.status_code}): {error.message}"
    return result.model_dump() if hasattr(result, "model_dump") else result


@mcp.tool
async def confirm_action(preparation_token: str) -> dict[str, Any] | str:
    """Confirm a prepared action under the MCP caller's forwarded token."""
    runtime = _require_runtime()
    try:
        token = get_mcp_user_token()
        if runtime.action_service is None:
            return "Error (503): action service is unavailable"
        result = await runtime.action_service.confirm(
            ConfirmActionRequest(preparation_token=preparation_token), token
        )
    except (McpAuthError, ActionError) as error:
        return f"Error ({error.status_code}): {error.message}"
    return result.model_dump() if hasattr(result, "model_dump") else result
