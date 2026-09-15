"""Compile supported SHACL Core constraints to SPARQL and execute them."""

from __future__ import annotations

import re

import pytest
from rdflib import BNode, Graph, Namespace
from rdflib.namespace import SH

from shacl.shapes import ShapesGraph
from shacl.sparql_validator import SparqlValidator, SparqlViolationQuery
from shacl.types import IllFormedShapeError

EX = Namespace("http://example.org/shacl-test/")

_PREFIXES = """
@prefix ex: <http://example.org/shacl-test/> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def _parse(ttl: str) -> ShapesGraph:
    graph = Graph()
    graph.parse(data=_PREFIXES + ttl, format="turtle")
    return ShapesGraph.from_graph(graph)


def _compile(ttl: str) -> list[SparqlViolationQuery]:
    return SparqlValidator(_parse(ttl)).validate()


def _data(ttl: str) -> Graph:
    graph = Graph()
    graph.parse(data=_PREFIXES + ttl, format="turtle")
    return graph


def _normalized(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def test_compiles_min_count_with_inherited_class_target_and_default_metadata() -> None:
    queries = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property ex:NameShape .

        ex:NameShape a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount 1 .
        """
    )

    assert len(queries) == 1
    compiled = queries[0]
    assert compiled.constraint_iri == SH.MinCountConstraintComponent
    assert compiled.path == str(EX.name)
    assert compiled.message is None
    assert compiled.severity == SH.Violation

    query = _normalized(compiled.query)
    assert "SELECT ?focus_node" in query
    assert "SELECT DISTINCT ?focus_node WHERE" in query
    assert (
        "?focus_node "
        "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://example.org/shacl-test/Person> ."
    ) in query
    assert (
        "OPTIONAL { ?focus_node <http://example.org/shacl-test/name> ?value }"
        in query
    )
    assert "GROUP BY ?focus_node" in query
    assert "HAVING (COUNT(?value) < 1)" in query


def test_compiles_max_count_with_target_node_and_required_path() -> None:
    compiled = _compile(
        """
        ex:NameShape a sh:PropertyShape ;
          sh:targetNode ex:Alice, ex:Carol ;
          sh:path ex:name ;
          sh:maxCount 1 .
        """
    )[0]

    assert compiled.constraint_iri == SH.MaxCountConstraintComponent
    query = _normalized(compiled.query)
    assert (
        "VALUES ?focus_node { <http://example.org/shacl-test/Alice> }" in query
    )
    assert (
        "VALUES ?focus_node { <http://example.org/shacl-test/Carol> }" in query
    )
    assert "UNION" in query
    assert "?focus_node <http://example.org/shacl-test/name> ?value" in query
    assert "OPTIONAL" not in query
    assert "HAVING (COUNT(?value) > 1)" in query


