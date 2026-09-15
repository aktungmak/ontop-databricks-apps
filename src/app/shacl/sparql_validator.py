"""SPARQL query generation for the supported SHACL Core constraints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from rdflib import Literal, Node, URIRef
from rdflib.namespace import SH

from .constraint_components import (
    ClassConstraintComponent,
    ConstraintComponent,
    DatatypeConstraintComponent,
    LessThanConstraintComponent,
    LessThanOrEqualsConstraintComponent,
    MaxCountConstraintComponent,
    MaxExclusiveConstraintComponent,
    MaxInclusiveConstraintComponent,
    MinCountConstraintComponent,
    MinExclusiveConstraintComponent,
    MinInclusiveConstraintComponent,
    NodeKindConstraintComponent,
    PatternConstraintComponent,
    QualifiedMaxCountConstraintComponent,
    QualifiedMinCountConstraintComponent,
    UniqueLangConstraintComponent,
)
from .shapes import (
    AlternativePath,
    InversePath,
    NodeShape,
    OneOrMorePath,
    PredicatePath,
    PropertyShape,
    PropertyPath,
    Severity,
    SequencePath,
    Shape,
    ShapesGraph,
    Target,
    TargetClass,
    TargetNode,
    TargetObjectsOf,
    TargetSubjectsOf,
    ZeroOrMorePath,
    ZeroOrOnePath,
)
from .types import IllFormedShapeError, ShapeRef

__all__ = [
    "ConstraintValidator",
    "SparqlValidator",
    "SparqlViolationQuery",
    "ViolationContext",
    "property_path_sparql",
    "sparql_validator",
    "validator_for",
]

C = TypeVar("C", bound=ConstraintComponent)
_SPARQL_VALIDATORS: dict[type[ConstraintComponent], ConstraintValidator] = {}


def property_path_sparql(path: PropertyPath) -> str:
    """Render a supported SHACL property path as SPARQL 1.1 path syntax."""
    if isinstance(path, PredicatePath):
        return path.predicate.n3()
    if isinstance(path, InversePath):
        inner = property_path_sparql(path.path)
        if isinstance(path.path, PredicatePath):
            return f"^{inner}"
        return f"^({inner})"
    if isinstance(path, SequencePath):
        parts = [
            f"({property_path_sparql(part)})"
            if isinstance(part, AlternativePath)
            else property_path_sparql(part)
            for part in path.paths
        ]
        return " / ".join(parts)
    if isinstance(path, AlternativePath):
        return " | ".join(property_path_sparql(part) for part in path.paths)
    if isinstance(path, ZeroOrMorePath):
        raise NotImplementedError(
            "ZeroOrMorePath (sh:zeroOrMorePath / *) is not supported"
        )
    if isinstance(path, OneOrMorePath):
        raise NotImplementedError(
            "OneOrMorePath (sh:oneOrMorePath / +) is not supported"
        )
    if isinstance(path, ZeroOrOnePath):
        raise NotImplementedError(
            "ZeroOrOnePath (sh:zeroOrOnePath / ?) is not supported"
        )
    raise TypeError(f"Unknown PropertyPath {path!r}")


def sparql_validator(
    component: type[C],
) -> Callable[[type[ConstraintValidator]], type[ConstraintValidator]]:
    """Register a SPARQL validator for a SHACL constraint component."""

    def register(cls: type[ConstraintValidator]) -> type[ConstraintValidator]:
        cls.component = component
        _SPARQL_VALIDATORS[component] = cls()
        return cls

    return register


def validator_for(constraint: ConstraintComponent) -> ConstraintValidator | None:
    return _SPARQL_VALIDATORS.get(type(constraint))


@dataclass(frozen=True)
class SparqlViolationQuery:
    """A query plus the metadata needed to construct validation results."""

    query: str
    shape_ref: ShapeRef
    constraint_iri: URIRef
    path: str | None
    message: str | None
    severity: URIRef


@dataclass(frozen=True)
class ViolationContext:
    shapes_graph: ShapesGraph
    focus_nodes_sparql: str
    value_nodes_sparql: str
    path_sparql: str | None
    shape_ref: ShapeRef
    message: str | None
    severity: URIRef


class ConstraintValidator:
    component: type[ConstraintComponent]

    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        raise NotImplementedError

    def _query(
        self,
        ctx: ViolationContext,
        constraint: ConstraintComponent,
        query: str,
    ) -> SparqlViolationQuery:
        result_path = ctx.path_sparql
        if (
            result_path is not None
            and result_path.startswith("<")
            and result_path.endswith(">")
            and result_path.count("<") == 1
            and result_path.count(">") == 1
        ):
            result_path = result_path[1:-1]
        return SparqlViolationQuery(
            query=query,
            shape_ref=ctx.shape_ref,
            constraint_iri=type(constraint).component_iri,
            path=result_path,
            message=ctx.message,
            severity=ctx.severity,
        )


@sparql_validator(MinCountConstraintComponent)
class MinCountValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MinCountConstraintComponent)
        assert ctx.path_sparql is not None
        query = f"""SELECT ?focus_node
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  OPTIONAL {{ ?focus_node {ctx.path_sparql} ?value }}
}}
GROUP BY ?focus_node
HAVING (COUNT(?value) < {constraint.minCount})"""
        return self._query(ctx, constraint, query)


@sparql_validator(MaxCountConstraintComponent)
class MaxCountValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MaxCountConstraintComponent)
        assert ctx.path_sparql is not None
        query = f"""SELECT ?focus_node
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  ?focus_node {ctx.path_sparql} ?value
}}
GROUP BY ?focus_node
HAVING (COUNT(?value) > {constraint.maxCount})"""
        return self._query(ctx, constraint, query)


@sparql_validator(ClassConstraintComponent)
class ClassValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, ClassConstraintComponent)
        class_iri = constraint.class_.n3()
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  {ctx.value_nodes_sparql}
  FILTER (isLiteral(?value) || NOT EXISTS {{
    ?value <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>/<http://www.w3.org/2000/01/rdf-schema#subClassOf>* {class_iri} .
  }})
}}"""
        return self._query(ctx, constraint, query)


