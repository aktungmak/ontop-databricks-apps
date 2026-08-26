"""Parse SHACL Turtle into the shapes AST."""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH, XSD

from shacl.constraint_components import (
    AndConstraintComponent,
    ClassConstraintComponent,
    ClosedConstraintComponent,
    DatatypeConstraintComponent,
    HasValueConstraintComponent,
    IllFormedShapeError,
    InConstraintComponent,
    LanguageInConstraintComponent,
    MaxCountConstraintComponent,
    MaxExclusiveConstraintComponent,
    MinCountConstraintComponent,
    MinInclusiveConstraintComponent,
    MinLengthConstraintComponent,
    NodeConstraintComponent,
    NodeKindConstraintComponent,
    PatternConstraintComponent,
    QualifiedMaxCountConstraintComponent,
    QualifiedMinCountConstraintComponent,
    UniqueLangConstraintComponent,
)
from shacl.shapes import (
    AlternativePath,
    InversePath,
    NodeShape,
    OneOrMorePath,
    PredicatePath,
    PropertyShape,
    SequencePath,
    Severity,
    ShapesGraph,
    TargetClass,
    TargetNode,
    TargetObjectsOf,
    TargetSubjectsOf,
    ZeroOrMorePath,
    ZeroOrOnePath,
)

EX = Namespace("http://example.org/tpch/")
REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SHAPES = REPO_ROOT / "mappings" / "shapes.ttl"


def _parse(ttl: str) -> ShapesGraph:
    graph = Graph()
    graph.parse(data=ttl, format="turtle")
    return ShapesGraph.from_graph(graph)


def _property_by_path(sg: ShapesGraph, node_shape: NodeShape, path: URIRef) -> PropertyShape:
    matches = [
        sg.shapes[ref]
        for ref in node_shape.property
        if isinstance(sg.shapes[ref], PropertyShape)
        and sg.shapes[ref].path == PredicatePath(path)
    ]
    assert len(matches) == 1, f"expected one property shape for {path}"
    shape = matches[0]
    assert isinstance(shape, PropertyShape)
    return shape


def _constraint(shape, component_type):
    matches = [c for c in shape.constraints if isinstance(c, component_type)]
    assert len(matches) == 1, f"expected one {component_type.__name__}"
    return matches[0]


def test_parses_node_shape_with_target_class_and_blank_property_shapes() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        ex:RegionShape a sh:NodeShape ;
          sh:targetClass ex:Region ;
          sh:property [
            sh:path ex:regionKey ;
            sh:datatype xsd:integer ;
            sh:minCount 1 ;
            sh:maxCount 1
          ] ;
          sh:property [
            sh:path ex:name ;
            sh:nodeKind sh:Literal ;
            sh:minCount 1
          ] .
        """
    )

    region = sg.shapes[EX.RegionShape]
    assert isinstance(region, NodeShape)
    assert region.deactivated is False
    assert region.severity is None
    assert [t.targetClass for t in region.targets if isinstance(t, TargetClass)] == [
        EX.Region
    ]
    assert len(region.property) == 2

    key = _property_by_path(sg, region, EX.regionKey)
    assert key.path == PredicatePath(EX.regionKey)
    assert _constraint(key, DatatypeConstraintComponent).datatype == XSD.integer
    assert _constraint(key, MinCountConstraintComponent).minCount == 1
    assert _constraint(key, MaxCountConstraintComponent).maxCount == 1

    name = _property_by_path(sg, region, EX.name)
    assert _constraint(name, NodeKindConstraintComponent).nodeKind == SH.Literal
    assert _constraint(name, MinCountConstraintComponent).minCount == 1


def test_discovers_shapes_from_all_target_predicates() -> None:
    node = URIRef("http://example.org/tpch/known-customer")
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:ByClass sh:targetClass ex:Customer .
        ex:ByNode sh:targetNode <http://example.org/tpch/known-customer> .
        ex:BySubjects sh:targetSubjectsOf ex:name .
        ex:ByObjects sh:targetObjectsOf ex:inRegion .
        """
    )

    assert isinstance(sg.shapes[EX.ByClass], NodeShape)
    assert sg.shapes[EX.ByClass].targets == [TargetClass(targetClass=EX.Customer)]

    assert isinstance(sg.shapes[EX.ByNode], NodeShape)
    assert sg.shapes[EX.ByNode].targets == [TargetNode(targetNode=node)]

    assert isinstance(sg.shapes[EX.BySubjects], NodeShape)
    assert sg.shapes[EX.BySubjects].targets == [
        TargetSubjectsOf(targetSubjectsOf=EX.name)
    ]

    assert isinstance(sg.shapes[EX.ByObjects], NodeShape)
    assert sg.shapes[EX.ByObjects].targets == [
        TargetObjectsOf(targetObjectsOf=EX.inRegion)
    ]


