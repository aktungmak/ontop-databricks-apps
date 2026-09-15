"""RDF list walking and typed-literal helpers used while parsing shapes."""

from __future__ import annotations

from typing import TypeVar

from rdflib import Graph, Literal, Node, URIRef
from rdflib.namespace import RDF

from .types import IllFormedShapeError, ShapeRef

T = TypeVar("T")

__all__ = [
    "expand_rdf_list",
    "literal_str",
    "literal_strings",
    "optional_bool",
    "optional_int",
    "optional_typed",
    "typed_literal",
]


def expand_rdf_list(graph: Graph, head: Node) -> list[Node]:
    """Return the members of a well-formed RDF list starting at ``head``.

    ``rdf:nil`` is the empty list. Duplicate members are allowed. Cycles,
    missing or repeated ``rdf:first`` / ``rdf:rest``, and a non-list node
    where a list is required raise ``IllFormedShapeError``.
    """
    if head == RDF.nil:
        return []

    firsts = list(graph.objects(head, RDF.first))
    rests = list(graph.objects(head, RDF.rest))
    if not firsts and not rests:
        raise IllFormedShapeError(f"Expected an RDF list, got non-list node {head!r}")

    members: list[Node] = []
    seen: set[Node] = set()
    current: Node = head
    while current != RDF.nil:
        if current in seen:
            raise IllFormedShapeError(f"RDF list has a cycle at {current}")
        seen.add(current)
        firsts = list(graph.objects(current, RDF.first))
        rests = list(graph.objects(current, RDF.rest))
        if len(firsts) != 1:
            raise IllFormedShapeError(
                f"RDF list node {current} must have exactly one rdf:first, "
                f"got {len(firsts)}"
            )
        if len(rests) != 1:
            raise IllFormedShapeError(
                f"RDF list node {current} must have exactly one rdf:rest, "
                f"got {len(rests)}"
            )
        members.append(firsts[0])
        current = rests[0]
    return members


def _python_value(node: Node, shape_node: ShapeRef, predicate: URIRef) -> object:
    if not isinstance(node, Literal):
        raise IllFormedShapeError(
            f"Shape {shape_node} value of {predicate} must be a literal, got {node!r}"
        )
    try:
        return node.toPython()
    except (TypeError, ValueError) as exc:
        raise IllFormedShapeError(
            f"Shape {shape_node} value of {predicate} is not a valid literal"
        ) from exc


def typed_literal(
    node: Node,
    shape_node: ShapeRef,
    predicate: URIRef,
    expected: type[T],
    spec_name: str,
) -> T:
    value = _python_value(node, shape_node, predicate)
    # bool is a subclass of int; SHACL numeric parameters must be integer literals.
    if type(value) is not expected:
        raise IllFormedShapeError(
            f"Shape {shape_node} value of {predicate} must be an {spec_name} literal, "
            f"got {node!r}"
        )
    return value


def optional_typed(
    graph: Graph,
    shape_node: ShapeRef,
    predicate: URIRef,
    expected: type[T],
    spec_name: str,
) -> T | None:
    node = graph.value(shape_node, predicate)
    if node is None:
        return None
    return typed_literal(node, shape_node, predicate, expected, spec_name)


def optional_bool(
    graph: Graph, shape_node: ShapeRef, predicate: URIRef
) -> bool | None:
    return optional_typed(graph, shape_node, predicate, bool, "xsd:boolean")


def optional_int(graph: Graph, shape_node: ShapeRef, predicate: URIRef) -> int | None:
    return optional_typed(graph, shape_node, predicate, int, "xsd:integer")


def literal_str(node: Node, shape_node: ShapeRef, predicate: URIRef) -> str:
    value = _python_value(node, shape_node, predicate)
    if type(value) is not str:
        raise IllFormedShapeError(
            f"Shape {shape_node} value of {predicate} must be a string literal, "
            f"got {node!r}"
        )
    return value


def literal_strings(
    graph: Graph, shape_node: ShapeRef, predicate: URIRef
) -> list[str]:
    return [
        literal_str(obj, shape_node, predicate)
        for obj in graph.objects(shape_node, predicate)
    ]