_NODEKIND_VIOLATION_FILTERS = {
    SH.IRI: "FILTER (!isIRI(?value))",
    SH.Literal: "FILTER (!isLiteral(?value))",
    SH.BlankNode: "FILTER (!isBlank(?value))",
    SH.BlankNodeOrIRI: "FILTER (isLiteral(?value))",
    SH.BlankNodeOrLiteral: "FILTER (isIRI(?value))",
    SH.IRIOrLiteral: "FILTER (isBlank(?value))",
}


@sparql_validator(NodeKindConstraintComponent)
class NodeKindValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, NodeKindConstraintComponent)
        filt = _NODEKIND_VIOLATION_FILTERS.get(constraint.nodeKind)
        if filt is None:
            raise IllFormedShapeError(f"Unknown sh:nodeKind {constraint.nodeKind}")
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  {ctx.value_nodes_sparql}
  {filt}
}}"""
        return self._query(ctx, constraint, query)


@sparql_validator(DatatypeConstraintComponent)
class DatatypeValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, DatatypeConstraintComponent)
        datatype = constraint.datatype.n3()
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  {ctx.value_nodes_sparql}
  FILTER (!isLiteral(?value) || datatype(?value) != {datatype})
}}"""
        return self._query(ctx, constraint, query)


@sparql_validator(PatternConstraintComponent)
class PatternValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, PatternConstraintComponent)
        pattern = Literal(constraint.pattern).n3()
        flags = f", {Literal(constraint.flags).n3()}" if constraint.flags else ""
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  {ctx.value_nodes_sparql}
  FILTER (!REGEX(STR(?value), {pattern}{flags}))
}}"""
        return self._query(ctx, constraint, query)


def _range_violation_query(ctx: ViolationContext, bound: Node, operator: str) -> str:
    bound_n3 = bound.n3()
    # COALESCE turns SPARQL comparison errors into violations
    # type guards cover rdflib treating some incomparable pairs as true (SHACL §4.3).
    comparable = (
        f"((isNumeric({bound_n3}) && isNumeric(?value)) "
        f"|| (datatype({bound_n3}) = datatype(?value)))"
    )
    return f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  {ctx.value_nodes_sparql}
  FILTER (!COALESCE({comparable} && ({bound_n3} {operator} ?value), false))
}}"""


@sparql_validator(MinExclusiveConstraintComponent)
class MinExclusiveValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MinExclusiveConstraintComponent)
        return self._query(
            ctx, constraint, _range_violation_query(ctx, constraint.minExclusive, "<")
        )


@sparql_validator(MinInclusiveConstraintComponent)
class MinInclusiveValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MinInclusiveConstraintComponent)
        return self._query(
            ctx, constraint, _range_violation_query(ctx, constraint.minInclusive, "<=")
        )


@sparql_validator(MaxExclusiveConstraintComponent)
class MaxExclusiveValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MaxExclusiveConstraintComponent)
        return self._query(
            ctx, constraint, _range_violation_query(ctx, constraint.maxExclusive, ">")
        )


@sparql_validator(MaxInclusiveConstraintComponent)
class MaxInclusiveValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MaxInclusiveConstraintComponent)
        return self._query(
            ctx, constraint, _range_violation_query(ctx, constraint.maxInclusive, ">=")
        )


