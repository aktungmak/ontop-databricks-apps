"""Thin app orchestration for compiling and executing one SHACL shape."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SH

from config import Settings
from ontop_manager import OntopProcessManager
from shacl import (
    VKG,
    IllFormedShapeError,
    ShapesGraph,
    SparqlValidator,
    UnsupportedShapeError,
    add_violation_results,
    validation_report,
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
) -> Graph:
    """Parse, compile, execute, and assemble a ValidationReport.
    Either succeeds fully or fails entirely, no partial results."""
    if not ontop_manager.is_running:
        raise ShaclValidationError("Ontop is not running", 503)

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

    report = validation_report(truncated=False, has_targets=compiled.has_targets)
    if not compiled.queries:
        return report

    total = 0
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
        remaining = MAX_SHACL_VIOLATIONS - total
        if len(bindings) > remaining:
            truncated = True
            bindings = bindings[:remaining]
        try:
            total += add_violation_results(report, compiled_query, bindings)
        except ValueError as err:
            raise ShaclValidationError(str(err), 502) from err
        if truncated:
            break  # further queries cannot appear in the capped payload

    if truncated:
        report_node = report.value(predicate=RDF.type, object=SH.ValidationReport)
        report.set((report_node, VKG.truncated, Literal(True)))
    return report