def test_compiles_datatype_with_target_subjects_of() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetSubjectsOf ex:memberOf ;
          sh:path ex:age ;
          sh:datatype xsd:integer .
        """
    )[0]

    assert compiled.constraint_iri == SH.DatatypeConstraintComponent
    query = _normalized(compiled.query)
    assert "?focus_node <http://example.org/shacl-test/memberOf> ?any ." in query
    assert "SELECT DISTINCT ?focus_node ?value" in query
    assert (
        "FILTER (!isLiteral(?value) || datatype(?value) != "
        "<http://www.w3.org/2001/XMLSchema#integer>)"
    ) in query


def test_compiles_pattern_flags_and_target_objects_of_with_result_metadata() -> None:
    compiled = _compile(
        """
        ex:CodeShape a sh:PropertyShape ;
          sh:targetObjectsOf ex:hasMember ;
          sh:path ex:code ;
          sh:pattern "^[a-z]+$" ;
          sh:flags "i" ;
          sh:message "Code must contain letters only" ;
          sh:severity sh:Warning .
        """
    )[0]

    assert compiled.constraint_iri == SH.PatternConstraintComponent
    assert compiled.path == str(EX.code)
    assert compiled.message == "Code must contain letters only"
    assert compiled.severity == SH.Warning
    query = _normalized(compiled.query)
    assert "?any <http://example.org/shacl-test/hasMember> ?focus_node ." in query
    assert 'FILTER (!REGEX(STR(?value), "^[a-z]+$", "i"))' in query


def test_deactivated_property_shape_compiles_no_queries() -> None:
    queries = _compile(
        """
        ex:NameShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:name ;
          sh:deactivated true ;
          sh:minCount 1 .
        """
    )

    assert queries == []


def test_unsupported_path_without_targets_compiles_no_queries() -> None:
    queries = _compile(
        """
        ex:NameShape a sh:PropertyShape ;
          sh:path [ sh:zeroOrMorePath ex:name ] ;
          sh:minCount 1 .
        """
    )

    assert queries == []


def test_property_only_constraint_on_node_shape_is_ill_formed() -> None:
    with pytest.raises(IllFormedShapeError, match="property-shape only"):
        _compile(
            """
            ex:PersonShape a sh:NodeShape ;
              sh:targetClass ex:Person ;
              sh:minCount 1 .
            """
        )


def test_node_shape_leaf_constraint_binds_focus_as_value() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:datatype xsd:string .
        """
    )[0]

    assert compiled.path is None
    assert "BIND (?focus_node AS ?value)" in _normalized(compiled.query)


def test_inverse_path_compiles_for_class_constraint() -> None:
    compiled = _compile(
        """
        ex:ManagerShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path [ sh:inversePath ex:manages ] ;
          sh:class ex:Manager .
        """
    )[0]

    assert compiled.path == "^<http://example.org/shacl-test/manages>"
    assert (
        "?focus_node ^<http://example.org/shacl-test/manages> ?value ."
        in _normalized(compiled.query)
    )


@pytest.mark.parametrize(
    ("path_ttl", "path_type"),
    [
        ("[ sh:zeroOrMorePath ex:knows ]", "ZeroOrMorePath"),
        ("[ sh:oneOrMorePath ex:knows ]", "OneOrMorePath"),
        ("[ sh:zeroOrOnePath ex:knows ]", "ZeroOrOnePath"),
    ],
)
def test_unsupported_repetition_paths_raise(
    path_ttl: str, path_type: str
) -> None:
    with pytest.raises(NotImplementedError, match=path_type):
        _compile(
            f"""
            ex:PathShape a sh:PropertyShape ;
              sh:targetClass ex:Person ;
              sh:path {path_ttl} ;
              sh:class ex:Person .
            """
        )


@pytest.mark.parametrize(
    ("path_ttl", "expected"),
    [
        (
            "[ sh:inversePath ex:a ]",
            "^<http://example.org/shacl-test/a>",
        ),
        (
            "( ex:a ex:b )",
            "<http://example.org/shacl-test/a> / "
            "<http://example.org/shacl-test/b>",
        ),
        (
            "[ sh:alternativePath ( ex:a ex:b ) ]",
            "<http://example.org/shacl-test/a> | "
            "<http://example.org/shacl-test/b>",
        ),
    ],
)
def test_compiles_supported_property_path_snapshots(
    path_ttl: str, expected: str
) -> None:
    compiled = _compile(
        f"""
        ex:PathShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path {path_ttl} ;
          sh:nodeKind sh:IRI .
        """
    )[0]

    assert compiled.path == expected
    assert f"?focus_node {expected} ?value ." in _normalized(compiled.query)


