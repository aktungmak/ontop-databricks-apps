"""Assemble a SHACL ValidationReport graph from compiled violation queries."""

from __future__ import annotations

from typing import Any, Iterable

from rdflib import BNode, Graph, Literal, Namespace, Node, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, SH, XSD

from .shapes import (
    AlternativePath,
    InversePath,
    PredicatePath,
    PropertyPath,
    SequencePath,
)
from .sparql_validator import SparqlViolationQuery

__all__ = [
    "VKG",
    "add_violation_results",
    "validation_report",
]

VKG = Namespace("urn:ontop-vkg:shacl#")


def _node_from_sparql_json(term: Any) -> Node | None:
    """Map one SPARQL Results JSON term to an RDF node."""
    if not isinstance(term, dict):
        return None
    kind = term.get("type")
    value = term.get("value")
    if not isinstance(value, str):
        return None
    if kind in ("uri", "iri"):
        return URIRef(value)
    if kind == "bnode":
        return BNode(value)
    if kind in ("literal", "typed-literal"):
        lang = term.get("xml:lang") or term.get("lang")
        datatype = term.get("datatype")
        if isinstance(lang, str) and lang:
            return Literal(value, lang=lang)
        if isinstance(datatype, str) and datatype:
            return Literal(value, datatype=URIRef(datatype))
        return Literal(value)
    return None


def _binding_node(binding: dict[str, Any], variable: str) -> Node | None:
    return _node_from_sparql_json(binding.get(variable))


def _add_property_path(graph: Graph, path: PropertyPath) -> Node:
    """Encode a SHACL property path and return the node for ``sh:resultPath``."""
    if isinstance(path, PredicatePath):
        return path.predicate
    if isinstance(path, InversePath):
        node = BNode()
        graph.add((node, SH.inversePath, _add_property_path(graph, path.path)))
        return node
    if isinstance(path, SequencePath):
        node = BNode()
        Collection(
            graph, node, [_add_property_path(graph, part) for part in path.paths]
        )
        return node
    if isinstance(path, AlternativePath):
        node = BNode()
        members = BNode()
        Collection(
            graph, members, [_add_property_path(graph, part) for part in path.paths]
        )
        graph.add((node, SH.alternativePath, members))
        return node
    raise TypeError(f"Unsupported property path in validation report: {path!r}")


def validation_report(*, truncated: bool, has_targets: bool) -> Graph:
    """Create an empty (conforming) SHACL ValidationReport graph."""
    graph = Graph()
    graph.bind("sh", SH)
    graph.bind("xsd", XSD)
    graph.bind("vkg", VKG)
    report = BNode()
    graph.add((report, RDF.type, SH.ValidationReport))
    graph.add((report, SH.conforms, Literal(True)))
    graph.add((report, VKG.truncated, Literal(truncated)))
    graph.add((report, VKG.hasTargets, Literal(has_targets)))
    return graph


def add_violation_results(
    graph: Graph,
    query: SparqlViolationQuery,
    bindings: Iterable[dict[str, Any]],
) -> int:
    """Write one sh:result ValidationResult per binding; return how many were added."""
    report = next(graph.subjects(RDF.type, SH.ValidationReport))
    added = 0
    for binding in bindings:
        focus_node = _binding_node(binding, "focus_node")
        if focus_node is None:
            raise ValueError("Violation binding is missing focus_node")
        result = BNode()
        graph.add((report, SH.result, result))
        graph.add((result, RDF.type, SH.ValidationResult))
        graph.add((result, SH.focusNode, focus_node))
        graph.add((result, SH.sourceShape, query.shape_ref))
        graph.add((result, SH.sourceConstraintComponent, query.constraint_iri))
        graph.add((result, SH.resultSeverity, query.severity))
        value = _binding_node(binding, "value")
        if value is not None:
            graph.add((result, SH.value, value))
        if query.property_path is not None:
            path_node = _add_property_path(graph, query.property_path)
            graph.add((result, SH.resultPath, path_node))
        if query.message is not None:
            graph.add((result, SH.resultMessage, Literal(query.message)))
        added += 1
    if added:
        graph.set((report, SH.conforms, Literal(False)))
    return added