def test_typed_property_shape_is_not_overwritten_by_target_discovery() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:NamedProp a sh:PropertyShape ;
          sh:path ex:name ;
          sh:targetClass ex:Customer ;
          sh:minCount 1 .
        """
    )

    shape = sg.shapes[EX.NamedProp]
    assert isinstance(shape, PropertyShape)
    assert shape.path == PredicatePath(EX.name)
    assert shape.targets == [TargetClass(targetClass=EX.Customer)]
    assert _constraint(shape, MinCountConstraintComponent).minCount == 1


def test_parses_property_shape_metadata_and_deactivation() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:CustomerShape a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:property ex:NameProp .

        ex:NameProp a sh:PropertyShape ;
          sh:path ex:name ;
          sh:name "customer name" ;
          sh:description "Legal name of the customer" ;
          sh:message "Customer must have a name" ;
          sh:severity sh:Warning ;
          sh:order 2 ;
          sh:deactivated true ;
          sh:minCount 1 .
        """
    )

    prop = sg.shapes[EX.NameProp]
    assert isinstance(prop, PropertyShape)
    assert prop.deactivated is True
    assert prop.name == ["customer name"]
    assert prop.description == ["Legal name of the customer"]
    assert prop.message == ["Customer must have a name"]
    assert prop.severity == Severity.WARNING
    assert prop.order == 2


def test_parses_custom_severity_iri() -> None:
    custom = URIRef("http://example.org/tpch/Critical")
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:S a sh:NodeShape ;
          sh:severity ex:Critical ;
          sh:targetClass ex:Customer .
        """
    )
    assert sg.shapes[EX.S].severity == custom


def test_effective_targets_inherit_from_parent_node_shape() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:CustomerShape a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:property ex:NameProp .

        ex:NameProp a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount 1 .
        """
    )

    parents = sg.parent_node_shapes(EX.NameProp)
    assert len(parents) == 1
    assert parents[0] is sg.shapes[EX.CustomerShape]

    inherited = sg.effective_targets(EX.NameProp)
    assert inherited == [TargetClass(targetClass=EX.Customer)]


def test_effective_targets_prefer_the_property_shape_own_targets() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:CustomerShape a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:property ex:NameProp .

        ex:NameProp a sh:PropertyShape ;
          sh:path ex:name ;
          sh:targetClass ex:Supplier ;
          sh:minCount 1 .
        """
    )

    own = sg.effective_targets(EX.NameProp)
    assert own == [TargetClass(targetClass=EX.Supplier)]


def test_parses_value_and_string_constraint_components() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:comment ;
          sh:class ex:Region ;
          sh:hasValue "AFRICA" ;
          sh:pattern "^[A-Z]+$" ;
          sh:flags "i" ;
          sh:minLength 3 ;
          sh:minInclusive 0 ;
          sh:maxExclusive 100 ;
          sh:uniqueLang true ;
          sh:closed true .
        """
    )

    shape = sg.shapes[EX.P]
    assert _constraint(shape, ClassConstraintComponent).class_ == EX.Region
    assert _constraint(shape, HasValueConstraintComponent).hasValue == Literal("AFRICA")
    pattern = _constraint(shape, PatternConstraintComponent)
    assert pattern.pattern == "^[A-Z]+$"
    assert pattern.flags == "i"
    assert _constraint(shape, MinLengthConstraintComponent).minLength == 3
    assert _constraint(shape, MinInclusiveConstraintComponent).minInclusive == Literal(
        0
    )
    assert _constraint(shape, MaxExclusiveConstraintComponent).maxExclusive == Literal(
        100
    )
    assert _constraint(shape, UniqueLangConstraintComponent).uniqueLang is True
    assert _constraint(shape, ClosedConstraintComponent).closed is True


def test_sh_in_expands_rdf_list_members() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:in ("AFRICA" "AMERICA" "ASIA") .
        """
    )
    in_constraint = _constraint(sg.shapes[EX.P], InConstraintComponent)
    assert in_constraint.in_ == [
        Literal("AFRICA"),
        Literal("AMERICA"),
        Literal("ASIA"),
    ]


def test_empty_sh_in_is_well_formed() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:in () .
        """
    )
    in_constraint = _constraint(sg.shapes[EX.P], InConstraintComponent)
    assert in_constraint.in_ == []