def test_executes_sequence_path() -> None:
    compiled = _compile(
        """
        ex:CityShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ( ex:address ex:city ) ;
          sh:nodeKind sh:IRI .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:address ex:AliceAddress .
        ex:AliceAddress ex:city ex:London .
        ex:Bob a ex:Person ; ex:address ex:BobAddress .
        ex:BobAddress ex:city "Paris" .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.BobAddress, EX.city)))
    }


def test_unsupported_constraint_aborts_compilation() -> None:
    shapes = _parse(
        """
        ex:ManagerShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:manager ;
          sh:minLength 1 .
        """
    )

    with pytest.raises(NotImplementedError, match="No SPARQL validator"):
        SparqlValidator(shapes).validate()


def test_multiple_supported_constraints_compile_one_query_each() -> None:
    queries = _compile(
        """
        ex:ValueShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:value ;
          sh:minCount 1 ;
          sh:maxCount 2 ;
          sh:datatype xsd:string ;
          sh:pattern "^ok" .
        """
    )

    assert len(queries) == 4
    assert {query.constraint_iri for query in queries} == {
        SH.MinCountConstraintComponent,
        SH.MaxCountConstraintComponent,
        SH.DatatypeConstraintComponent,
        SH.PatternConstraintComponent,
    }
    assert all(query.path == str(EX.value) for query in queries)


def test_executes_min_count_with_inherited_target_class_including_blank_nodes() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property [
            sh:path ex:name ;
            sh:minCount 1
          ] .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:name "Alice" .
        ex:Bob a ex:Person .
        [] a ex:Person .
        """
    )

    rows = list(graph.query(compiled.query))

    focus_nodes = {row[0] for row in rows}
    assert EX.Bob in focus_nodes
    assert len(focus_nodes) == 2
    assert any(isinstance(node, BNode) for node in focus_nodes)


def test_executes_node_shape_leaf_constraint_for_blank_focus_node() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:nodeKind sh:IRI .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person .
        [] a ex:Person .
        """
    )

    rows = list(graph.query(compiled.query))

    assert len(rows) == 1
    assert isinstance(rows[0][0], BNode)
    assert rows[0][0] == rows[0][1]


def test_executes_nested_property_shape_with_value_nodes_as_focus() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property [
            sh:path ex:address ;
            sh:property [
              sh:path ex:city ;
              sh:minCount 1
            ]
          ] .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:address ex:AliceAddress .
        ex:AliceAddress ex:city ex:London .
        ex:Bob a ex:Person ; ex:address ex:BobAddress .
        """
    )

    assert compiled.constraint_iri == SH.MinCountConstraintComponent
    assert compiled.path == str(EX.city)
    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.BobAddress,)
    }


def test_executes_doubly_nested_property_shape_focus() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property [
            sh:path ex:address ;
            sh:property [
              sh:path ex:city ;
              sh:property [
                sh:path ex:label ;
                sh:minCount 1
              ]
            ]
          ] .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:address ex:AliceAddress .
        ex:AliceAddress ex:city ex:London .
        ex:London ex:label "London" .
        ex:Bob a ex:Person ; ex:address ex:BobAddress .
        ex:BobAddress ex:city ex:Paris .
        """
    )

    assert compiled.constraint_iri == SH.MinCountConstraintComponent
    assert compiled.path == str(EX.label)
    assert {tuple(row) for row in graph.query(compiled.query)} == {(EX.Paris,)}


def test_executes_max_count_for_target_node_values() -> None:
    compiled = _compile(
        """
        ex:NameShape a sh:PropertyShape ;
          sh:targetNode ex:Alice, ex:Carol ;
          sh:path ex:name ;
          sh:maxCount 1 .
        """
    )[0]
    graph = _data(
        """
        ex:Alice ex:name "Alice" .
        ex:Carol ex:name "Carol", "C. Jones" .
        ex:Other ex:name "One", "Two" .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {(EX.Carol,)}


def test_executes_datatype_and_returns_focus_node_and_value() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:datatype xsd:integer .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:age 42 .
        ex:Bob a ex:Person ; ex:age "old" .
        ex:Carol a ex:Person ; ex:age ex:UnknownAge .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.age))),
        (EX.Carol, EX.UnknownAge),
    }


