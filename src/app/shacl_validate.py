"""Thin app orchestration for compiling and executing one SHACL shape."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from rdflib import Graph, URIRef

from config import Settings
from ontop_manager import OntopProcessManager
from shacl import (
    IllFormedShapeError,
    ShapesGraph,
    SparqlValidator,
    UnsupportedShapeError,
    build_violation_results,
)
from sparql_execute import SparqlExecuteError, execute_sparql_query

MAX_SHACL_VIOLATIONS = 100


@dataclass(frozen=True)
class ShaclValidationError(Exception):
    message: str
    status_code: int

    def __str__(self) -> str:
        return self.message


def _shape_ref(shape_iri: str) -> URIRef:
    parsed = urlparse(shape_iri)
    if not shape_iri or not parsed.scheme or any(char.isspace() for char in shape_iri):
        raise ShaclValidationError("shape_iri must be an absolute IRI", 400)
    return URIRef(shape_iri)


def _limited_query(query: str, limit: int) -> str:
    return f"SELECT * WHERE {{\n  {{ {query} }}\n}}\nLIMIT {limit}"


async def run_shacl_validation(
    shapes_turtle: str,
    shape_iri: str,
    token: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    ontop_manager: OntopProcessManager,
) -> dict[str, Any]:
    """Parse, compile, execute, and assemble a validation result.
    Either succeeds fully or fails entirely, no partial results."""
    graph = Graph()
    try:
        graph.parse(data=shapes_turtle, format="turtle")
    except Exception as err:
        raise ShaclValidationError(f"Invalid Turtle shapes graph: {err}", 400) from err

    try:
        shapes = ShapesGraph.from_graph(graph)
        compiled = SparqlValidator(shapes).compile_shape(_shape_ref(shape_iri))
    except (IllFormedShapeError, UnsupportedShapeError) as err:
        raise ShaclValidationError(str(err), 400) from err

    if not compiled.queries:
        return {
            "conforms": True,
            "violations": [],
            "truncated": False,
            "has_targets": compiled.has_targets,
        }

    violations: list[dict[str, str]] = []
    truncated = False
    query_limit = MAX_SHACL_VIOLATIONS + 1
    for compiled_query in compiled.queries:
        result = await execute_sparql_query(
            _limited_query(compiled_query.query, query_limit),
            token,
            settings,
            http_client,
            ontop_manager,
        )
        if isinstance(result, SparqlExecuteError):
            raise ShaclValidationError(result.message, result.status_code)
        bindings = result.data.get("results", {}).get("bindings")
        if not isinstance(bindings, list):
            raise ShaclValidationError(
                "SHACL query returned invalid SPARQL JSON bindings", 502
            )
        try:
            violations.extend(build_violation_results(compiled_query, bindings))
        except ValueError as err:
            raise ShaclValidationError(str(err), 502) from err
        truncated = truncated or len(violations) > MAX_SHACL_VIOLATIONS
        if truncated:
            break  # further queries cannot appear in the capped payload

    violations = violations[:MAX_SHACL_VIOLATIONS]
    return {
        "conforms": len(violations) == 0,
        "violations": violations,
        "truncated": truncated,
        "has_targets": True,
    }
