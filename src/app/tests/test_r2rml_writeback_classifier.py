from __future__ import annotations

import pytest
from rdflib import Graph

from actions.models import ActionDefinition
from actions.r2rml_classifier import classify_writeback


MAPPING = """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ont: <https://example.com/ontology#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ont:SupplierMap a rr:TriplesMap ;
  rr:logicalTable [
    rr:sqlQuery "SELECT `supplier_id` AS `supplier_id`, `supplier_name` AS `supplier_name` FROM `cat`.`sch`.`supplier` WHERE `supplier_id` IS NOT NULL"
  ] ;
  rr:subjectMap [
    rr:class ont:Supplier ;
    rr:template "https://example.com/ontology/Supplier/{supplier_id}"
  ] ;
  rr:predicateObjectMap [
    rr:predicate ont:supplierName ;
    rr:objectMap [ rr:column "supplier_name" ; rr:datatype xsd:string ]
  ] .
"""


def _action() -> ActionDefinition:
    return ActionDefinition(
        iri="https://example.com/ontology#updateSupplierName",
        logical_key="updateSupplierName",
        kind="WRITE_BACK",
        bound_class_iri="https://example.com/ontology#Supplier",
        target_property_iri="https://example.com/ontology#supplierName",
        status="PUBLISHED",
    )


def test_classifier_accepts_simple_studio_mapping():
    mapping = Graph().parse(data=MAPPING, format="turtle")
    action = _action()

    catalog = classify_writeback(mapping, None, [action])

    target = catalog.target_for_action(action.iri)
    assert target is not None
    assert target.table_fqn == ("cat", "sch", "supplier")
    assert target.key_column == "supplier_id"
    assert target.value_column == "supplier_name"
    assert (
        target.subject_template == "https://example.com/ontology/Supplier/{supplier_id}"
    )


@pytest.mark.parametrize(
    ("mapping_ttl", "reason"),
    [
        (
            MAPPING.replace(
                'rr:column "supplier_name"',
                'rr:template "name/{supplier_name}"',
            ),
            "UNSUPPORTED_OBJECT_MAP",
        ),
        (
            MAPPING.replace(
                "SELECT `supplier_id` AS `supplier_id`, `supplier_name` AS `supplier_name`",
                "SELECT DISTINCT `supplier_id` AS `supplier_id`, `supplier_name` AS `supplier_name`",
            ),
            "DISTINCT_OR_AGGREGATED_QUERY",
        ),
        (
            MAPPING.replace(
                "FROM `cat`.`sch`.`supplier`",
                "FROM `cat`.`sch`.`supplier` JOIN `cat`.`sch`.`other` ON supplier.id = other.id",
            ),
            "JOINED_OR_COMPUTED_QUERY",
        ),
        (
            MAPPING.replace(
                "`supplier_name` AS `supplier_name`",
                "upper(`supplier_name`) AS `supplier_name`",
            ),
            "VALUE_MAPPING_NOT_PLAIN_COLUMN",
        ),
    ],
)
def test_classifier_blocks_unsafe_mapping_shapes(mapping_ttl, reason):
    mapping = Graph().parse(data=mapping_ttl, format="turtle")
    action = _action()

    catalog = classify_writeback(mapping, None, [action])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert reason in classification.reasons


def test_classifier_blocks_coupled_source_column():
    coupled = MAPPING.replace(
        "] .\n",
        """] ;
  rr:predicateObjectMap [
    rr:predicate ont:supplierDisplayName ;
    rr:objectMap [ rr:column "supplier_name" ; rr:datatype xsd:string ]
  ] .
""",
    )
    mapping = Graph().parse(data=coupled, format="turtle")
    action = _action()

    catalog = classify_writeback(mapping, None, [action])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "COUPLED_SOURCE_COLUMN" in classification.reasons


def test_classifier_blocks_iri_term_type_without_ontology():
    mapping = Graph().parse(
        data=MAPPING.replace(
            "rr:datatype xsd:string",
            "rr:termType rr:IRI",
        ),
        format="turtle",
    )

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "NOT_DATATYPE_PROPERTY" in classification.reasons


