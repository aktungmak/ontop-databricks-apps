"""SPARQL query generation for the supported SHACL Core constraints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, TypeVar

from rdflib import Literal, Node, URIRef
from rdflib.namespace import SH

from .constraint_components import (
    ClassConstraintComponent,
    ConstraintComponent,
    DatatypeConstraintComponent,
    MaxCountConstraintComponent,
    MaxExclusiveConstraintComponent,
    MaxInclusiveConstraintComponent,
    MinCountConstraintComponent,
    MinExclusiveConstraintComponent,
    MinInclusiveConstraintComponent,
    NodeKindConstraintComponent,
    PatternConstraintComponent,
)
from .shapes import (
    PropertyShape,
    Severity,
    Shape,
    ShapesGraph,
    Target,
    TargetClass,
    TargetNode,
    TargetObjectsOf,
    TargetSubjectsOf,
    PredicatePath,
)
from .types import IllFormedShapeError, ShapeRef

__all__ = [
    "ConstraintValidator",
    "SparqlValidator",
    "SparqlViolationQuery",
    "ViolationContext",
    "sparql_validator",
    "validator_for",
]

logger = logging.getLogger(__name__)

C = TypeVar("C", bound=ConstraintComponent)
_SPARQL_VALIDATORS: dict[type[ConstraintComponent], ConstraintValidator] = {}


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
    path: str
    message: str | None
    severity: URIRef


@dataclass(frozen=True)
class ViolationContext:
    shapes_graph: ShapesGraph
    focus_nodes_sparql: str
    path: URIRef
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
        return SparqlViolationQuery(
            query=query,
            shape_ref=ctx.shape_ref,
            constraint_iri=type(constraint).component_iri,
            path=str(ctx.path),
            message=ctx.message,
            severity=ctx.severity,
        )


@sparql_validator(MinCountConstraintComponent)
class MinCountValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, MinCountConstraintComponent)
        path = ctx.path.n3()
        query = f"""SELECT ?focus_node
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  OPTIONAL {{ ?focus_node {path} ?value }}
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
        path = ctx.path.n3()
        query = f"""SELECT ?focus_node
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  ?focus_node {path} ?value
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
        path = ctx.path.n3()
        class_iri = constraint.class_.n3()
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  ?focus_node {path} ?value .
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
        path = ctx.path.n3()
        filt = _NODEKIND_VIOLATION_FILTERS.get(constraint.nodeKind)
        if filt is None:
            raise IllFormedShapeError(f"Unknown sh:nodeKind {constraint.nodeKind}")
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  ?focus_node {path} ?value .
  {filt}
}}"""
        return self._query(ctx, constraint, query)


@sparql_validator(DatatypeConstraintComponent)
class DatatypeValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, DatatypeConstraintComponent)
        path = ctx.path.n3()
        datatype = constraint.datatype.n3()
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  ?focus_node {path} ?value .
  FILTER (!isLiteral(?value) || datatype(?value) != {datatype})
}}"""
        return self._query(ctx, constraint, query)


@sparql_validator(PatternConstraintComponent)
class PatternValidator(ConstraintValidator):
    def violations(
        self, ctx: ViolationContext, constraint: ConstraintComponent
    ) -> SparqlViolationQuery:
        assert isinstance(constraint, PatternConstraintComponent)
        path = ctx.path.n3()
        pattern = Literal(constraint.pattern).n3()
        flags = f", {Literal(constraint.flags).n3()}" if constraint.flags else ""
        query = f"""SELECT DISTINCT ?focus_node ?value
WHERE {{
  {{ {ctx.focus_nodes_sparql} }}
  ?focus_node {path} ?value .
  FILTER (!REGEX(STR(?value), {pattern}{flags}))
}}"""
        return self._query(ctx, constraint, query)


def _range_violation_query(ctx: ViolationContext, bound: Node, operator: str) -> str:
    path = ctx.path.n3()
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
  ?focus_node {path} ?value .
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
    """Compile supported property-shape constraints to violation SELECTs."""

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
        if shape.deactivated or not isinstance(shape, PropertyShape):
            return []
        if not isinstance(shape.path, PredicatePath):
            logger.info(
                "Only IRI sh:path values are supported for %s; skipping", shape_ref
            )
            return []

        focus_nodes_sparql = self._focus_nodes_sparql(shape_ref)
        if focus_nodes_sparql is None:
            return []

        severity = shape.severity or Severity.VIOLATION
        severity_iri = severity.value if isinstance(severity, Severity) else severity
        ctx = ViolationContext(
            shapes_graph=self.shapes_graph,
            focus_nodes_sparql=focus_nodes_sparql,
            path=shape.path.predicate,
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
        return queries

    def _focus_nodes_sparql(self, shape_ref: ShapeRef) -> str | None:
        """Compile effective targets to an IRI-only focus-node subquery."""
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
            "    FILTER (isIRI(?focus_node))\n"
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
