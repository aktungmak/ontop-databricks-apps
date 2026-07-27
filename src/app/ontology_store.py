"""In-memory ontology cache with rapidfuzz label/comment search."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

_NOT_LOADED = (
    "# Ontology not loaded\n# Discovery tools require an ontology Turtle file."
)

_TYPED_PREDICATES = (RDF.type,)
_ANNOTATION_PREDICATES = (RDFS.label, RDFS.comment)
_SCHEMA_PREDICATES = (
    RDFS.domain,
    RDFS.range,
    RDFS.subClassOf,
    RDFS.subPropertyOf,
    OWL.inverseOf,
)

_DECLARATION_TYPES = frozenset(
    {
        OWL.Class,
        OWL.ObjectProperty,
        OWL.DatatypeProperty,
        OWL.AnnotationProperty,
        OWL.Ontology,
        RDFS.Class,
        RDF.Property,
    }
)


def _best_text_score(
    query: str,
    texts: list[str],
    *,
    score_cutoff: float | None = None,
    **_: object,
) -> float:
    """Score an IRI by its best matching label, comment, or IRI fallback."""
    return max(fuzz.WRatio(query, text, score_cutoff=score_cutoff) for text in texts)


class OntologyStore:
    """Load ontology into rdflib and serve Turtle excerpts via a fuzzy search index."""

    def __init__(self) -> None:
        self._graph: Graph | None = None
        self._available = False
        # IRI → label/comment strings (and full-IRI fallbacks), matched independently
        self._iri_to_texts: dict[str, list[str]] = {}

    @classmethod
    def load(cls, path: Path | str | None) -> OntologyStore:
        """Load ontology from ``path``. Missing/unreadable paths yield an unavailable store."""
        store = cls()
        if path is None:
            logger.info("OntologyStore: no ontology path provided")
            return store

        ontology_path = Path(path)
        if not ontology_path.is_file():
            logger.info("OntologyStore: ontology file not found at %s", ontology_path)
            return store

        graph = Graph()
        try:
            graph.parse(ontology_path, format="turtle")
        except Exception:
            logger.exception("OntologyStore: failed to parse %s", ontology_path)
            return store

        store._graph = graph
        store._available = True
        store._build_search_index()
        logger.info(
            "OntologyStore: loaded %s (%d triples, %d searchable terms)",
            ontology_path,
            len(graph),
            len(store._iri_to_texts),
        )
        return store

    def is_available(self) -> bool:
        return self._available

    @property
    def graph(self) -> Graph | None:
        """In-memory rdflib graph when loaded; ``None`` if unavailable."""
        return self._graph

    def search(self, query: str, limit: int = 10) -> str:
        if not self._available or self._graph is None:
            return _NOT_LOADED

        if not query.strip() or not self._iri_to_texts:
            return "# No matches\n"

        matches = process.extract(
            query.strip(),
            self._iri_to_texts,
            scorer=_best_text_score,
            limit=max(1, limit),
        )

        out = Graph()
        out.namespace_manager = self._graph.namespace_manager
        for _texts, _score, iri in matches:
            uri = URIRef(iri)
            for p in (*_TYPED_PREDICATES, *_ANNOTATION_PREDICATES, *_SCHEMA_PREDICATES):
                for o in self._graph.objects(uri, p):
                    out.add((uri, p, o))
        if len(out) == 0:
            return "# No matches\n"
        return out.serialize(format="turtle")

    def describe(self, iri: str) -> str:
        """Return a Turtle neighborhood for ``iri``.

        ``iri`` must be a full IRI (e.g. ``http://example.org/tpch/placedBy``).
        Prefixed names and bare local names are not resolved.
        """
        if not self._available or self._graph is None:
            return _NOT_LOADED

        uri = URIRef(iri)
        out = Graph()
        out.namespace_manager = self._graph.namespace_manager

        # Outgoing triples from the term
        for p, o in self._graph.predicate_objects(uri):
            out.add((uri, p, o))

        # Incoming schema links that reference the term (e.g. inverseOf, domain of others)
        for s, p in self._graph.subject_predicates(uri):
            if p in _SCHEMA_PREDICATES or p == OWL.inverseOf:
                out.add((s, p, uri))
                for ann in _ANNOTATION_PREDICATES:
                    for lit in self._graph.objects(s, ann):
                        out.add((s, ann, lit))
                for t in self._graph.objects(s, RDF.type):
                    out.add((s, RDF.type, t))

        if len(out) == 0:
            return f"# IRI not found (expected full IRI): {iri}\n"

        return out.serialize(format="turtle")

    def _build_search_index(self) -> None:
        assert self._graph is not None
        texts: dict[str, list[str]] = defaultdict(list)

        for s, p, o in self._graph:
            if p not in _ANNOTATION_PREDICATES:
                continue
            if not isinstance(o, Literal):
                continue
            text = str(o).strip()
            if text:
                texts[str(s)].append(text)

        # Also index typed terms that lack labels/comments (use the full IRI).
        for s, o in self._graph.subject_objects(RDF.type):
            if not isinstance(s, URIRef):
                continue
            if not isinstance(o, URIRef) or o not in _DECLARATION_TYPES:
                continue
            iri = str(s)
            if iri not in texts:
                texts[iri].append(iri)

        self._iri_to_texts = dict(texts)