def test_duplicate_sh_and_members_are_kept() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:S a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:and ( ex:A ex:A ) .
        """
    )
    and_constraint = _constraint(sg.shapes[EX.S], AndConstraintComponent)
    assert and_constraint.shapes == [EX.A, EX.A]


def test_malformed_rdf_list_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:in ex:NotAList .
        """
    with pytest.raises(IllFormedShapeError, match="non-list"):
        _parse(ttl)


def test_cyclic_rdf_list_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:in _:head .
        _:head rdf:first "AFRICA" ;
          rdf:rest _:head .
        """
    with pytest.raises(IllFormedShapeError, match="cycle"):
        _parse(ttl)


def test_ignored_properties_list_is_expanded() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:closed true ;
          sh:ignoredProperties ( rdf:type ex:comment ) .
        """
    )
    closed = _constraint(sg.shapes[EX.P], ClosedConstraintComponent)
    assert closed.closed is True
    assert closed.ignoredProperties == [RDF.type, EX.comment]


def test_ignored_properties_non_iri_member_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:closed true ;
          sh:ignoredProperties ( "type" ) .
        """
    with pytest.raises(IllFormedShapeError, match="IRIs"):
        _parse(ttl)


def test_language_in_expands_string_literals() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:languageIn ( "en" "fr" ) .
        """
    )
    language_in = _constraint(sg.shapes[EX.P], LanguageInConstraintComponent)
    assert language_in.languageIn == ["en", "fr"]


def test_predicate_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name .
        """
    )
    assert sg.shapes[EX.P].path == PredicatePath(EX.name)


def test_inverse_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:inversePath ex:inRegion ] .
        """
    )
    assert sg.shapes[EX.P].path == InversePath(path=PredicatePath(EX.inRegion))


def test_sequence_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ( ex:inRegion ex:name ) .
        """
    )
    assert sg.shapes[EX.P].path == SequencePath(
        paths=[PredicatePath(EX.inRegion), PredicatePath(EX.name)]
    )


def test_alternative_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:alternativePath ( ex:name ex:comment ) ] .
        """
    )
    assert sg.shapes[EX.P].path == AlternativePath(
        paths=[PredicatePath(EX.name), PredicatePath(EX.comment)]
    )


def test_zero_or_one_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:zeroOrOnePath ex:name ] .
        """
    )
    assert sg.shapes[EX.P].path == ZeroOrOnePath(path=PredicatePath(EX.name))


def test_zero_or_more_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:zeroOrMorePath ex:inRegion ] .
        """
    )
    assert sg.shapes[EX.P].path == ZeroOrMorePath(path=PredicatePath(EX.inRegion))


def test_one_or_more_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:oneOrMorePath ex:inRegion ] .
        """
    )
    assert sg.shapes[EX.P].path == OneOrMorePath(path=PredicatePath(EX.inRegion))


def test_nested_inverse_of_sequence_path() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:inversePath ( ex:inRegion ex:name ) ] .
        """
    )
    assert sg.shapes[EX.P].path == InversePath(
        path=SequencePath(
            paths=[PredicatePath(EX.inRegion), PredicatePath(EX.name)]
        )
    )


def test_empty_sequence_path_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path () .
        """
    with pytest.raises(IllFormedShapeError, match="Empty sequence"):
        _parse(ttl)


def test_empty_alternative_path_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:alternativePath () ] .
        """
    with pytest.raises(IllFormedShapeError, match="Empty alternative"):
        _parse(ttl)


def test_multiple_sh_path_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:path ex:comment .
        """
    with pytest.raises(IllFormedShapeError, match="exactly one sh:path"):
        _parse(ttl)


def test_mixed_path_constructors_raise() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path [ sh:inversePath ex:name ; sh:zeroOrMorePath ex:comment ] .
        """
    with pytest.raises(IllFormedShapeError, match="mixes path constructors"):
        _parse(ttl)


