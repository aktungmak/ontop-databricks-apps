"""Tests for Ontology-Based Query Check (OBQC)."""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from obqc import check_sparql, extract_bgp_triples

EX = Namespace("http://example.org/tpch/")
IN = Namespace("http://example.org/insurance/")

REPO_ROOT = Path(__file__).resolve().parents[3]
TPC_H_ONTOLOGY = REPO_ROOT / "mappings" / "ontology.ttl"


def _insurance_ontology() -> Graph:
    """Minimal ontology mirroring paper domain/direction/path examples."""
    g = Graph()
    g.bind("in", IN)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)

    for cls in (
        IN.Agent,
        IN.Policy,
        IN.Claim,
        IN.PolicyCoverageDetail,
    ):
        g.add((cls, RDF.type, OWL.Class))

    g.add((IN.soldByAgent, RDF.type, OWL.ObjectProperty))
    g.add((IN.soldByAgent, RDFS.domain, IN.Policy))
    g.add((IN.soldByAgent, RDFS.range, IN.Agent))
    g.add((IN.soldByAgent, RDFS.label, Literal("sold by agent")))

    g.add((IN.against, RDF.type, OWL.ObjectProperty))
    g.add((IN.against, RDFS.domain, IN.Claim))
    g.add((IN.against, RDFS.range, IN.PolicyCoverageDetail))

    g.add((IN.hasPolicy, RDF.type, OWL.ObjectProperty))
    g.add((IN.hasPolicy, RDFS.domain, IN.PolicyCoverageDetail))
    g.add((IN.hasPolicy, RDFS.range, IN.Policy))

    g.add((IN.policyNumber, RDF.type, OWL.DatatypeProperty))
    g.add((IN.policyNumber, RDFS.domain, IN.Policy))
    g.add((IN.policyNumber, RDFS.range, XSD.string))

    return g


def _tpch_ontology() -> Graph:
    g = Graph()
    g.parse(TPC_H_ONTOLOGY, format="turtle")
    return g


def test_domain_violation_wrong_direction() -> None:
    """Paper example: Agent as subject of soldByAgent (domain is Policy)."""
    query = """
    PREFIX in: <http://example.org/insurance/>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    SELECT ?agent ?policy WHERE {
      ?agent in:soldByAgent ?policy .
      ?agent rdf:type in:Agent .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    rules = {v["rule"] for v in result["violations"]}
    assert "domain" in rules
    domain_msgs = [v["message"] for v in result["violations"] if v["rule"] == "domain"]
    assert any("soldByAgent" in m or "sold by agent" in m for m in domain_msgs)
    assert any("Agent" in m and "Policy" in m for m in domain_msgs)


def test_range_violation_short_path() -> None:
    """Claim against Policy instead of PolicyCoverageDetail."""
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT ?claim ?policy WHERE {
      ?claim a in:Claim .
      ?policy a in:Policy .
      ?claim in:against ?policy .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "range" for v in result["violations"])
    range_msgs = [v["message"] for v in result["violations"] if v["rule"] == "range"]
    assert any("PolicyCoverageDetail" in m and "Policy" in m for m in range_msgs)


def test_double_range_violation() -> None:
    """Shared object with incompatible property ranges."""
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT * WHERE {
      ?claim in:against ?policy .
      ?detail in:hasPolicy ?policy .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "double_range" for v in result["violations"])


def test_double_domain_violation() -> None:
    """Shared subject with incompatible property domains."""
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT * WHERE {
      ?x in:soldByAgent ?agent .
      ?x in:against ?detail .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "double_domain" for v in result["violations"])


def test_domain_range_chain_violation() -> None:
    """Object of first triple is subject of second with incompatible range/domain."""
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT * WHERE {
      ?claim in:against ?policy .
      ?policy in:soldByAgent ?agent .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "domain_range" for v in result["violations"])


def test_incorrect_property_violation() -> None:
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT ?p WHERE {
      ?p a in:Policy .
      ?p in:unknownProp ?v .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "incorrect_property" for v in result["violations"])
    msgs = [v["message"] for v in result["violations"] if v["rule"] == "incorrect_property"]
    assert any("unknownProp" in m for m in msgs)


def test_valid_insurance_query_passes() -> None:
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT ?policy ?agent WHERE {
      ?policy a in:Policy .
      ?agent a in:Agent .
      ?policy in:soldByAgent ?agent .
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result == {"ok": True, "violations": []}


def test_valid_tpch_shaped_query_passes() -> None:
    query = """
    PREFIX ex: <http://example.org/tpch/>
    SELECT ?customer ?name ?order ?total WHERE {
      ?customer a ex:Customer ;
                ex:name ?name ;
                ex:placesOrder ?order .
      ?order a ex:Order ;
             ex:totalPrice ?total .
      OPTIONAL {
        ?order ex:hasLineItem ?line .
        ?line a ex:LineItem .
      }
    }
    """
    result = check_sparql(query, _tpch_ontology())
    assert result["ok"] is True
    assert result["violations"] == []


def test_tpch_direction_error_flagged() -> None:
    """Customer as subject of placedBy (domain is Order)."""
    query = """
    PREFIX ex: <http://example.org/tpch/>
    SELECT ?customer ?order WHERE {
      ?customer a ex:Customer .
      ?order a ex:Order .
      ?customer ex:placedBy ?order .
    }
    """
    result = check_sparql(query, _tpch_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "domain" for v in result["violations"])


def test_optional_bgp_is_linted() -> None:
    """BGPs inside OPTIONAL still produce violations."""
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT ?agent WHERE {
      ?agent a in:Agent .
      OPTIONAL {
        ?agent in:soldByAgent ?policy .
      }
    }
    """
    result = check_sparql(query, _insurance_ontology())
    assert result["ok"] is False
    assert any(v["rule"] == "domain" for v in result["violations"])


def test_top_class_domain_is_compatible_with_any_class() -> None:
    """A domain of rdfs:Resource constrains nothing, so must not be flagged."""
    g = _insurance_ontology()
    g.add((IN.sourceSystem, RDF.type, OWL.DatatypeProperty))
    g.add((IN.sourceSystem, RDFS.domain, RDFS.Resource))
    g.add((IN.sourceSystem, RDFS.range, XSD.string))

    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT * WHERE {
      ?policy a in:Policy ;
              in:policyNumber ?num ;
              in:sourceSystem ?src .
    }
    """
    result = check_sparql(query, g)
    assert result == {"ok": True, "violations": []}


def test_extract_bgp_includes_optional() -> None:
    query = """
    PREFIX ex: <http://example.org/tpch/>
    SELECT * WHERE {
      ?o a ex:Order .
      OPTIONAL { ?o ex:placedBy ?c }
    }
    """
    triples = extract_bgp_triples(query)
    preds = {str(p) for _, p, _ in triples}
    assert str(EX.placedBy) in preds
    assert str(RDF.type) in preds
