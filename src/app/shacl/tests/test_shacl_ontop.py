"""Live Ontop integration tests for the compiled SHACL Core validators."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
import pytest
from rdflib import Graph, Namespace, URIRef

from shacl.shapes import ShapesGraph
from shacl.sparql_validator import SparqlValidator

EX = Namespace("http://example.org/tpch/")
REGION_0 = EX["region/0"]
NATION_0 = EX["nation/0"]
SHACL_IT_ENDPOINT = os.environ.get("SHACL_IT_ENDPOINT")
SHACL_IT_TOKEN = os.environ.get("SHACL_IT_TOKEN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not SHACL_IT_ENDPOINT or not SHACL_IT_TOKEN,
        reason="set SHACL_IT_ENDPOINT and SHACL_IT_TOKEN for live Ontop tests",
    ),
]

_PREFIXES = f"""
@prefix ex: <{EX}> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


@dataclass(frozen=True)
class ExpectedBinding:
    focus: URIRef
    value: URIRef | str | None = None


@dataclass(frozen=True)
class ValidatorCase:
    name: str
    shape_kind: str
    target: str
    constraint: str
    path: str | None = None
    expected: tuple[ExpectedBinding, ...] = ()
    expect_empty: bool = False
    query_contains: tuple[str, ...] = ()


CASES = [
    ValidatorCase(
        "min-count",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minCount 2",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0),),
    ),
    ValidatorCase(
        "max-count",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxCount 1",
        path="[ sh:alternativePath ( ex:name ex:comment ) ]",
        expected=(ExpectedBinding(REGION_0),),
    ),
    ValidatorCase(
        "class",
        "PropertyShape",
        f"sh:targetNode {NATION_0.n3()}",
        "sh:class ex:Nation",
        path="ex:inRegion",
        expected=(ExpectedBinding(NATION_0, REGION_0),),
    ),
    ValidatorCase(
        "datatype",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:datatype xsd:string",
        path="ex:regionKey",
        expected=(ExpectedBinding(REGION_0, "0"),),
    ),
    ValidatorCase(
        "node-kind",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:nodeKind sh:IRI",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
    ),
    ValidatorCase(
        "pattern",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:pattern "^EUROPE$"',
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
    ),
    ValidatorCase(
        "min-exclusive",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minExclusive 0",
        path="ex:regionKey",
        expected=(ExpectedBinding(REGION_0, "0"),),
    ),
    ValidatorCase(
        "min-inclusive",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minInclusive 1",
        path="ex:regionKey",
        expected=(ExpectedBinding(REGION_0, "0"),),
    ),
    ValidatorCase(
        "max-exclusive",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxExclusive 0",
        path="ex:regionKey",
        expected=(ExpectedBinding(REGION_0, "0"),),
    ),
    ValidatorCase(
        "max-inclusive",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxInclusive -1",
        path="ex:regionKey",
        expected=(ExpectedBinding(REGION_0, "0"),),
    ),
    ValidatorCase(
        "target-class-direct",
        "PropertyShape",
        "sh:targetClass ex:Region",
        "sh:minCount 2",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0),),
    ),
    ValidatorCase(
        "target-class-subclass-closure",
        "PropertyShape",
        "sh:targetClass ex:GeographicArea",
        "sh:minCount 2",
        path="ex:name",
        expected=(
            ExpectedBinding(REGION_0),
            ExpectedBinding(NATION_0),
        ),
        query_contains=(
            "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
            "<http://example.org/tpch/GeographicArea>",
        ),
    ),
    ValidatorCase(
        "target-subjects-of",
        "PropertyShape",
        "sh:targetSubjectsOf ex:inRegion",
        "sh:minCount 2",
        path="ex:name",
        expected=(ExpectedBinding(NATION_0),),
    ),
    ValidatorCase(
        "target-objects-of",
        "PropertyShape",
        "sh:targetObjectsOf ex:inRegion",
        "sh:minCount 2",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0),),
    ),
    ValidatorCase(
        "node-shape-focus-value",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:class ex:Nation",
        expected=(ExpectedBinding(REGION_0, REGION_0),),
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "inverse-path-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minCount 1",
        path="[ sh:inversePath ex:inRegion ]",
        expect_empty=True,
    ),
    ValidatorCase(
        "inverse-path-violates",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minCount 1000",
        path="[ sh:inversePath ex:inRegion ]",
        expected=(ExpectedBinding(REGION_0),),
    ),
    ValidatorCase(
        "sequence-path-violates",
        "PropertyShape",
        f"sh:targetNode {NATION_0.n3()}",
        'sh:pattern "^EUROPE$"',
        path="( ex:inRegion ex:name )",
        expected=(ExpectedBinding(NATION_0, "AFRICA"),),
    ),
    ValidatorCase(
        "sequence-path-conforms",
        "PropertyShape",
        f"sh:targetNode {NATION_0.n3()}",
        "sh:datatype xsd:string",
        path="( ex:inRegion ex:name )",
        expect_empty=True,
    ),
    ValidatorCase(
        "alternative-path-distinct-cardinality",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxCount 1",
        path="[ sh:alternativePath ( ex:name ex:name ) ]",
        expect_empty=True,
        query_contains=("COUNT(DISTINCT ?value)",),
    ),
    ValidatorCase(
        "min-count-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minCount 1",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "pattern-case-insensitive-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:pattern "^africa$"; sh:flags "i"',
        path="ex:name",
        expect_empty=True,
        query_contains=('"^africa$", "i"',),
    ),
    ValidatorCase(
        "pattern-case-sensitive-violates",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:pattern "^africa$"',
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
    ),
    ValidatorCase(
        "node-kind-literal-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:nodeKind sh:Literal",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "node-kind-blank-node-violates",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:nodeKind sh:BlankNode",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
    ),
    ValidatorCase(
        "incomparable-range",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:minExclusive "x"^^xsd:string',
        path="ex:regionKey",
        expected=(ExpectedBinding(REGION_0, "0"),),
        query_contains=("FILTER (!COALESCE(",),
    ),
]


