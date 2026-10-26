"""Shared in-process SPARQL execution (Ontop reformulate → DBSQL → SPARQL JSON).

Callers pass a SPARQL query string. Used by the HTTP ``/sparql`` adapter and
MCP ``execute_sparql``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from databricks.sql.exc import Error as DbsqlError, RequestError
from rdflib.term import Node, URIRef, Variable

from config import Settings
from obo import get_workspace_host
from obqc import extract_bgp_triples
from ontop_manager import OntopProcessManager

logger = logging.getLogger(__name__)

_NATIVE_LINE = re.compile(
    r"^\s*NATIVE\s*\[[^\]]*\]\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# EmptyNodeImpl.toString() is "EMPTY " + projectedVariables, e.g. "EMPTY [focus_node, value]".
# Native generation dumps the IQ root as "IQ: " + tree; an empty root is an empty SPARQL result.
_EMPTY_NODE = re.compile(r"EMPTY\s*\[([^\]]*)\]")
_EMPTY_IQ_ROOT = re.compile(r"IQ:\s*EMPTY\s*\[([^\]]*)\]")
_CONSTRUCT_TYPES = re.compile(
    r"CONSTRUCT\s*\[[^\]]*\]\s*\[([^\]]+)\]",
    re.IGNORECASE,
)
_VAR_RDF_TYPE = re.compile(
    r"(\w+)/RDF\((?:[^(),]|\([^()]*\))+,(IRI|xsd:[\w]+|https?://[^)]+)\)",
    re.IGNORECASE,
)
_XSD_NS = "http://www.w3.org/2001/XMLSchema#"

# Ontop ReformulateController binds @RequestParam("query") — form body only.
_REFORMULATE_HEADERS = {"content-type": "application/x-www-form-urlencoded"}


@dataclass(frozen=True)
class SparqlExecuteSuccess:
    """SPARQL Results JSON payload (application/sparql-results+json)."""

    data: dict


@dataclass(frozen=True)
class SparqlExecuteError:
    """Failure surfaced to callers; never includes reformulated native SQL."""

    message: str
    status_code: int


SparqlExecuteResult = SparqlExecuteSuccess | SparqlExecuteError


def extract_native_sql(reformulate_output: str) -> str:
    """Return executable SQL from Ontop reformulate output (5.5 IQ tree or plain SQL)."""
    text = reformulate_output.strip()
    match = _NATIVE_LINE.search(text)
    if match:
        return text[match.end() :].lstrip("\n").strip() or text
    return text


def _vars_from_empty_iq(bracket_contents: str) -> list[str]:
    return [part.strip() for part in bracket_contents.split(",") if part.strip()]


def empty_sparql_json_from_ontop_error(message: str) -> dict | None:
    """Map Ontop's empty-IQ native-reformulation failure to empty SPARQL JSON.

    Key off EmptyNode's dump as the IQ root (``IQ: EMPTY [vars]``), not the
    wrapping English exception phrase. A non-empty root such as ``IQ: UNION``
    is a real native-translation failure even if an EMPTY child appears later.
    """
    match = _EMPTY_IQ_ROOT.search(message)
    if not match:
        return None
    return to_sparql_json(_vars_from_empty_iq(match.group(1)), [])


def empty_sparql_json_from_reformulate_output(text: str) -> dict | None:
    """Map a successful reformulate IQ tree that bottoms out at EMPTY to no rows.

    Occurs when Ontop returns the optimized tree without a NATIVE SQL node.
    """
    if _NATIVE_LINE.search(text):
        return None
    matches = list(_EMPTY_NODE.finditer(text))
    if len(matches) != 1:
        return None
    upper = text.upper()
    if "SELECT" in upper and "FROM" in upper:
        return None
    return to_sparql_json(_vars_from_empty_iq(matches[0].group(1)), [])


def format_ontop_error(body: str, status_code: int) -> str:
    """Prefer the Spring Boot/Ontop exception message over a generic error envelope."""
    text = (body or "").strip()
    if not text:
        return f"Ontop reformulate failed with HTTP {status_code}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict):
        return text
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    error = payload.get("error")
    path = payload.get("path")
    parts = [str(error)] if error else []
    parts.append(f"(HTTP {status_code})")
    if path:
        parts.append(f"at {path}")
    return " ".join(parts)


# Unity Catalog permission denials arrive as databricks.sql.exc.ServerOperationError
# with SQLSTATE 42501 and an [INSUFFICIENT_PERMISSIONS] prefix. These tokens are emitted
# verbatim by the SQL warehouse for catalog-, schema-, and table-level denials.
_PERMISSION_TOKENS = ("SQLSTATE: 42501", "INSUFFICIENT_PERMISSIONS")


def is_permission_denied(message: str) -> bool:
    """True when a DBSQL error message is a Unity Catalog authorization denial."""
    return any(token in message for token in _PERMISSION_TOKENS)


def _last_line(message: str) -> str:
    """Last non-empty line of a driver error, dropping any multi-line SQL echo."""
    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    return lines[-1] if lines else message.strip()


def permission_denied_summary(message: str) -> str:
    """One-line, caller-safe summary of a permission denial.

    Uses only the last non-empty line of ``str(exc)`` (which carries the object and
    privilege, e.g. "User does not have SELECT on Table 'cat.sch.tbl'. SQLSTATE: 42501").
    Never includes exc.context, so the multi-KB Java stacktrace cannot leak.
    """
    return f"You lack Unity Catalog access to an object this query requires: {_last_line(message)}"


def extract_variable_types(reformulate_output: str) -> dict[str, str]:
    """Parse projected-variable RDF types from Ontop 5.5 IQ-tree CONSTRUCT metadata.

    Ontop 5.6 (PR #933) will expose a better native-consumption API, until then this parser
    targets the 5.5 reformulate output shape.
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

    connection_options = {
        "server_hostname": get_workspace_host(),
        "http_path": app_settings.warehouse_http_path,
        "access_token": token,
        "catalog": app_settings.default_catalog,
        "schema": app_settings.default_schema,
    }

    try:
        with dbsql.connect(**connection_options) as conn:
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


def _unbound_predicate_triple(query: str) -> tuple[Node, Node, Node] | None:
    """Return the first fully-unbound-predicate BGP triple, or ``None``.

    Fully-unbound patterns like ``?s ?p ?o`` forces Ontop to build a UNION over
    every TriplesMap, which is not efficient so we reject queries like this.
    Returns ``None`` when the query does not parse so Ontop surfaces its own error.
    """
    try:
        triples = extract_bgp_triples(query)
    except Exception:
        return None
    for s, p, o in triples:
        if (
            isinstance(p, Variable)
            and not isinstance(s, URIRef)
            and not isinstance(o, URIRef)
        ):
            return (s, p, o)
    return None


async def execute_sparql_query(
    query: str,
    token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    ontop_manager: OntopProcessManager,
) -> SparqlExecuteResult:
    """Reformulate via Ontop, run DBSQL with ``token``, return SPARQL JSON or error.

    On failure returns :class:`SparqlExecuteError` with a human-readable message
    (Ontop or DBSQL). The reformulated native SQL is never included in the error
    result so MCP/HTTP adapters can surface it safely to agents.
    """
    if not ontop_manager.is_running:
        return SparqlExecuteError(
            message="Ontop is not running",
            status_code=503,
        )

    # Reject fully-unbound-predicate shapes before they reach the reformulator.
    if _unbound_predicate_triple(query) is not None:
        return SparqlExecuteError(
            message=(
                "This query uses a fully-unbound pattern (e.g. `?s ?p ?o`). "
                "Bind at least one variable to a specific value."
            ),
            status_code=400,
        )

    target = f"http://127.0.0.1:{settings.ontop_internal_port}/ontop/reformulate"
    try:
        upstream = await http_client.post(
            target,
            headers=_REFORMULATE_HEADERS,
            content=urlencode({"query": query}).encode("utf-8"),
        )
    except httpx.RequestError:
        logger.exception("Failed to reformulate POST request at %s", target)
        return SparqlExecuteError(
            message="Failed to reach Ontop reformulate endpoint",
            status_code=502,
        )

    if upstream.status_code >= 400:
        detail = format_ontop_error(upstream.text, upstream.status_code)
        empty = empty_sparql_json_from_ontop_error(detail)
        if empty is not None:
            logger.info(
                "Ontop proved SPARQL empty (native EMPTY IQ); returning no rows"
            )
            return SparqlExecuteSuccess(data=empty)
        logger.error(
            "Ontop returned %s for POST %s: %s",
            upstream.status_code,
            target,
            detail[:1000],
        )
        return SparqlExecuteError(
            message=detail,
            status_code=upstream.status_code,
        )

    empty = empty_sparql_json_from_reformulate_output(upstream.text)
    if empty is not None:
        logger.info("Ontop reformulate returned EMPTY IQ; returning no rows")
        return SparqlExecuteSuccess(data=empty)

    var_types = extract_variable_types(upstream.text)
    sql = extract_native_sql(upstream.text)
    try:
        columns, rows = await asyncio.to_thread(run_sql, sql, token, settings)
    except (RuntimeError, DbsqlError) as exc:
        logger.exception("Databricks SQL execution failed")
        message = str(exc)
        if is_permission_denied(message):
            return SparqlExecuteError(
                message=permission_denied_summary(message),
                status_code=403,
            )
        # Non-permission DBSQL/execution failures: surface only the last non-empty
        # line (never the native SQL or exc.context) as a 502.
        return SparqlExecuteError(message=_last_line(message), status_code=502)

    return SparqlExecuteSuccess(data=to_sparql_json(columns, rows, var_types))