def test_executes_pattern_and_returns_only_non_matching_value() -> None:
    compiled = _compile(
        """
        ex:CodeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:code ;
          sh:pattern "^[A-Z]" .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:code "ABC" .
        ex:Bob a ex:Person ; ex:code "lowercase" .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.code)))
    }


def test_compiles_class_with_not_exists_and_literal_filter() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property ex:AddressShape .

        ex:AddressShape a sh:PropertyShape ;
          sh:path ex:address ;
          sh:class ex:PostalAddress .
        """
    )[0]

    assert compiled.constraint_iri == SH.ClassConstraintComponent
    query = _normalized(compiled.query)
    assert "SELECT DISTINCT ?focus_node ?value" in query
    assert "NOT EXISTS" in query
    assert "isLiteral(?value)" in query
    assert (
        "?value <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>/"
        "<http://www.w3.org/2000/01/rdf-schema#subClassOf>* "
        "<http://example.org/shacl-test/PostalAddress> ."
    ) in query
    assert (
        "?focus_node <http://example.org/shacl-test/address> ?value ."
        in query
    )


def test_compiles_repeatable_class_as_one_query_each() -> None:
    queries = _compile(
        """
        ex:AddressShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:address ;
          sh:class ex:PostalAddress, ex:Location .
        """
    )

    assert len(queries) == 2
    assert all(
        query.constraint_iri == SH.ClassConstraintComponent for query in queries
    )
    texts = {_normalized(query.query) for query in queries}
    assert any(
        "<http://example.org/shacl-test/PostalAddress>" in text for text in texts
    )
    assert any(
        "<http://example.org/shacl-test/Location>" in text for text in texts
    )


def test_executes_class_and_returns_untyped_and_literal_values() -> None:
    compiled = _compile(
        """
        ex:PersonShape a sh:NodeShape ;
          sh:targetClass ex:Person ;
          sh:property [
            sh:path ex:address ;
            sh:class ex:PostalAddress
          ] .
        """
    )[0]
    graph = _data(
        """
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        ex:HomeAddress rdfs:subClassOf ex:PostalAddress .
        ex:Alice a ex:Person ; ex:address ex:AliceHome .
        ex:AliceHome a ex:PostalAddress .
        ex:Dana a ex:Person ; ex:address ex:DanaHome .
        ex:DanaHome a ex:HomeAddress .
        ex:Bob a ex:Person ; ex:address ex:BobHome .
        ex:Carol a ex:Person ; ex:address "not an address" .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, EX.BobHome),
        (EX.Carol, next(graph.objects(EX.Carol, EX.address))),
    }


def test_compiles_node_kind_filters_for_iri_literal_and_combination() -> None:
    iri = _compile(
        """
        ex:IriShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:address ;
          sh:nodeKind sh:IRI .
        """
    )[0]
    literal = _compile(
        """
        ex:LiteralShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:name ;
          sh:nodeKind sh:Literal .
        """
    )[0]
    combination = _compile(
        """
        ex:ComboShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:value ;
          sh:nodeKind sh:IRIOrLiteral .
        """
    )[0]

    assert iri.constraint_iri == SH.NodeKindConstraintComponent
    assert "FILTER (!isIRI(?value))" in _normalized(iri.query)
    assert "FILTER (!isLiteral(?value))" in _normalized(literal.query)
    assert "FILTER (isBlank(?value))" in _normalized(combination.query)


def test_unknown_node_kind_raises() -> None:
    shapes = _parse(
        """
        ex:KindShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:value ;
          sh:nodeKind ex:NotAKind .
        """
    )

    with pytest.raises(IllFormedShapeError, match="Unknown sh:nodeKind"):
        SparqlValidator(shapes).validate()


def test_executes_node_kind_iri_and_returns_literal_value() -> None:
    compiled = _compile(
        """
        ex:AddressShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:address ;
          sh:nodeKind sh:IRI .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:address ex:AliceHome .
        ex:Bob a ex:Person ; ex:address "literal address" .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.address))),
    }


