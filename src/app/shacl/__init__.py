"""Reusable SHACL AST and SPARQL validation-query generation."""

from .report import VKG, add_violation_results, validation_report
from .shapes import ShapesGraph
from .sparql_validator import (
    UNSUPPORTED_SHACL_INVENTORY,
    ShapeCompileResult,
    SparqlValidator,
    SparqlViolationQuery,
)
from .types import IllFormedShapeError, UnsupportedShapeError

__all__ = [
    "IllFormedShapeError",
    "ShapeCompileResult",
    "ShapesGraph",
    "SparqlValidator",
    "SparqlViolationQuery",
    "UNSUPPORTED_SHACL_INVENTORY",
    "UnsupportedShapeError",
    "VKG",
    "add_violation_results",
    "validation_report",
]
