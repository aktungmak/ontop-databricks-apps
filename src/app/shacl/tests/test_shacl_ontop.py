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
NATION_2 = EX["nation/2"]
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
    """A live-Ontop case, compiled one of two ways.

    Most cases keep the original single-shape template: set ``shape_kind``,
    ``target``, and ``constraint`` (optionally ``path``) and the shape is
    compiled via ``SparqlValidator.validate()``, asserting exactly one query
    (unchanged behavior).

    Cases that need nested ``sh:property`` or the ``compile_shape`` (MCP)
    path instead supply ``shapes_turtle`` — a full shapes Turtle snippet
    (prefixes from ``_PREFIXES`` are added automatically) — and
    ``compile_shape_iri``, the IRI of the named node shape to compile. If
    that compiles to more than one query, set ``select_query_contains`` to a
    fragment unique to the query that should be executed.
    """

    name: str
    shape_kind: str | None = None
    target: str | None = None
    constraint: str | None = None
    path: str | None = None
    expected: tuple[ExpectedBinding, ...] = ()
    expect_empty: bool = False
    query_contains: tuple[str, ...] = ()
    shapes_turtle: str | None = None
    compile_shape_iri: URIRef | None = None
    select_query_contains: str | None = None


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
    pytest.param(
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
        marks=pytest.mark.xfail(
            reason=(
                "Ontop full-native reformulation crashes (NativeNode cannot "
                "accept a visitor) when COUNT(DISTINCT ?value) is applied to "
                "a TBox-expanded superclass such as ex:GeographicArea. The "
                "compiler still emits the query (direct rdf:type "
                "GeographicArea; subclass closure is Ontop TBox rewriting) "
                "and the compile-time assertions above still run; only the "
                "live HTTP/execution assertion is expected to fail. See "
                "UNSUPPORTED_SHACL_INVENTORY. Remove once the upstream Ontop "
                "fix is released."
            ),
            strict=True,
        ),
        id="target-class-subclass-closure",
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
    ValidatorCase(
        "min-length",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minLength 7",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
        query_contains=("STRLEN(STR(?value)) < 7",),
    ),
    ValidatorCase(
        "max-length",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxLength 5",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
        query_contains=("STRLEN(STR(?value)) > 5",),
    ),
    ValidatorCase(
        "min-length-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minLength 6",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "language-in",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:languageIn ("en")',
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
        query_contains=('langMatches(lang(?value), "en")',),
    ),
    # No uniqueLang *violation* case: TPC-H names/comments are plain literals
    # without language tags (lang(?value) == ""), so the duplicate-language
    # self-join this validator emits can never match and uniqueLang cannot
    # fire against this dataset. The conforms case below is the only live
    # coverage; a violation would require literals with repeated language
    # tags, which this mapping does not produce.
    ValidatorCase(
        "unique-lang-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:uniqueLang true",
        path="ex:name",
        expect_empty=True,
        query_contains=('lang(?value) != ""',),
    ),
    ValidatorCase(
        "has-value",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:hasValue "EUROPE"',
        path="ex:name",
        expected=(ExpectedBinding(REGION_0),),
        query_contains=('FILTER NOT EXISTS { ?focus_node',),
    ),
    ValidatorCase(
        "has-value-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:hasValue "AFRICA"',
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "in",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:in ("EUROPE" "ASIA")',
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
        query_contains=('MINUS { VALUES ?value { "EUROPE" "ASIA" } }',),
    ),
    ValidatorCase(
        "in-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:in ("AFRICA" "EUROPE")',
        path="ex:name",
        expect_empty=True,
        query_contains=('MINUS { VALUES ?value { "AFRICA" "EUROPE" } }',),
    ),
    ValidatorCase(
        "equals",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:equals ex:comment",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
        query_contains=("UNION",),
    ),
    ValidatorCase(
        "equals-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:equals ex:name",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "disjoint",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:disjoint ex:name",
        path="ex:name",
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
    ),
    ValidatorCase(
        "disjoint-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:disjoint ex:comment",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "less-than",
        "PropertyShape",
        f"sh:targetNode {NATION_0.n3()}",
        "sh:lessThan ex:nationKey",
        path="ex:regionKey",
        expected=(ExpectedBinding(NATION_0, "0"),),
        query_contains=("?value < ?other",),
    ),
    ValidatorCase(
        "less-than-conforms",
        "PropertyShape",
        f"sh:targetNode {NATION_2.n3()}",
        "sh:lessThan ex:nationKey",
        path="ex:regionKey",
        expect_empty=True,
    ),
    ValidatorCase(
        "less-than-or-equals-conforms",
        "PropertyShape",
        f"sh:targetNode {NATION_0.n3()}",
        "sh:lessThanOrEquals ex:nationKey",
        path="ex:regionKey",
        expect_empty=True,
        query_contains=("?value <= ?other",),
    ),
    ValidatorCase(
        "less-than-or-equals",
        "PropertyShape",
        f"sh:targetNode {NATION_2.n3()}",
        "sh:lessThanOrEquals ex:regionKey",
        path="ex:nationKey",
        expected=(ExpectedBinding(NATION_2, "2"),),
    ),
    # -- Property-shape conform counterparts -------------------------------
    # Several property-shape validators above only exercise the violate
    # path; these add the cheap conforming counterpart against the same
    # live data.
    ValidatorCase(
        "datatype-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:datatype xsd:string",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "pattern-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        'sh:pattern "^AFRICA$"',
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "min-exclusive-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:minExclusive -1",
        path="ex:regionKey",
        expect_empty=True,
    ),
    ValidatorCase(
        "max-inclusive-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxInclusive 0",
        path="ex:regionKey",
        expect_empty=True,
    ),
    ValidatorCase(
        "max-length-conforms",
        "PropertyShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:maxLength 6",
        path="ex:name",
        expect_empty=True,
    ),
    ValidatorCase(
        "class-inregion-conforms",
        "PropertyShape",
        f"sh:targetNode {NATION_0.n3()}",
        "sh:class ex:Region",
        path="ex:inRegion",
        expect_empty=True,
    ),
    # -- Node-shape leaf constraints (BIND focus as value) ------------------
    # `node-shape-focus-value` (sh:class ex:Nation) above is the only
    # existing node-shape leaf case; these round out BIND (?focus_node AS
    # ?value) coverage for other validators against REGION_0/NATION_0.
    ValidatorCase(
        "node-shape-nodekind-iri-conforms",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:nodeKind sh:IRI",
        expect_empty=True,
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-nodekind-literal-violates",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:nodeKind sh:Literal",
        expected=(ExpectedBinding(REGION_0, REGION_0),),
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-has-value-conforms",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        f"sh:hasValue {REGION_0.n3()}",
        expect_empty=True,
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-has-value-violates",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        f"sh:hasValue {NATION_0.n3()}",
        # Node-shape sh:hasValue compares the focus node itself, so the
        # violation's value binding is the focus (matching the existing
        # property-shape has-value binding behavior: see `has-value` above
        # and the unit tests), not the (absent) expected hasValue term.
        expected=(ExpectedBinding(REGION_0, REGION_0),),
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-in-violates",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        f"sh:in ({NATION_0.n3()})",
        expected=(ExpectedBinding(REGION_0, REGION_0),),
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-in-conforms",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        f"sh:in ({REGION_0.n3()} {NATION_0.n3()})",
        expect_empty=True,
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-class-region-conforms",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:class ex:Region",
        expect_empty=True,
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-datatype-violates",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        "sh:datatype xsd:string",
        expected=(ExpectedBinding(REGION_0, REGION_0),),
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    ValidatorCase(
        "node-shape-pattern-iri-conforms",
        "NodeShape",
        f"sh:targetNode {REGION_0.n3()}",
        # STR() of the focus IRI contains "region/0"; stable because it is
        # the mapping's rr:template, not a value sensitive to row content.
        'sh:pattern "region/0"',
        expect_empty=True,
        query_contains=("BIND (?focus_node AS ?value)",),
    ),
    # -- Nested sh:property (compile_shape; the MCP compile path) -----------
    # A nested sh:property must rebind the inner shape's focus to the
    # *parent's value nodes* (?parent_focus_N <path> ?focus_node), not
    # inherit the outer NodeShape's sh:targetNode onto the inner path. If
    # that rebinding were wrong, ex:name would be read directly off
    # NATION_0 ("ALGERIA") instead of off its ex:inRegion value REGION_0
    # ("AFRICA").
    ValidatorCase(
        "nested-property-violates",
        shapes_turtle=f"""
ex:NestedRegionNameViolatesShape a sh:NodeShape ;
  sh:targetNode {NATION_0.n3()} ;
  sh:property [
    sh:path ex:inRegion ;
    sh:property [
      sh:path ex:name ;
      sh:pattern "^EUROPE$"
    ]
  ] .
""",
        compile_shape_iri=EX["NestedRegionNameViolatesShape"],
        expected=(ExpectedBinding(REGION_0, "AFRICA"),),
        query_contains=(
            "?parent_focus_1 <http://example.org/tpch/inRegion> ?focus_node",
        ),
    ),
    ValidatorCase(
        "nested-property-conforms",
        shapes_turtle=f"""
ex:NestedRegionNameConformsShape a sh:NodeShape ;
  sh:targetNode {NATION_0.n3()} ;
  sh:property [
    sh:path ex:inRegion ;
    sh:property [
      sh:path ex:name ;
      sh:datatype xsd:string
    ]
  ] .
""",
        compile_shape_iri=EX["NestedRegionNameConformsShape"],
        expect_empty=True,
        query_contains=(
            "?parent_focus_1 <http://example.org/tpch/inRegion> ?focus_node",
        ),
    ),
]


def _shapes_turtle(case: ValidatorCase) -> str:
    if case.shapes_turtle is not None:
        return _PREFIXES + case.shapes_turtle
    assert (
        case.shape_kind is not None
        and case.target is not None
        and case.constraint is not None
    ), (
        f"{case.name}: must supply either shapes_turtle or "
        "shape_kind/target/constraint"
    )
    path = f"\n  sh:path {case.path} ;" if case.path is not None else ""
    return _PREFIXES + f"""
ex:IntegrationShape a sh:{case.shape_kind} ;
  {case.target} ;{path}
  {case.constraint} .
"""


def _select_query(
    case: ValidatorCase,
    queries: list,
    shapes_turtle: str,
):
    if case.select_query_contains is not None:
        matches = [q for q in queries if case.select_query_contains in q.query]
        assert len(matches) == 1, (
            f"{case.name}: expected exactly one compiled query containing "
            f"{case.select_query_contains!r}, got {len(matches)} of "
            f"{len(queries)}\nshape:\n{shapes_turtle}"
        )
        return matches[0]
    assert len(queries) == 1, (
        f"{case.name}: expected one query, got {len(queries)}\n"
        f"shape:\n{shapes_turtle}"
    )
    return queries[0]


def _compile(case: ValidatorCase):
    shapes_turtle = _shapes_turtle(case)
    graph = Graph()
    graph.parse(data=shapes_turtle, format="turtle")
    validator = SparqlValidator(ShapesGraph.from_graph(graph))
    if case.compile_shape_iri is not None:
        # The MCP (`validate_shacl`) compile path: a named node shape plus
        # its (possibly nested) sh:property children.
        queries = validator.compile_shape(case.compile_shape_iri).queries
    else:
        queries = validator.validate()
    compiled = _select_query(case, queries, shapes_turtle)
    return shapes_turtle, compiled


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