def test_executes_node_kind_literal_and_returns_iri_value() -> None:
    compiled = _compile(
        """
        ex:NameShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:name ;
          sh:nodeKind sh:Literal .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:name "Alice" .
        ex:Bob a ex:Person ; ex:name ex:BobName .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, EX.BobName),
    }


def test_executes_node_kind_iri_or_literal_accepts_iri_and_literal() -> None:
    compiled = _compile(
        """
        ex:ValueShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:value ;
          sh:nodeKind sh:IRIOrLiteral .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:value ex:Thing .
        ex:Bob a ex:Person ; ex:value "text" .
        """
    )

    assert list(graph.query(compiled.query)) == []


@pytest.mark.parametrize(
    ("constraint_ttl", "constraint_iri", "bound_n3", "operator"),
    [
        ("sh:minExclusive 0", SH.MinExclusiveConstraintComponent, '"0"^^<http://www.w3.org/2001/XMLSchema#integer>', "<"),
        ("sh:minInclusive 0", SH.MinInclusiveConstraintComponent, '"0"^^<http://www.w3.org/2001/XMLSchema#integer>', "<="),
        ("sh:maxExclusive 100", SH.MaxExclusiveConstraintComponent, '"100"^^<http://www.w3.org/2001/XMLSchema#integer>', ">"),
        ("sh:maxInclusive 100", SH.MaxInclusiveConstraintComponent, '"100"^^<http://www.w3.org/2001/XMLSchema#integer>', ">="),
    ],
)
def test_compiles_value_range_filters(
    constraint_ttl: str,
    constraint_iri,
    bound_n3: str,
    operator: str,
) -> None:
    compiled = _compile(
        f"""
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          {constraint_ttl} .
        """
    )[0]

    assert compiled.constraint_iri == constraint_iri
    query = _normalized(compiled.query)
    assert "SELECT DISTINCT ?focus_node ?value" in query
    assert "?focus_node <http://example.org/shacl-test/age> ?value ." in query
    assert bound_n3 in query
    assert f"{bound_n3} {operator} ?value" in query
    assert "FILTER (!COALESCE(" in query
    assert f"isNumeric({bound_n3}) && isNumeric(?value)" in query
    assert f"datatype({bound_n3}) = datatype(?value)" in query


def test_executes_min_exclusive_integer_bounds() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:minExclusive 0 .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:age 1 .
        ex:Bob a ex:Person ; ex:age 0 .
        ex:Carol a ex:Person ; ex:age -1 .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.age))),
        (EX.Carol, next(graph.objects(EX.Carol, EX.age))),
    }


def test_executes_min_inclusive_integer_bounds() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:minInclusive 0 .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:age 1 .
        ex:Bob a ex:Person ; ex:age 0 .
        ex:Carol a ex:Person ; ex:age -1 .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Carol, next(graph.objects(EX.Carol, EX.age))),
    }


def test_executes_max_exclusive_integer_bounds() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:maxExclusive 100 .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:age 99 .
        ex:Bob a ex:Person ; ex:age 100 .
        ex:Carol a ex:Person ; ex:age 101 .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.age))),
        (EX.Carol, next(graph.objects(EX.Carol, EX.age))),
    }


def test_executes_max_inclusive_integer_bounds() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:maxInclusive 100 .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:age 100 .
        ex:Bob a ex:Person ; ex:age 101 .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.age))),
    }


def test_executes_min_inclusive_incomparable_values_are_violations() -> None:
    compiled = _compile(
        """
        ex:AgeShape a sh:PropertyShape ;
          sh:targetClass ex:Person ;
          sh:path ex:age ;
          sh:minInclusive 0 .
        """
    )[0]
    graph = _data(
        """
        ex:Alice a ex:Person ; ex:age 21 .
        ex:Bob a ex:Person ; ex:age "twenty one" .
        ex:Carol a ex:Person ; ex:age ex:UnknownAge .
        """
    )

    assert {tuple(row) for row in graph.query(compiled.query)} == {
        (EX.Bob, next(graph.objects(EX.Bob, EX.age))),
        (EX.Carol, EX.UnknownAge),
    }