def test_missing_sh_path_on_property_shape_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:minCount 1 .
        """
    with pytest.raises(IllFormedShapeError, match="exactly one sh:path"):
        _parse(ttl)


def test_from_file_parses_sample_tpch_shapes() -> None:
    sg = ShapesGraph.from_file(str(SAMPLE_SHAPES))

    node_shapes = {
        ref: shape
        for ref, shape in sg.shapes.items()
        if isinstance(shape, NodeShape)
    }
    expected_nodes = {
        EX.RegionShape,
        EX.NationShape,
        EX.CustomerShape,
        EX.SupplierShape,
        EX.PartShape,
        EX.PartSuppShape,
        EX.OrderShape,
        EX.LineItemShape,
    }
    assert set(node_shapes) == expected_nodes

    property_shapes = [
        shape for shape in sg.shapes.values() if isinstance(shape, PropertyShape)
    ]
    assert len(property_shapes) > 0
    assert all(isinstance(shape.path, PredicatePath) for shape in property_shapes)

    region = node_shapes[EX.RegionShape]
    assert region.targets == [TargetClass(targetClass=EX.Region)]
    key = _property_by_path(sg, region, EX.regionKey)
    assert _constraint(key, DatatypeConstraintComponent).datatype == XSD.integer
    assert sg.effective_targets(region.property[0]) == [
        TargetClass(targetClass=EX.Region)
    ]

    nation = node_shapes[EX.NationShape]
    in_region = _property_by_path(sg, nation, EX.inRegion)
    assert _constraint(in_region, ClassConstraintComponent).class_ == EX.Region
    assert _constraint(in_region, NodeKindConstraintComponent).nodeKind == SH.IRI


def test_parses_qualified_min_count_only() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:inRegion ;
          sh:qualifiedValueShape ex:RegionShape ;
          sh:qualifiedMinCount 1 .
        """
    )

    shape = sg.shapes[EX.P]
    min_c = _constraint(shape, QualifiedMinCountConstraintComponent)
    assert min_c.qualifiedValueShape == EX.RegionShape
    assert min_c.qualifiedMinCount == 1
    assert min_c.qualifiedValueShapesDisjoint is None
    assert not any(
        isinstance(c, QualifiedMaxCountConstraintComponent) for c in shape.constraints
    )


def test_parses_qualified_max_count_only() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:inRegion ;
          sh:qualifiedValueShape ex:RegionShape ;
          sh:qualifiedMaxCount 2 .
        """
    )

    shape = sg.shapes[EX.P]
    max_c = _constraint(shape, QualifiedMaxCountConstraintComponent)
    assert max_c.qualifiedValueShape == EX.RegionShape
    assert max_c.qualifiedMaxCount == 2
    assert max_c.qualifiedValueShapesDisjoint is None
    assert not any(
        isinstance(c, QualifiedMinCountConstraintComponent) for c in shape.constraints
    )


def test_parses_qualified_min_and_max_count_with_disjoint() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:inRegion ;
          sh:qualifiedValueShape ex:RegionShape ;
          sh:qualifiedMinCount 1 ;
          sh:qualifiedMaxCount 2 ;
          sh:qualifiedValueShapesDisjoint true .
        """
    )

    shape = sg.shapes[EX.P]
    min_c = _constraint(shape, QualifiedMinCountConstraintComponent)
    max_c = _constraint(shape, QualifiedMaxCountConstraintComponent)
    assert min_c.qualifiedValueShape == EX.RegionShape
    assert max_c.qualifiedValueShape == EX.RegionShape
    assert min_c.qualifiedMinCount == 1
    assert max_c.qualifiedMaxCount == 2
    assert min_c.qualifiedValueShapesDisjoint is True
    assert max_c.qualifiedValueShapesDisjoint is True


def test_qualified_value_shape_without_counts_is_ignored() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:inRegion ;
          sh:qualifiedValueShape ex:RegionShape .
        """
    )

    shape = sg.shapes[EX.P]
    assert not any(
        isinstance(
            c,
            (
                QualifiedMinCountConstraintComponent,
                QualifiedMaxCountConstraintComponent,
            ),
        )
        for c in shape.constraints
    )


def test_repeatable_class_parameters_each_become_a_constraint() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:inRegion ;
          sh:class ex:Region ;
          sh:class ex:Nation .
        """
    )
    classes = [
        c.class_
        for c in sg.shapes[EX.P].constraints
        if isinstance(c, ClassConstraintComponent)
    ]
    assert set(classes) == {EX.Region, EX.Nation}


def test_repeatable_has_value_parameters_each_become_a_constraint() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:hasValue "AFRICA" ;
          sh:hasValue "ASIA" .
        """
    )
    values = [
        c.hasValue
        for c in sg.shapes[EX.P].constraints
        if isinstance(c, HasValueConstraintComponent)
    ]
    assert set(values) == {Literal("AFRICA"), Literal("ASIA")}


def test_repeatable_node_parameters_each_become_a_constraint() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:inRegion ;
          sh:node ex:RegionShape ;
          sh:node ex:NationShape .
        """
    )
    nodes = [
        c.shape
        for c in sg.shapes[EX.P].constraints
        if isinstance(c, NodeConstraintComponent)
    ]
    assert set(nodes) == {EX.RegionShape, EX.NationShape}


