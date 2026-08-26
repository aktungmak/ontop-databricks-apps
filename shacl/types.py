"""Shared RDF term type aliases."""

from rdflib import BNode, URIRef

ShapeRef = URIRef | BNode
PropertyRef = URIRef | BNode
ClassRef = URIRef | BNode
NodeKindRef = URIRef


class IllFormedShapeError(ValueError):
    """A shape in the shapes graph violates SHACL syntax."""
