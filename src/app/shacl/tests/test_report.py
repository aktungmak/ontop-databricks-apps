"""SHACL ValidationReport assembly from SPARQL JSON bindings."""

from __future__ import annotations

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from shacl.report import VKG, add_violation_results, validation_report
from shacl.report import _add_property_path, _node_from_sparql_json
from shacl.shapes import AlternativePath, InversePath, PredicatePath, SequencePath
from shacl.shapes import ShapesGraph
from shacl.sparql_validator import SparqlValidator

EX = Namespace("http://example.org/shacl-test/")

_PREFIXES = """
@prefix ex: <http://example.org/shacl-test/> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def _compile(ttl: str):
    graph = Graph()
    graph.parse(data=_PREFIXES + ttl, format="turtle")
    return SparqlValidator(ShapesGraph.from_graph(graph)).validate()[0]


def _report_node(graph: Graph):
    reports = list(graph.subjects(RDF.type, SH.ValidationReport))
    assert len(reports) == 1
    return reports[0]


def test_node_from_sparql_json_preserves_term_kinds() -> None:
    assert _node_from_sparql_json(
        {"type": "uri", "value": str(EX.Alice)}
    ) == EX.Alice
    assert _node_from_sparql_json({"type": "bnode", "value": "b0"}) == BNode("b0")
    assert _node_from_sparql_json({"type": "literal", "value": "Alice"}) == Literal(
        "Alice"
    )
    assert _node_from_sparql_json(
        {"type": "literal", "value": "42", "datatype": str(XSD.integer)}
    ) == Literal("42", datatype=XSD.integer)
    assert _node_from_sparql_json(
        {"type": "literal", "value": "chat", "xml:lang": "fr"}
    ) == Literal("chat", lang="fr")
    assert _node_from_sparql_json({"type": "uri"}) is None


def test_add_violation_results_combines_bindings_and_metadata_without_cardinality_value() -> (
    None
):
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property ex:NameShape .
        ex:NameShape a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount 1 ;
          sh:message "Name is required" .
        """
    )

    graph = validation_report(truncated=False, has_targets=True)
    added = add_violation_results(
        graph,
        compiled,
        [{"focus_node": {"type": "uri", "value": str(EX.Alice)}}],
    )

    assert added == 1
    report = _report_node(graph)
    result = next(graph.objects(report, SH.result))
    assert graph.value(result, RDF.type) == SH.ValidationResult
    assert graph.value(result, SH.focusNode) == EX.Alice
    assert graph.value(result, SH.sourceShape) == EX.NameShape
    assert graph.value(result, SH.sourceConstraintComponent) == (
        SH.MinCountConstraintComponent
    )
    assert graph.value(result, SH.resultSeverity) == SH.Violation
    assert graph.value(result, SH.value) is None
    assert graph.value(result, SH.resultPath) == EX.name
    assert graph.value(result, SH.resultMessage) == Literal("Name is required")
    assert graph.value(report, SH.conforms) == Literal(False)


def test_add_violation_results_raises_on_missing_focus_node() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property ex:NameShape .
        ex:NameShape a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount 1 .
        """
    )

    graph = validation_report(truncated=False, has_targets=True)
    with pytest.raises(ValueError, match="missing focus_node"):
        add_violation_results(graph, compiled, [{}])


def test_add_property_path_encodes_complex_paths() -> None:
    graph = Graph()
    inverse = _add_property_path(graph, InversePath(PredicatePath(EX.manages)))
    assert graph.value(inverse, SH.inversePath) == EX.manages

    sequence = _add_property_path(
        graph, SequencePath([PredicatePath(EX.a), PredicatePath(EX.b)])
    )
    assert graph.value(sequence, RDF.first) == EX.a
    rest = graph.value(sequence, RDF.rest)
    assert graph.value(rest, RDF.first) == EX.b

    alternative = _add_property_path(
        graph, AlternativePath([PredicatePath(EX.a), PredicatePath(EX.b)])
    )
    members = graph.value(alternative, SH.alternativePath)
    assert members is not None
    assert graph.value(members, RDF.first) == EX.a


def test_typed_literal_value_survives_report_assembly() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:datatype xsd:integer .
        """
    )
    graph = validation_report(truncated=True, has_targets=True)
    add_violation_results(
        graph,
        compiled,
        [
            {
                "focus_node": {"type": "uri", "value": str(EX.Alice)},
                "value": {
                    "type": "literal",
                    "value": "n/a",
                    "datatype": str(XSD.string),
                },
            }
        ],
    )
    report = _report_node(graph)
    result = next(graph.objects(report, SH.result))
    assert graph.value(result, SH.value) == Literal("n/a", datatype=XSD.string)
    assert graph.value(report, VKG.truncated) == Literal(True)


def test_conforming_report_has_no_results() -> None:
    graph = validation_report(truncated=False, has_targets=False)
    report = _report_node(graph)
    assert graph.value(report, SH.conforms) == Literal(True)
    assert graph.value(report, VKG.hasTargets) == Literal(False)
    assert list(graph.objects(report, SH.result)) == []