def test_repeatable_and_lists_each_become_a_constraint() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:S a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:and ( ex:A ex:B ) ;
          sh:and ( ex:C ) .
        """
    )
    lists = [
        tuple(c.shapes)
        for c in sg.shapes[EX.S].constraints
        if isinstance(c, AndConstraintComponent)
    ]
    assert set(lists) == {(EX.A, EX.B), (EX.C,)}


def test_duplicate_non_repeatable_parameter_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount 1 ;
          sh:minCount 2 .
        """
    with pytest.raises(IllFormedShapeError, match="not repeatable"):
        _parse(ttl)


def test_duplicate_datatype_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:regionKey ;
          sh:datatype xsd:integer ;
          sh:datatype xsd:string .
        """
    with pytest.raises(IllFormedShapeError, match="not repeatable"):
        _parse(ttl)


def test_duplicate_in_parameter_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:in ("AFRICA") ;
          sh:in ("ASIA") .
        """
    with pytest.raises(IllFormedShapeError, match="not repeatable"):
        _parse(ttl)


def test_duplicate_optional_parameter_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:pattern "A" ;
          sh:flags "i" ;
          sh:flags "s" .
        """
    with pytest.raises(IllFormedShapeError, match="not repeatable"):
        _parse(ttl)


def test_malformed_order_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:order "2" .
        """
    with pytest.raises(IllFormedShapeError, match="xsd:integer"):
        _parse(ttl)


def test_boolean_order_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:order true .
        """
    with pytest.raises(IllFormedShapeError, match="xsd:integer"):
        _parse(ttl)


def test_malformed_integer_parameter_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount "1" .
        """
    with pytest.raises(IllFormedShapeError, match="xsd:integer"):
        _parse(ttl)


def test_boolean_integer_parameter_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount true .
        """
    with pytest.raises(IllFormedShapeError, match="xsd:integer"):
        _parse(ttl)


def test_string_boolean_parameter_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:uniqueLang "false" .
        """
    with pytest.raises(IllFormedShapeError, match="xsd:boolean"):
        _parse(ttl)


def test_string_deactivated_flag_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:S a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:deactivated "false" .
        """
    with pytest.raises(IllFormedShapeError, match="xsd:boolean"):
        _parse(ttl)


def test_deactivated_false_is_not_treated_as_true() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:S a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:deactivated false .
        """
    )
    assert sg.shapes[EX.S].deactivated is False


def test_effective_targets_skip_deactivated_parents() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:CustomerShape a sh:NodeShape ;
          sh:targetClass ex:Customer ;
          sh:deactivated true ;
          sh:property ex:NameProp .

        ex:SupplierShape a sh:NodeShape ;
          sh:targetClass ex:Supplier ;
          sh:property ex:NameProp .

        ex:NameProp a sh:PropertyShape ;
          sh:path ex:name ;
          sh:minCount 1 .
        """
    )

    inherited = sg.effective_targets(EX.NameProp)
    assert inherited == [TargetClass(targetClass=EX.Supplier)]


def test_non_string_pattern_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:pattern 1 .
        """
    with pytest.raises(IllFormedShapeError, match="string literal"):
        _parse(ttl)


def test_iri_pattern_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:pattern ex:name .
        """
    with pytest.raises(IllFormedShapeError, match="literal"):
        _parse(ttl)


def test_non_string_flags_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:pattern "A" ;
          sh:flags 1 .
        """
    with pytest.raises(IllFormedShapeError, match="string literal"):
        _parse(ttl)


def test_non_string_message_raises() -> None:
    ttl = """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:message 1 .
        """
    with pytest.raises(IllFormedShapeError, match="string literal"):
        _parse(ttl)


def test_language_tagged_message_parses_as_str() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:message "Customer must have a name"@en .
        """
    )
    assert sg.shapes[EX.P].message == ["Customer must have a name"]


def test_pattern_with_single_flags_is_well_formed() -> None:
    sg = _parse(
        """
        @prefix ex: <http://example.org/tpch/> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .

        ex:P a sh:PropertyShape ;
          sh:path ex:name ;
          sh:pattern "A" ;
          sh:flags "i" .
        """
    )
    shape = sg.shapes[EX.P]
    pattern = _constraint(shape, PatternConstraintComponent)
    assert pattern.pattern == "A"
    assert pattern.flags == "i"
