"""Tests for OntologyStore load, search, describe, and missing-file behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontology_store import OntologyStore

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_ONTOLOGY = REPO_ROOT / "mappings" / "ontology.ttl"


@pytest.fixture(scope="module")
def store() -> OntologyStore:
    if not SAMPLE_ONTOLOGY.is_file():
        pytest.skip(f"Sample ontology not found at {SAMPLE_ONTOLOGY}")
    loaded = OntologyStore.load(SAMPLE_ONTOLOGY)
    assert loaded.is_available()
    return loaded


def test_load_sample_ontology(store: OntologyStore) -> None:
    assert store.graph is not None
    assert len(store.graph) > 0
    turtle = store.search("customer", limit=10)
    assert "Ontology not loaded" not in turtle
    assert "Customer" in turtle or "ex:Customer" in turtle


def test_search_customer(store: OntologyStore) -> None:
    turtle = store.search("customer", limit=10)
    assert "Ontology not loaded" not in turtle
    assert "Customer" in turtle or "ex:Customer" in turtle or "customer" in turtle.lower()


def test_search_line_item(store: OntologyStore) -> None:
    turtle = store.search("line item", limit=10)
    assert "Ontology not loaded" not in turtle
    assert "LineItem" in turtle or "line item" in turtle.lower()


PLACED_BY_IRI = "http://example.org/tpch/placedBy"


def test_describe_full_iri(store: OntologyStore) -> None:
    turtle = store.describe(PLACED_BY_IRI)
    assert "Ontology not loaded" not in turtle
    assert "placedBy" in turtle
    assert "domain" in turtle.lower() or "ex:Order" in turtle
    assert "range" in turtle.lower() or "ex:Customer" in turtle


def test_describe_rejects_curie_and_local_name(store: OntologyStore) -> None:
    for term in ("ex:placedBy", "placedBy"):
        turtle = store.describe(term)
        assert "IRI not found" in turtle
        assert "expected full IRI" in turtle


def test_search_matches_comment_independently(tmp_path: Path) -> None:
    """Each label/comment is scored on its own, not as one concatenated string."""
    ttl = tmp_path / "mini.ttl"
    ttl.write_text(
        """
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix ex: <http://example.org/> .

        ex:Widget a owl:Class ;
            rdfs:label "Widget" ;
            rdfs:comment "a sprocket used in assembly lines" .
        """,
        encoding="utf-8",
    )
    store = OntologyStore.load(ttl)
    assert store.is_available()
    turtle = store.search("sprocket assembly", limit=5)
    assert "Widget" in turtle or "ex:Widget" in turtle


def test_missing_file_not_available() -> None:
    missing = OntologyStore.load(REPO_ROOT / "mappings" / "does-not-exist.ttl")
    assert missing.is_available() is False
    assert "Ontology not loaded" in missing.search("customer")
    assert "Ontology not loaded" in missing.describe(PLACED_BY_IRI)


def test_none_path_not_available() -> None:
    empty = OntologyStore.load(None)
    assert empty.is_available() is False
    assert "Ontology not loaded" in empty.search("customer")