def _compile(case: ValidatorCase):
    path = f"\n  sh:path {case.path} ;" if case.path is not None else ""
    shapes_turtle = _PREFIXES + f"""
ex:IntegrationShape a sh:{case.shape_kind} ;
  {case.target} ;{path}
  {case.constraint} .
"""
    graph = Graph()
    graph.parse(data=shapes_turtle, format="turtle")
    queries = SparqlValidator(ShapesGraph.from_graph(graph)).validate()
    assert len(queries) == 1, (
        f"{case.name}: expected one query, got {len(queries)}\n"
        f"shape:\n{shapes_turtle}"
    )
    return shapes_turtle, queries[0]


def _diagnostics(
    case: ValidatorCase,
    shapes_turtle: str,
    compiled,
    response_text: str = "",
) -> str:
    response = (
        f"\nresponse (truncated):\n{response_text[:2000]}" if response_text else ""
    )
    return (
        f"\nconstraint: {compiled.constraint_iri}"
        f"\nshape:\n{shapes_turtle}"
        f"\nquery:\n{compiled.query}"
        f"{response}"
    )


def _sparql_endpoint() -> str:
    assert SHACL_IT_ENDPOINT is not None
    return (
        SHACL_IT_ENDPOINT.rstrip("/")
        if SHACL_IT_ENDPOINT.rstrip("/").endswith("/sparql")
        else f"{SHACL_IT_ENDPOINT.rstrip('/')}/sparql"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_compiled_validator_against_tpch(case: ValidatorCase) -> None:
    shapes_turtle, compiled = _compile(case)
    assert SHACL_IT_TOKEN is not None
    for fragment in case.query_contains:
        assert fragment in compiled.query, (
            f"{case.name}: expected query fragment {fragment!r}"
            + _diagnostics(case, shapes_turtle, compiled)
        )

    try:
        response = httpx.post(
            _sparql_endpoint(),
            headers={
                "Authorization": f"Bearer {SHACL_IT_TOKEN}",
                "Accept": "application/sparql-results+json",
            },
            data={"query": compiled.query},
            timeout=120.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        pytest.fail(
            f"{case.name}: /sparql request failed: {exc}"
            + _diagnostics(case, shapes_turtle, compiled)
        )

    assert response.status_code == 200, (
        f"{case.name}: /sparql returned HTTP {response.status_code}: "
        f"{response.text[:2000]}"
        + _diagnostics(case, shapes_turtle, compiled, response.text)
    )
    try:
        payload = response.json()
        bindings = payload["results"]["bindings"]
    except (ValueError, KeyError, TypeError) as exc:
        pytest.fail(
            f"{case.name}: invalid SPARQL JSON response: {exc}; "
            f"body={response.text[:2000]!r}"
            + _diagnostics(case, shapes_turtle, compiled, response.text)
        )

    if case.expect_empty:
        assert bindings == [], (
            f"{case.name}: expected no violations; bindings={bindings!r}"
            + _diagnostics(case, shapes_turtle, compiled, response.text)
        )
        return

    for expected in case.expected:
        matching = [
            binding
            for binding in bindings
            if binding.get("focus_node", {}).get("value") == str(expected.focus)
        ]
        assert matching, (
            f"{case.name}: expected focus node {expected.focus!r}; "
            f"bindings={bindings!r}"
            + _diagnostics(case, shapes_turtle, compiled, response.text)
        )
        if expected.value is not None:
            assert any(
                binding.get("value", {}).get("value") == str(expected.value)
                for binding in matching
            ), (
                f"{case.name}: expected value {expected.value!r}; "
                f"bindings={matching!r}"
                + _diagnostics(case, shapes_turtle, compiled, response.text)
            )
