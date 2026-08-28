"""Tests for the unbound-predicate guard in execute_sparql_query.

A fully-unbound-predicate triple (a variable predicate with no bound IRI endpoint, e.g.
``?s ?p ?o``) forces Ontop to union over the whole mapped vocabulary — the shape that
walls reformulation under load — so it is rejected before the reformulate call. A bound
IRI at either endpoint (``<iri> ?p ?o`` describe, ``?s ?p <iri>`` reverse lookup) lets
Ontop prune the union and is allowed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from config import Settings
from sparql_execute import (
    SparqlExecuteError,
    _unbound_predicate_triple,
    execute_sparql_query,
)


def _settings() -> Settings:
    return Settings(
        warehouse_id="wh",
        mappings_volume_path="/Volumes/test/mappings",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog="test_catalog",
        default_schema="test_schema",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=Path("/tmp/ontop-vkg-test"),
        fm_model_name="test-model",
    )


def test_fully_unbound_triple_is_flagged() -> None:
    assert _unbound_predicate_triple("SELECT * WHERE { ?s ?p ?o }") is not None


def test_unbound_join_chain_is_flagged() -> None:
    query = "SELECT * WHERE { ?s ?p ?o . ?o ?p2 ?v . ?v ?p3 ?w }"
    assert _unbound_predicate_triple(query) is not None


def test_bound_predicates_pass() -> None:
    query = """
    PREFIX in: <http://example.org/insurance/>
    SELECT ?policy ?agent WHERE {
      ?policy a in:Policy .
      ?policy in:soldByAgent ?agent .
    }
    """
    assert _unbound_predicate_triple(query) is None


def test_variable_predicate_on_bound_subject_passes() -> None:
    """Single-entity describe ``<iri> ?p ?o`` is bounded and allowed."""
    query = (
        "SELECT ?p ?o WHERE { <http://example.org/insurance/policy-1> ?p ?o }"
    )
    assert _unbound_predicate_triple(query) is None


def test_variable_predicate_on_bound_object_passes() -> None:
    """Reverse lookup ``?s ?p <iri>`` binds the object, so Ontop can prune — allowed."""
    query = (
        "SELECT ?s ?p WHERE { ?s ?p <http://example.org/insurance/policy-1> }"
    )
    assert _unbound_predicate_triple(query) is None


def test_unparseable_query_falls_through() -> None:
    """A query rdflib cannot parse is left for Ontop to reject."""
    assert _unbound_predicate_triple("NOT SPARQL AT ALL") is None


def test_execute_sparql_query_rejects_unbound_predicate_before_reformulate() -> None:
    """The guard short-circuits to a 400 without touching the reformulate endpoint."""
    client = AsyncMock(spec=httpx.AsyncClient)
    manager = MagicMock()
    manager.is_running = True

    result = asyncio.run(
        execute_sparql_query(
            "SELECT * WHERE { ?s ?p ?o }", "tok", _settings(), client, manager
        )
    )

    assert isinstance(result, SparqlExecuteError)
    assert result.status_code == 400
    assert "unbound predicate" in result.message.lower()
    client.post.assert_not_awaited()
