"""Tests for SPARQL reformulate parsing and JSON serialization."""

from __future__ import annotations

from main import extract_native_sql, extract_variable_types, to_sparql_json

CONSTRUCT_REFORMULATE = """\
ans1(c, name)
   CONSTRUCT [c, name] [c/RDF(STRINGToSTRING(c),IRI), name/RDF(STRINGToSTRING(name),xsd:string)]
      NATIVE [c, name]
SELECT c, name FROM books
"""

PLAIN_SQL_REFORMULATE = """\
SELECT id, title FROM books
"""


def test_extract_variable_types_from_construct() -> None:
    types = extract_variable_types(CONSTRUCT_REFORMULATE)
    assert types == {"c": "IRI", "name": "xsd:string"}


def test_extract_variable_types_plain_sql_returns_empty() -> None:
    assert extract_variable_types(PLAIN_SQL_REFORMULATE) == {}


def test_to_sparql_json_with_iri_and_xsd_string() -> None:
    columns = ["c", "name"]
    rows = [("http://example.com/book/1", "Moby Dick")]
    result = to_sparql_json(
        columns,
        rows,
        {"c": "IRI", "name": "xsd:string"},
    )
    assert result == {
        "head": {"vars": ["c", "name"]},
        "results": {
            "bindings": [
                {
                    "c": {"type": "uri", "value": "http://example.com/book/1"},
                    "name": {
                        "type": "literal",
                        "value": "Moby Dick",
                        "datatype": "http://www.w3.org/2001/XMLSchema#string",
                    },
                }
            ]
        },
    }


def test_to_sparql_json_without_types_uses_literal_fallback() -> None:
    columns = ["id", "title"]
    rows = [(1, "http://example.com/not-a-uri")]
    result = to_sparql_json(columns, rows)
    assert result["results"]["bindings"] == [
        {
            "id": {"type": "literal", "value": "1"},
            "title": {"type": "literal", "value": "http://example.com/not-a-uri"},
        }
    ]


def test_extract_native_sql_from_iq_tree() -> None:
    sql = extract_native_sql(CONSTRUCT_REFORMULATE)
    assert sql == "SELECT c, name FROM books"


def test_extract_native_sql_plain_sql_passthrough() -> None:
    assert extract_native_sql(PLAIN_SQL_REFORMULATE) == PLAIN_SQL_REFORMULATE.strip()
