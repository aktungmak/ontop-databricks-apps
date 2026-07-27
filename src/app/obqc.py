"""Ontology-Based Query Check (OBQC) for SPARQL queries.

Deterministic RDFS consistency checks inspired by Allemang & Sequeda 2024
(https://arxiv.org/html/2405.11706v1). Stateless, returning violations only.
Callers are expected to handle repairs based on the violations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.term import BNode, Identifier, Node, Variable

logger = logging.getLogger(__name__)

# Reserved namespaces for the conjunctive query/ontology view
QUERY_GRAPH = URIRef("urn:obqc:query")
ONTOLOGY_GRAPH = URIRef("urn:obqc:ontology")
QQ = URIRef("urn:obqc:qq#")  # skolem prefix for SPARQL variables

_SPARQL_INIT_NS = {
    "rdf": RDF,
    "rdfs": RDFS,
}


@dataclass(frozen=True)
class _Rule:
    name: str
    query: str
    message: str


# v1 body rules in priority order (SELECT-clause IRI heuristics omitted)
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "double_domain",
        f"""
        SELECT ?p ?domp ?q ?domq WHERE {{
          GRAPH <{QUERY_GRAPH}> {{
            ?s ?p ?o1 .
            ?s ?q ?o2 .
          }}
          GRAPH <{ONTOLOGY_GRAPH}> {{
            ?p rdfs:domain ?domp .
            ?q rdfs:domain ?domq .
            FILTER (ISIRI(?domp) && ISIRI(?domq))
          }}
          FILTER (STR(?p) < STR(?q))
          FILTER NOT EXISTS {{
            GRAPH <{ONTOLOGY_GRAPH}> {{
              {{ ?domp rdfs:subClassOf* ?domq . }}
              UNION
              {{ ?domq rdfs:subClassOf* ?domp . }}
            }}
          }}
        }}
        """,
        (
            "The property {p} has domain {domp}, and {q} has domain {domq}, "
            "and these are incompatible."
        ),
    ),
    _Rule(
        "domain_range",
        f"""
        SELECT ?p ?rangep ?q ?domq WHERE {{
          GRAPH <{QUERY_GRAPH}> {{
            ?s ?p ?o .
            ?o ?q ?o2 .
          }}
          GRAPH <{ONTOLOGY_GRAPH}> {{
            ?p rdfs:range ?rangep .
            ?q rdfs:domain ?domq .
            FILTER (ISIRI(?rangep) && ISIRI(?domq))
          }}
          FILTER NOT EXISTS {{
            GRAPH <{ONTOLOGY_GRAPH}> {{
              {{ ?rangep rdfs:subClassOf* ?domq . }}
              UNION
              {{ ?domq rdfs:subClassOf* ?rangep . }}
            }}
          }}
        }}
        """,
        (
            "The property {p} has range {rangep}, and {q} has domain {domq}, "
            "and these are incompatible with the query."
        ),
    ),
    _Rule(
        "domain",
        f"""
        SELECT ?p ?domain ?s ?cls WHERE {{
          GRAPH <{QUERY_GRAPH}> {{
            ?s ?p ?o .
            ?s a ?cls .
          }}
          GRAPH <{ONTOLOGY_GRAPH}> {{
            ?p rdfs:domain ?domain .
            FILTER (ISIRI(?domain))
          }}
          FILTER NOT EXISTS {{
            GRAPH <{ONTOLOGY_GRAPH}> {{
              ?cls rdfs:subClassOf* ?domain .
            }}
          }}
        }}
        """,
        (
            "The property {p} has domain {domain}, but its subject {s} is a {cls}, "
            "which isn't a subclass of {domain}."
        ),
    ),
    _Rule(
        "incorrect_property",
        f"""
        SELECT DISTINCT ?p WHERE {{
          GRAPH <{QUERY_GRAPH}> {{
            ?s ?p ?o .
            FILTER (ISIRI(?p))
            FILTER (!STRSTARTS(STR(?p), "{QQ}"))
            FILTER NOT EXISTS {{
              VALUES ?ns {{ <{RDF}> <{RDFS}> <{OWL}> <{SKOS}> }}
              FILTER (STRSTARTS(STR(?p), STR(?ns)))
            }}
          }}
          FILTER NOT EXISTS {{
            GRAPH <{ONTOLOGY_GRAPH}> {{
              ?p a ?type .
            }}
          }}
        }}
        """,
        (
            "The property {p} isn't defined in the ontology. Please only use "
            "properties from the ontology, or from a standard source like "
            "rdf:, rdfs:, owl:, or skos:."
        ),
    ),
    _Rule(
        "range",
        f"""
        SELECT ?p ?range ?o ?cls WHERE {{
          GRAPH <{QUERY_GRAPH}> {{
            ?s ?p ?o .
            ?o a ?cls .
          }}
          GRAPH <{ONTOLOGY_GRAPH}> {{
            ?p rdfs:range ?range .
            FILTER (ISIRI(?range))
          }}
          FILTER NOT EXISTS {{
            GRAPH <{ONTOLOGY_GRAPH}> {{
              ?cls rdfs:subClassOf* ?range .
            }}
          }}
        }}
        """,
        (
            "The property {p} has range {range}, but its object {o} is a {cls}, "
            "which isn't a subclass of {range}."
        ),
    ),
    _Rule(
        "double_range",
        f"""
        SELECT ?p ?rangep ?q ?rangeq WHERE {{
          GRAPH <{QUERY_GRAPH}> {{
            ?s1 ?p ?o .
            ?s2 ?q ?o .
          }}
          GRAPH <{ONTOLOGY_GRAPH}> {{
            ?p rdfs:range ?rangep .
            ?q rdfs:range ?rangeq .
            FILTER (ISIRI(?rangep) && ISIRI(?rangeq))
          }}
          FILTER (STR(?p) < STR(?q))
          FILTER NOT EXISTS {{
            GRAPH <{ONTOLOGY_GRAPH}> {{
              {{ ?rangep rdfs:subClassOf* ?rangeq . }}
              UNION
              {{ ?rangeq rdfs:subClassOf* ?rangep . }}
            }}
          }}
        }}
        """,
        (
            "The property {p} has range {rangep}, and {q} has range {rangeq}, "
            "and these are incompatible."
        ),
    ),
)


def check_sparql(query: str, ontology: Graph) -> dict[str, Any]:
    """Check a SPARQL query against an ontology using OBQC body rules.

    Args:
        query: SPARQL query text.
        ontology: RDFS/OWL TBox as an rdflib graph.

    Returns:
        Result dict with ``ok`` and ``violations``.
    """
    try:
        bgp_triples = extract_bgp_triples(query)
    except Exception as exc:
        logger.debug("OBQC SPARQL parse failed: %s", exc)
        return {
            "ok": False,
            "violations": [
                {
                    "rule": "parse_error",
                    "message": f"Could not parse SPARQL query: {exc}",
                }
            ],
        }

    query_graph = materialize_bgp(bgp_triples)
    dataset = _build_conjunctive_dataset(query_graph, ontology)
    labels = _term_labels(ontology)

    violations: list[dict[str, Any]] = [
        _format_violation(rule, binding, labels)
        for rule in _RULES
        for binding in _run_rule(dataset, rule)
    ]

    return {
        "ok": len(violations) == 0,
        "violations": violations,
    }


def extract_bgp_triples(query: str) -> list[tuple[Node, Node, Node]]:
    """Parse SPARQL and collect BGP triples from the WHERE pattern.

    Includes triples inside OPTIONAL, UNION, MINUS, and FILTER (NOT) EXISTS
    so those patterns are linted as in the paper.
    """
    algebra = translateQuery(parseQuery(query)).algebra
    triples: list[tuple[Node, Node, Node]] = []
    _collect_bgp_triples(algebra, triples)
    return triples


def materialize_bgp(
    triples: Sequence[tuple[Node, Node, Node]],
) -> Graph:
    """Materialize BGP triples with skolem IRIs for SPARQL variables.

    Variables become ``urn:obqc:qq#<varname>`` so the query BGP can be
    queried as RDF alongside the ontology.
    """
    g = Graph()
    for s, p, o in triples:
        g.add((_skolemize(s), _skolemize(p), _skolemize(o)))
    return g


def _collect_bgp_triples(node: Any, out: list[tuple[Node, Node, Node]]) -> None:
    if node is None:
        return

    name = getattr(node, "name", None)
    if name in ("BGP", "TriplesBlock"):
        for triple in node.get("triples") or ():
            if isinstance(triple, tuple) and len(triple) == 3:
                out.append(triple)  # type: ignore[arg-type]
        return

    if not hasattr(node, "keys"):
        return

    for key in node.keys():
        if key == "_vars":
            continue
        value = node[key]
        if hasattr(value, "name") or hasattr(value, "keys"):
            _collect_bgp_triples(value, out)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if hasattr(item, "name") or hasattr(item, "keys"):
                    _collect_bgp_triples(item, out)


def _skolemize(term: Node) -> Identifier:
    if isinstance(term, Variable):
        return URIRef(f"{QQ}{term}")
    if isinstance(term, BNode):
        return URIRef(f"{QQ}b_{term}")
    return term  # type: ignore[return-value]


def _build_conjunctive_dataset(query_graph: Graph, ontology: Graph) -> Dataset:
    ds = Dataset()
    qg = ds.graph(identifier=QUERY_GRAPH)
    og = ds.graph(identifier=ONTOLOGY_GRAPH)
    for triple in query_graph:
        qg.add(triple)
    for triple in ontology:
        og.add(triple)
    return ds


def _run_rule(dataset: Dataset, rule: _Rule) -> list[Mapping[str, Node]]:
    rows: list[Mapping[str, Node]] = []
    for result in dataset.query(rule.query, initNs=_SPARQL_INIT_NS):
        binding = {str(var): result[var] for var in result.labels}
        rows.append(binding)
    return rows


def _format_violation(
    rule: _Rule,
    binding: Mapping[str, Node],
    labels: Mapping[str, str],
) -> dict[str, Any]:
    display = {key: _format_term(value, labels) for key, value in binding.items()}
    try:
        message = rule.message.format(**display)
    except KeyError:
        message = f"{rule.name}: " + ", ".join(f"{k}={v}" for k, v in display.items())

    violation: dict[str, Any] = {"rule": rule.name, "message": message}
    # Preserve structured bindings (display form) for MCP / repair consumers.
    for key, value in display.items():
        violation[key] = value
    return violation


def _format_term(term: Node, labels: Mapping[str, str]) -> str:
    if isinstance(term, URIRef):
        iri = str(term)
        if iri.startswith(str(QQ)):
            local = iri[len(str(QQ)) :]
            if local.startswith("b_"):
                return f"_:{local[2:]}"
            return f"?{local}"
        if iri in labels:
            return labels[iri]
        if "#" in iri:
            return iri.rsplit("#", 1)[-1]
        return iri.rsplit("/", 1)[-1] or iri
    if isinstance(term, Literal):
        return str(term)
    return str(term)


def _term_labels(ontology: Graph) -> dict[str, str]:
    """Prefer rdfs:label for compact English messages when available."""
    labels: dict[str, str] = {}
    for subject, _, literal in ontology.triples((None, RDFS.label, None)):
        if isinstance(subject, URIRef) and isinstance(literal, Literal):
            labels.setdefault(str(subject), str(literal))
    return labels
