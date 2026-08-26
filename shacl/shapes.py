"""SHACL shapes, targets, and shapes-graph parsing."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass, field
from enum import Enum

from rdflib import Graph, Literal, Node, SH, URIRef
from rdflib.namespace import RDF

from .constraint_components import ConstraintComponent, parse_constraints
from .rdf_tools import expand_rdf_list, literal_strings, optional_bool, optional_int
from .types import ClassRef, IllFormedShapeError, PropertyRef, ShapeRef


__all__ = [
    "AlternativePath",
    "InversePath",
    "OneOrMorePath",
    "PredicatePath",
    "PropertyPath",
    "SequencePath",
    "ZeroOrMorePath",
    "ZeroOrOnePath",
    "parse_property_path",
    "NodeShape",
    "PropertyShape",
    "Severity",
    "Shape",
    "ShapesGraph",
    "Target",
    "TargetClass",
    "TargetNode",
    "TargetObjectsOf",
    "TargetSubjectsOf",
    "parse_node_shape",
    "parse_property_shape",
]


class Severity(Enum):
    INFO = URIRef("http://www.w3.org/ns/shacl#Info")
    VIOLATION = URIRef("http://www.w3.org/ns/shacl#Violation")
    WARNING = URIRef("http://www.w3.org/ns/shacl#Warning")


@dataclass
class Target:
    """Base class for sh:Target values used via sh:target."""


@dataclass
class TargetClass(Target):
    targetClass: ClassRef


@dataclass
class TargetNode(Target):
    targetNode: Node


@dataclass
class TargetObjectsOf(Target):
    targetObjectsOf: PropertyRef


@dataclass
class TargetSubjectsOf(Target):
    targetSubjectsOf: PropertyRef


@dataclass(frozen=True)
class PropertyPath:
    """A well-formed SHACL property path."""


@dataclass(frozen=True)
class PredicatePath(PropertyPath):
    predicate: URIRef


@dataclass(frozen=True)
class InversePath(PropertyPath):
    path: PropertyPath


@dataclass(frozen=True)
class SequencePath(PropertyPath):
    paths: list[PropertyPath]


@dataclass(frozen=True)
class AlternativePath(PropertyPath):
    paths: list[PropertyPath]


@dataclass(frozen=True)
class ZeroOrMorePath(PropertyPath):
    path: PropertyPath


@dataclass(frozen=True)
class OneOrMorePath(PropertyPath):
    path: PropertyPath


@dataclass(frozen=True)
class ZeroOrOnePath(PropertyPath):
    path: PropertyPath


_PATH_CONSTRUCTORS = (
    SH.inversePath,
    SH.alternativePath,
    SH.zeroOrMorePath,
    SH.oneOrMorePath,
    SH.zeroOrOnePath,
)


def parse_property_path(graph: Graph, node: Node) -> PropertyPath:
    return _parse_property_path(graph, node, set())


def _parse_property_path(
    graph: Graph, node: Node, ancestors: set[Node]
) -> PropertyPath:
    if node in ancestors:
        raise IllFormedShapeError(f"Property path has a cycle at {node}")
    if node == RDF.nil:
        raise IllFormedShapeError("Empty sequence path is ill-formed")
    if isinstance(node, URIRef):
        return PredicatePath(predicate=node)
    if isinstance(node, Literal):
        raise IllFormedShapeError(f"Predicate path must be an IRI, got {node!r}")

    found: list[tuple[URIRef, Node]] = []
    for predicate in _PATH_CONSTRUCTORS:
        values = list(graph.objects(node, predicate))
        if len(values) > 1:
            raise IllFormedShapeError(
                f"Path node {node} has {len(values)} values for {predicate}"
            )
        if values:
            found.append((predicate, values[0]))
    if len(found) > 1:
        raise IllFormedShapeError(f"Path node {node} mixes path constructors")

    next_ancestors = ancestors | {node}
    if len(found) == 1:
        predicate, obj = found[0]
        if predicate == SH.alternativePath:
            members = expand_rdf_list(graph, obj)
            if not members:
                raise IllFormedShapeError("Empty alternative path is ill-formed")
            return AlternativePath(
                paths=[
                    _parse_property_path(graph, member, next_ancestors)
                    for member in members
                ]
            )
        inner = _parse_property_path(graph, obj, next_ancestors)
        if predicate == SH.inversePath:
            return InversePath(path=inner)
        if predicate == SH.zeroOrMorePath:
            return ZeroOrMorePath(path=inner)
        if predicate == SH.oneOrMorePath:
            return OneOrMorePath(path=inner)
        return ZeroOrOnePath(path=inner)

    if (node, RDF.first, None) in graph or (node, RDF.rest, None) in graph:
        members = expand_rdf_list(graph, node)
        if not members:
            raise IllFormedShapeError("Empty sequence path is ill-formed")
        return SequencePath(
            paths=[
                _parse_property_path(graph, member, next_ancestors)
                for member in members
            ]
        )

    raise IllFormedShapeError(
        f"Path node {node} is neither a path constructor nor an RDF list"
    )


@dataclass
class Shape:
    deactivated: bool = False
    message: list[str] = field(default_factory=list)
    severity: Severity | URIRef | None = None
    targets: list[Target] = field(default_factory=list)
    constraints: list[ConstraintComponent] = field(default_factory=list)
    property: list[ShapeRef] = field(default_factory=list)
    sparql: list[ShapeRef] = field(default_factory=list)


@dataclass
class NodeShape(Shape):
    pass


@dataclass
class PropertyShape(Shape):
    _: KW_ONLY
    path: PropertyPath
    defaultValue: Node | None = None
    description: list[str] = field(default_factory=list)
    group: ShapeRef | None = None
    name: list[str] = field(default_factory=list)
    order: int | None = None


def _parse_severity(graph: Graph, shape_node: ShapeRef) -> Severity | URIRef | None:
    val = graph.value(shape_node, SH.severity)
    if val is None:
        return None
    for member in Severity:
        if member.value == val:
            return member
    return val


def _parse_targets(graph: Graph, shape_node: ShapeRef) -> list[Target]:
    targets: list[Target] = []
    for obj in graph.objects(shape_node, SH.targetClass):
        targets.append(TargetClass(targetClass=obj))
    for obj in graph.objects(shape_node, SH.targetNode):
        targets.append(TargetNode(targetNode=obj))
    for obj in graph.objects(shape_node, SH.targetSubjectsOf):
        targets.append(TargetSubjectsOf(targetSubjectsOf=obj))
    for obj in graph.objects(shape_node, SH.targetObjectsOf):
        targets.append(TargetObjectsOf(targetObjectsOf=obj))
    return targets


def _shape_common_kwargs(graph: Graph, shape_node: ShapeRef) -> dict:
    return {
        "deactivated": optional_bool(graph, shape_node, SH.deactivated) or False,
        "message": literal_strings(graph, shape_node, SH.message),
        "severity": _parse_severity(graph, shape_node),
        "targets": _parse_targets(graph, shape_node),
        "constraints": parse_constraints(graph, shape_node),
        "property": list(graph.objects(shape_node, SH.property)),
        "sparql": list(graph.objects(shape_node, SH.sparql)),
    }


def parse_property_shape(graph: Graph, shape_node: ShapeRef) -> PropertyShape:
    path_nodes = list(graph.objects(shape_node, SH.path))
    if len(path_nodes) != 1:
        raise IllFormedShapeError(
            f"Property shape {shape_node} must have exactly one sh:path, "
            f"got {len(path_nodes)}"
        )
    common = _shape_common_kwargs(graph, shape_node)
    return PropertyShape(
        **common,
        path=parse_property_path(graph, path_nodes[0]),
        group=graph.value(shape_node, SH.group),
        defaultValue=graph.value(shape_node, SH.defaultValue),
        description=literal_strings(graph, shape_node, SH.description),
        name=literal_strings(graph, shape_node, SH.name),
        order=optional_int(graph, shape_node, SH.order),
    )


def parse_node_shape(graph: Graph, shape_node: ShapeRef) -> NodeShape:
    return NodeShape(**_shape_common_kwargs(graph, shape_node))


class ShapesGraph:
    """Index of parsed shapes keyed by shape node (URI or blank node)."""

    def __init__(self, shapes: dict[ShapeRef, Shape]):
        self.shapes = shapes

    def parent_node_shapes(self, shape_ref: ShapeRef) -> list[NodeShape]:
        """Node shapes that list ``shape_ref`` in ``sh:property``."""
        return [
            shape
            for shape in self.shapes.values()
            if isinstance(shape, NodeShape) and shape_ref in shape.property
        ]

    def effective_targets(self, shape_ref: ShapeRef) -> list[Target]:
        """Targets on the shape, or inherited from non-deactivated parent node shapes."""
        shape = self.shapes[shape_ref]
        if shape.targets:
            return shape.targets
        targets: list[Target] = []
        for parent in self.parent_node_shapes(shape_ref):
            if parent.deactivated:
                continue
            targets.extend(parent.targets)
        return targets

    @classmethod
    def from_graph(cls, graph: Graph) -> ShapesGraph:
        shapes: dict[ShapeRef, Shape] = {}
        for node in graph.subjects(RDF.type, SH.NodeShape):
            shapes[node] = parse_node_shape(graph, node)
        for node in graph.subjects(RDF.type, SH.PropertyShape):
            if node not in shapes:
                shapes[node] = parse_property_shape(graph, node)
        for node in graph.objects(None, SH.property):
            if node not in shapes:
                shapes[node] = parse_property_shape(graph, node)
        for shape_predicate in (
            SH.targetClass,
            SH.targetNode,
            SH.targetSubjectsOf,
            SH.targetObjectsOf,
        ):
            for node in graph.subjects(shape_predicate, None):
                if node in shapes:
                    continue
                if (node, SH.path, None) in graph:
                    shapes[node] = parse_property_shape(graph, node)
                else:
                    shapes[node] = parse_node_shape(graph, node)
        return cls(shapes)

    @classmethod
    def from_file(cls, path: str) -> ShapesGraph:
        graph = Graph()
        graph.parse(path)
        return cls.from_graph(graph)
