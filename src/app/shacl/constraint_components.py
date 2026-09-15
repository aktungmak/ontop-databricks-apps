"""SHACL Core constraint components and RDF parsing."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Callable, ClassVar, Self, TypeVar

from rdflib import Graph, Node, SH, URIRef

from .rdf_tools import (
    expand_rdf_list,
    literal_str,
    optional_bool,
    optional_int,
)
from .types import ClassRef, IllFormedShapeError, NodeKindRef, PropertyRef, ShapeRef

C = TypeVar("C", bound="ConstraintComponent")
_CONSTRAINT_COMPONENTS: dict[URIRef, type[ConstraintComponent]] = {}


def constraint_component(
    component_iri: URIRef,
    *,
    mandatory: tuple[URIRef, ...],
    optional: tuple[URIRef, ...] = (),
    repeatable: bool = False,
) -> Callable[[type[C]], type[C]]:
    """Register a constraint component and its parameter index entries.

    ``repeatable`` follows SHACL Core: a component with a single parameter may
    appear multiple times, and each value is a separate constraint, unless the
    spec also says the shape has at most one value (``sh:datatype``,
    ``sh:minCount``, ``sh:in``, …). Multi-parameter components such as
    ``sh:pattern``/``sh:flags`` are not repeatable.
    """

    def register(cls: type[C]) -> type[C]:
        cls.component_iri = component_iri
        cls.mandatory_parameters = frozenset(mandatory)
        cls.optional_parameters = frozenset(optional)
        cls.repeatable = repeatable
        _CONSTRAINT_COMPONENTS[component_iri] = cls
        return cls

    return register


def _predicate_for_field(name: str) -> URIRef:
    return SH[name.rstrip("_")]


@dataclass
class ConstraintComponent:
    """SHACL Core constraint component.

    Class attributes define the constraint component. Instances of the class
    are constraints declared on a shape (parameters for this component).
    """

    component_iri: ClassVar[URIRef]
    mandatory_parameters: ClassVar[frozenset[URIRef]]
    optional_parameters: ClassVar[frozenset[URIRef]] = frozenset()
    repeatable: ClassVar[bool] = False

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | list[Self] | None:
        """Read this component's parameters from ``shape_node``.

        The default implementation stores RDF nodes as-is. Field names map to
        SHACL predicates by stripping a trailing ``_`` (``class_`` → ``sh:class``).
        Repeatable components must have a single instance field; each value of
        the single mandatory parameter becomes one constraint instance.

        Override when a parameter is a typed literal, an RDF list, or otherwise
        is not the raw node.
        """
        instance_fields = fields(cls)
        if cls.repeatable:
            if len(instance_fields) != 1 or len(cls.mandatory_parameters) != 1:
                raise TypeError(
                    f"{cls.__name__} is marked repeatable but does not map a "
                    "single field to a single mandatory parameter"
                )
            (predicate,) = cls.mandatory_parameters
            field_name = instance_fields[0].name
            values = list(graph.objects(shape_node, predicate))
            if not values:
                return None
            return [cls(**{field_name: obj}) for obj in values]

        kwargs: dict[str, object] = {}
        for instance_field in instance_fields:
            predicate = _predicate_for_field(instance_field.name)
            node = graph.value(shape_node, predicate)
            if node is None:
                if predicate in cls.mandatory_parameters:
                    return None
                continue
            kwargs[instance_field.name] = node
        return cls(**kwargs)


def parse_constraints(graph: Graph, shape_node: ShapeRef) -> list[ConstraintComponent]:
    predicates_on_shape = set(graph.predicates(shape_node, unique=True))
    constraints: list[ConstraintComponent] = []

    for component_cls in _CONSTRAINT_COMPONENTS.values():
        if not component_cls.mandatory_parameters.issubset(predicates_on_shape):
            continue
        if not component_cls.repeatable:
            params = (
                component_cls.mandatory_parameters | component_cls.optional_parameters
            )
            for predicate in params:
                values = list(graph.objects(shape_node, predicate))
                if len(values) > 1:
                    raise IllFormedShapeError(
                        f"Shape {shape_node} has {len(values)} values for "
                        f"{predicate}, but {component_cls.component_iri} "
                        "is not repeatable"
                    )
        parsed = component_cls.parse(graph, shape_node)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            constraints.extend(parsed)
        else:
            constraints.append(parsed)
    return constraints


@constraint_component(
    SH.AndConstraintComponent, mandatory=(SH["and"],), repeatable=True
)
@dataclass
class AndConstraintComponent(ConstraintComponent):
    shapes: list[ShapeRef] = field(default_factory=list)

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> list[Self] | None:
        heads = list(graph.objects(shape_node, SH["and"]))
        if not heads:
            return None
        return [cls(shapes=expand_rdf_list(graph, head)) for head in heads]


@constraint_component(
    SH.ClassConstraintComponent, mandatory=(SH["class"],), repeatable=True
)
@dataclass
class ClassConstraintComponent(ConstraintComponent):
    class_: ClassRef


@constraint_component(
    SH.ClosedConstraintComponent,
    mandatory=(SH.closed,),
    optional=(SH.ignoredProperties,),
)
@dataclass
class ClosedConstraintComponent(ConstraintComponent):
    closed: bool
    ignoredProperties: list[PropertyRef] = field(default_factory=list)

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        closed = optional_bool(graph, shape_node, SH.closed)
        if closed is None:
            return None
        head = graph.value(shape_node, SH.ignoredProperties)
        ignored: list[PropertyRef] = []
        if head is not None:
            ignored = expand_rdf_list(graph, head)
            for member in ignored:
                if not isinstance(member, URIRef):
                    raise IllFormedShapeError(
                        f"Shape {shape_node} sh:ignoredProperties members must "
                        f"be IRIs, got {member!r}"
                    )
        return cls(closed=closed, ignoredProperties=ignored)


@constraint_component(SH.DatatypeConstraintComponent, mandatory=(SH.datatype,))
@dataclass
class DatatypeConstraintComponent(ConstraintComponent):
    datatype: URIRef


@constraint_component(
    SH.DisjointConstraintComponent, mandatory=(SH.disjoint,), repeatable=True
)
@dataclass
class DisjointConstraintComponent(ConstraintComponent):
    disjoint: PropertyRef


@constraint_component(
    SH.EqualsConstraintComponent, mandatory=(SH.equals,), repeatable=True
)
@dataclass
class EqualsConstraintComponent(ConstraintComponent):
    equals: PropertyRef


@constraint_component(
    SH.HasValueConstraintComponent, mandatory=(SH.hasValue,), repeatable=True
)
@dataclass
class HasValueConstraintComponent(ConstraintComponent):
    hasValue: Node


@constraint_component(SH.InConstraintComponent, mandatory=(SH["in"],))
@dataclass
class InConstraintComponent(ConstraintComponent):
    in_: list[Node] = field(default_factory=list)

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        head = graph.value(shape_node, SH["in"])
        if head is None:
            return None
        return cls(in_=expand_rdf_list(graph, head))


@constraint_component(SH.LanguageInConstraintComponent, mandatory=(SH.languageIn,))
@dataclass
class LanguageInConstraintComponent(ConstraintComponent):
    languageIn: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        head = graph.value(shape_node, SH.languageIn)
        if head is None:
            return None
        return cls(
            languageIn=[
                literal_str(member, shape_node, SH.languageIn)
                for member in expand_rdf_list(graph, head)
            ]
        )


@constraint_component(
    SH.LessThanConstraintComponent, mandatory=(SH.lessThan,), repeatable=True
)
@dataclass
class LessThanConstraintComponent(ConstraintComponent):
    lessThan: PropertyRef


@constraint_component(
    SH.LessThanOrEqualsConstraintComponent,
    mandatory=(SH.lessThanOrEquals,),
    repeatable=True,
)
@dataclass
class LessThanOrEqualsConstraintComponent(ConstraintComponent):
    lessThanOrEquals: PropertyRef


@constraint_component(SH.MaxCountConstraintComponent, mandatory=(SH.maxCount,))
@dataclass
class MaxCountConstraintComponent(ConstraintComponent):
    maxCount: int

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        value = optional_int(graph, shape_node, SH.maxCount)
        return None if value is None else cls(maxCount=value)


@constraint_component(SH.MaxExclusiveConstraintComponent, mandatory=(SH.maxExclusive,))
@dataclass
class MaxExclusiveConstraintComponent(ConstraintComponent):
    maxExclusive: Node


@constraint_component(SH.MaxInclusiveConstraintComponent, mandatory=(SH.maxInclusive,))
@dataclass
class MaxInclusiveConstraintComponent(ConstraintComponent):
    maxInclusive: Node


@constraint_component(SH.MaxLengthConstraintComponent, mandatory=(SH.maxLength,))
@dataclass
class MaxLengthConstraintComponent(ConstraintComponent):
    maxLength: int

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        value = optional_int(graph, shape_node, SH.maxLength)
        return None if value is None else cls(maxLength=value)


@constraint_component(SH.MinCountConstraintComponent, mandatory=(SH.minCount,))
@dataclass
class MinCountConstraintComponent(ConstraintComponent):
    minCount: int

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        value = optional_int(graph, shape_node, SH.minCount)
        return None if value is None else cls(minCount=value)


@constraint_component(SH.MinExclusiveConstraintComponent, mandatory=(SH.minExclusive,))
@dataclass
class MinExclusiveConstraintComponent(ConstraintComponent):
    minExclusive: Node


@constraint_component(SH.MinInclusiveConstraintComponent, mandatory=(SH.minInclusive,))
@dataclass
class MinInclusiveConstraintComponent(ConstraintComponent):
    minInclusive: Node


@constraint_component(SH.MinLengthConstraintComponent, mandatory=(SH.minLength,))
@dataclass
class MinLengthConstraintComponent(ConstraintComponent):
    minLength: int

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        value = optional_int(graph, shape_node, SH.minLength)
        return None if value is None else cls(minLength=value)


@constraint_component(
    SH.NodeConstraintComponent, mandatory=(SH.node,), repeatable=True
)
@dataclass
class NodeConstraintComponent(ConstraintComponent):
    shape: ShapeRef


@constraint_component(SH.NodeKindConstraintComponent, mandatory=(SH.nodeKind,))
@dataclass
class NodeKindConstraintComponent(ConstraintComponent):
    nodeKind: NodeKindRef


@constraint_component(
    SH.NotConstraintComponent, mandatory=(SH["not"],), repeatable=True
)
@dataclass
class NotConstraintComponent(ConstraintComponent):
    shape: ShapeRef


@constraint_component(
    SH.OrConstraintComponent, mandatory=(SH["or"],), repeatable=True
)
@dataclass
class OrConstraintComponent(ConstraintComponent):
    shapes: list[ShapeRef] = field(default_factory=list)

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> list[Self] | None:
        heads = list(graph.objects(shape_node, SH["or"]))
        if not heads:
            return None
        return [cls(shapes=expand_rdf_list(graph, head)) for head in heads]


@constraint_component(
    SH.PatternConstraintComponent,
    mandatory=(SH.pattern,),
    optional=(SH.flags,),
)
@dataclass
class PatternConstraintComponent(ConstraintComponent):
    pattern: str
    flags: str | None = None

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        pattern_node = graph.value(shape_node, SH.pattern)
        if pattern_node is None:
            return None
        flags_node = graph.value(shape_node, SH.flags)
        return cls(
            pattern=literal_str(pattern_node, shape_node, SH.pattern),
            flags=(
                literal_str(flags_node, shape_node, SH.flags)
                if flags_node is not None
                else None
            ),
        )


@constraint_component(
    SH.QualifiedMaxCountConstraintComponent,
    mandatory=(SH.qualifiedValueShape, SH.qualifiedMaxCount),
    optional=(SH.qualifiedValueShapesDisjoint,),
)
@dataclass
class QualifiedMaxCountConstraintComponent(ConstraintComponent):
    qualifiedValueShape: ShapeRef
    qualifiedMaxCount: int
    qualifiedValueShapesDisjoint: bool | None = None

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        shape = graph.value(shape_node, SH.qualifiedValueShape)
        count = optional_int(graph, shape_node, SH.qualifiedMaxCount)
        if shape is None or count is None:
            return None
        return cls(
            qualifiedValueShape=shape,
            qualifiedMaxCount=count,
            qualifiedValueShapesDisjoint=optional_bool(
                graph, shape_node, SH.qualifiedValueShapesDisjoint
            ),
        )


@constraint_component(
    SH.QualifiedMinCountConstraintComponent,
    mandatory=(SH.qualifiedValueShape, SH.qualifiedMinCount),
    optional=(SH.qualifiedValueShapesDisjoint,),
)
@dataclass
class QualifiedMinCountConstraintComponent(ConstraintComponent):
    qualifiedValueShape: ShapeRef
    qualifiedMinCount: int
    qualifiedValueShapesDisjoint: bool | None = None

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        shape = graph.value(shape_node, SH.qualifiedValueShape)
        count = optional_int(graph, shape_node, SH.qualifiedMinCount)
        if shape is None or count is None:
            return None
        return cls(
            qualifiedValueShape=shape,
            qualifiedMinCount=count,
            qualifiedValueShapesDisjoint=optional_bool(
                graph, shape_node, SH.qualifiedValueShapesDisjoint
            ),
        )


@dataclass
class SparqlConstraintRef(ConstraintComponent):
    """Reference to a node describing a SPARQL-based constraint (sh:sparql)."""

    sparql: ShapeRef


@constraint_component(SH.UniqueLangConstraintComponent, mandatory=(SH.uniqueLang,))
@dataclass
class UniqueLangConstraintComponent(ConstraintComponent):
    uniqueLang: bool

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> Self | None:
        value = optional_bool(graph, shape_node, SH.uniqueLang)
        return None if value is None else cls(uniqueLang=value)


@constraint_component(
    SH.XoneConstraintComponent, mandatory=(SH.xone,), repeatable=True
)
@dataclass
class XoneConstraintComponent(ConstraintComponent):
    xone: list[ShapeRef] = field(default_factory=list)

    @classmethod
    def parse(cls, graph: Graph, shape_node: ShapeRef) -> list[Self] | None:
        heads = list(graph.objects(shape_node, SH.xone))
        if not heads:
            return None
        return [cls(xone=expand_rdf_list(graph, head)) for head in heads]


__all__ = [
    "ConstraintComponent",
    "IllFormedShapeError",
    "SparqlConstraintRef",
    "constraint_component",
    "parse_constraints",
    *[cls.__name__ for cls in _CONSTRAINT_COMPONENTS.values()],
]