def test_classifier_blocks_unrelated_computed_projection():
    mapping = Graph().parse(
        data=MAPPING.replace(
            "`supplier_name` AS `supplier_name`",
            "`supplier_name` AS `supplier_name`, upper(`supplier_name`) AS `display_name`",
        ),
        format="turtle",
    )

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "JOINED_OR_COMPUTED_QUERY" in classification.reasons


def test_classifier_blocks_multiple_object_maps():
    mapping = Graph().parse(
        data=MAPPING.replace(
            "]\n  ] .",
            '] ;\n    rr:objectMap [ rr:column "other_name" ; rr:datatype xsd:string ]\n  ] .',
        ),
        format="turtle",
    )

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "MULTIPLE_PROPERTY_MAPPINGS" in classification.reasons


def test_classifier_blocks_template_coupling():
    mapping = Graph().parse(
        data=MAPPING.replace(
            "] .\n",
            """] ;
  rr:predicateObjectMap [
    rr:predicate ont:supplierDisplayName ;
    rr:objectMap [ rr:template "name/{supplier_name}" ]
  ] .
""",
        ),
        format="turtle",
    )

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "COUPLED_SOURCE_COLUMN" in classification.reasons


@pytest.mark.parametrize("clause", ["ORDER BY `supplier_name`", "LIMIT 1"])
def test_classifier_blocks_unsupported_query_clauses(clause):
    mapping = Graph().parse(
        data=MAPPING.replace(
            "WHERE `supplier_id` IS NOT NULL",
            f"WHERE `supplier_id` IS NOT NULL {clause}",
        ),
        format="turtle",
    )

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "JOINED_OR_COMPUTED_QUERY" in classification.reasons


def test_classifier_blocks_table_sample():
    mapping = Graph().parse(
        data=MAPPING.replace(
            "FROM `cat`.`sch`.`supplier`",
            "FROM `cat`.`sch`.`supplier` TABLESAMPLE (10 PERCENT)",
        ),
        format="turtle",
    )

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification.writable is False
    assert "JOINED_OR_COMPUTED_QUERY" in classification.reasons


@pytest.mark.parametrize(
    ("mapping_ttl", "reason"),
    [
        (
            MAPPING.replace(
                "ont:SupplierMap a rr:TriplesMap ;",
                'ont:SupplierMap a rr:TriplesMap ;\n  rr:logicalTable [ rr:tableName "cat.sch.other" ] ;',
            ),
            "UNSUPPORTED_LOGICAL_TABLE",
        ),
        (
            MAPPING.replace(
                "rr:subjectMap [",
                'rr:subjectMap [ rr:class ont:Supplier ; rr:template "https://example.com/ontology/Supplier/{other_id}" ] ;\n  rr:subjectMap [',
            ),
            "UNSUPPORTED_SUBJECT_TEMPLATE",
        ),
        (
            MAPPING.replace(
                'rr:column "supplier_name"',
                'rr:column "supplier_name", "other_name"',
            ),
            "UNSUPPORTED_OBJECT_MAP",
        ),
        (
            MAPPING.replace(
                "rr:predicate ont:supplierName",
                "rr:predicate ont:supplierName, ont:supplierDisplayName",
            ),
            "MULTIPLE_PROPERTY_MAPPINGS",
        ),
        (
            MAPPING.replace(
                "`supplier_name` AS `supplier_name` FROM",
                "`supplier_name` AS `supplier_name`, `other_name` AS `supplier_name` FROM",
            ),
            "JOINED_OR_COMPUTED_QUERY",
        ),
    ],
)
def test_classifier_rejects_ambiguous_inversion_cardinality(mapping_ttl, reason):
    mapping = Graph().parse(data=mapping_ttl, format="turtle")

    catalog = classify_writeback(mapping, None, [_action()])

    classification = catalog.property_for(
        "https://example.com/ontology#Supplier",
        "https://example.com/ontology#supplierName",
    )
    assert classification is not None
    assert classification.writable is False
    assert reason in classification.reasons
