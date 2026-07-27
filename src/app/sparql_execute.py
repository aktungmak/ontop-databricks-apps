"""Shared in-process SPARQL execution (Ontop reformulate → DBSQL → SPARQL JSON).

Used by the HTTP ``/sparql`` adapter and (later) MCP ``execute_sparql``. Callers must
not HTTP-self-call ``/sparql``; invoke ``execute_sparql_query`` instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Mapping

import httpx
from databricks.sql.exc import RequestError

from config import Settings
from obo import get_workspace_host
from ontop_manager import OntopProcessManager

logger = logging.getLogger(__name__)

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

_HOP_BY_HOP = frozenset({"host", "content-length", "connection"})


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


def _reformulate_request_parts(
    query_or_request_body: str | bytes,
    *,
    method: str,
    headers: Mapping[str, str] | None,
) -> tuple[str, dict[str, str], bytes | None]:
    """Build method/headers/body for Ontop ``/ontop/reformulate``."""
    out_headers = {
        k: v for k, v in (headers or {}).items() if k.lower() not in _HOP_BY_HOP
    }
    if isinstance(query_or_request_body, str):
        # MCP / programmatic callers pass a SPARQL query string.
        out_headers.setdefault("content-type", "application/sparql-query")
        return method.upper(), out_headers, query_or_request_body.encode("utf-8")

    return method.upper(), out_headers, query_or_request_body or None


async def execute_sparql_query(
    query_or_request_body: str | bytes,
    token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    ontop_manager: OntopProcessManager,
    *,
    method: str = "POST",
    query_string: str = "",
    headers: Mapping[str, str] | None = None,
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

    req_method, req_headers, body = _reformulate_request_parts(
        query_or_request_body,
        method=method,
        headers=headers,
    )
    target = f"http://127.0.0.1:{settings.ontop_internal_port}/ontop/reformulate"
    if query_string:
        target = f"{target}?{query_string}"

    try:
        upstream = await http_client.request(
            req_method,
            target,
            headers=req_headers,
            content=body,
        )
    except httpx.RequestError:
        logger.exception("Failed to reformulate %s request at %s", req_method, target)
        return SparqlExecuteError(
            message="Failed to reach Ontop reformulate endpoint",
            status_code=502,
        )

    if upstream.status_code >= 400:
        detail = format_ontop_error(upstream.text, upstream.status_code)
        logger.error(
            "Ontop returned %s for %s %s: %s",
            upstream.status_code,
            req_method,
            target,
            detail[:1000],
        )
        return SparqlExecuteError(
            message=detail,
            status_code=upstream.status_code,
        )

    var_types = extract_variable_types(upstream.text)
    sql = extract_native_sql(upstream.text)
    try:
        columns, rows = await asyncio.to_thread(run_sql, sql, token, settings)
    except RuntimeError as exc:
        logger.exception("Databricks SQL execution failed")
        return SparqlExecuteError(message=str(exc), status_code=502)

    return SparqlExecuteSuccess(data=to_sparql_json(columns, rows, var_types))