class SparqlValidator:
    """Compile supported SHACL constraints to violation SELECTs."""

    def __init__(self, shapes_graph: ShapesGraph):
        self.shapes_graph = shapes_graph

    def validate(self) -> list[SparqlViolationQuery]:
        """Return executable violation queries; report assembly is a later step."""
        queries: list[SparqlViolationQuery] = []
        for shape_ref, shape in self.shapes_graph.shapes.items():
            queries.extend(self.validation_results(shape_ref, shape))
        return queries

    def validation_results(
        self, shape_ref: ShapeRef, shape: Shape
    ) -> list[SparqlViolationQuery]:
        if shape.deactivated:
            return []

        focus_nodes_sparql = self._focus_nodes_sparql(shape_ref)
        if focus_nodes_sparql is None:
            return []

        return self._validation_results_for_focus(
            shape_ref, shape, focus_nodes_sparql
        )

    def _validation_results_for_focus(
        self,
        shape_ref: ShapeRef,
        shape: Shape,
        focus_nodes_sparql: str,
        nested_ancestors: set[ShapeRef] | None = None,
    ) -> list[SparqlViolationQuery]:
        if nested_ancestors is None:
            nested_ancestors = {shape_ref}
        path_sparql: str | None = None
        if isinstance(shape, PropertyShape):
            path_sparql = property_path_sparql(shape.path)
            value_nodes_sparql = f"?focus_node {path_sparql} ?value ."
        else:
            self._check_node_shape_parameters(shape_ref, shape)
            value_nodes_sparql = "BIND (?focus_node AS ?value)"

        severity = shape.severity or Severity.VIOLATION
        severity_iri = severity.value if isinstance(severity, Severity) else severity
        ctx = ViolationContext(
            shapes_graph=self.shapes_graph,
            focus_nodes_sparql=focus_nodes_sparql,
            value_nodes_sparql=value_nodes_sparql,
            path_sparql=path_sparql,
            shape_ref=shape_ref,
            message=shape.message[0] if shape.message else None,
            severity=severity_iri,
        )

        queries: list[SparqlViolationQuery] = []
        for constraint in shape.constraints:
            validator = validator_for(constraint)
            if validator is None:
                # Fail loudly until every Core component in the shapes graph has SPARQL.
                raise NotImplementedError(f"No SPARQL validator for {constraint!r}")
            queries.append(validator.violations(ctx, constraint))

        if isinstance(shape, PropertyShape):
            queries.extend(
                self._nested_property_results(
                    shape, focus_nodes_sparql, nested_ancestors
                )
            )
        return queries

    def _nested_property_results(
        self,
        parent: PropertyShape,
        parent_focus_sparql: str,
        ancestors: set[ShapeRef],
    ) -> list[SparqlViolationQuery]:
        parent_path = property_path_sparql(parent.path)
        parent_var = f"?parent_focus_{len(ancestors)}"
        nested_focus_sparql = (
            "SELECT DISTINCT ?focus_node WHERE {\n"
            f"    {{ {parent_focus_sparql.replace('?focus_node', parent_var)} }}\n"
            f"    {parent_var} {parent_path} ?focus_node .\n"
            "  }"
        )
        queries: list[SparqlViolationQuery] = []
        for child_ref in parent.property:
            if child_ref in ancestors:
                continue
            child = self.shapes_graph.shapes.get(child_ref)
            if not isinstance(child, PropertyShape) or child.deactivated:
                continue
            if any(
                not node_parent.deactivated and node_parent.targets
                for node_parent in self.shapes_graph.parent_node_shapes(child_ref)
            ):
                continue
            queries.extend(
                self._validation_results_for_focus(
                    child_ref,
                    child,
                    nested_focus_sparql,
                    ancestors | {child_ref},
                )
            )
        return queries

    @staticmethod
    def _check_node_shape_parameters(shape_ref: ShapeRef, shape: Shape) -> None:
        property_only = (
            MinCountConstraintComponent,
            MaxCountConstraintComponent,
            UniqueLangConstraintComponent,
            LessThanConstraintComponent,
            LessThanOrEqualsConstraintComponent,
            QualifiedMinCountConstraintComponent,
            QualifiedMaxCountConstraintComponent,
        )
        for constraint in shape.constraints:
            if isinstance(constraint, property_only):
                raise IllFormedShapeError(
                    f"Node shape {shape_ref} is ill-formed: "
                    f"{type(constraint).component_iri.n3()} is property-shape only"
                )

    def _focus_nodes_sparql(self, shape_ref: ShapeRef) -> str | None:
        """Compile effective targets to a focus-node subquery."""
        target_patterns = [
            pattern
            for target in self.shapes_graph.effective_targets(shape_ref)
            if (pattern := self._focus_nodes_for_target(target)) is not None
        ]
        if not target_patterns:
            return None
        unions = "\n    UNION\n    ".join(f"{{ {part} }}" for part in target_patterns)
        return (
            "SELECT DISTINCT ?focus_node WHERE {\n"
            f"    {unions}\n"
            "  }"
        )

    def _focus_nodes_for_target(self, target: Target) -> str | None:
        if isinstance(target, TargetClass):
            return (
                "?focus_node "
                "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
                f"{target.targetClass.n3()} ."
            )
        if isinstance(target, TargetNode):
            return f"VALUES ?focus_node {{ {target.targetNode.n3()} }}"
        if isinstance(target, TargetSubjectsOf):
            return f"?focus_node {target.targetSubjectsOf.n3()} ?any ."
        if isinstance(target, TargetObjectsOf):
            return f"?any {target.targetObjectsOf.n3()} ?focus_node ."
        return None
