"""Error-path tests for shared SPARQL execution (MCP-friendly result shape)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from config import Settings
from sparql_execute import (
    SparqlExecuteError,
    SparqlExecuteSuccess,
    execute_sparql_query,
)
from sparql_execute import is_permission_denied, permission_denied_summary

TABLE_DENIAL = (
    "[INSUFFICIENT_PERMISSIONS] Insufficient privileges:\n"
    "User does not have SELECT on Table 'cat.sch.tbl'. SQLSTATE: 42501"
)
SCHEMA_DENIAL = (
    "[INSUFFICIENT_PERMISSIONS] Insufficient privileges:\n"
    "User does not have USE SCHEMA on Schema 'cat.sch'. SQLSTATE: 42501"
)
CATALOG_DENIAL = (
    "[INSUFFICIENT_PERMISSIONS] Insufficient privileges:\n"
    "User does not have USE CATALOG on Catalog 'cat'. SQLSTATE: 42501"
)
CATALOG_SCOPE_DENIAL = (
    "[INSUFFICIENT_PERMISSIONS] Insufficient privileges:\n"
    "Catalog 'cat' is not accessible in current workspace SQLSTATE: 42501"
)


def test_is_permission_denied_matches_all_levels() -> None:
    for msg in (TABLE_DENIAL, SCHEMA_DENIAL, CATALOG_DENIAL, CATALOG_SCOPE_DENIAL):
        assert is_permission_denied(msg) is True


def test_is_permission_denied_ignores_other_errors() -> None:
    assert is_permission_denied("Warehouse is stopped") is False
    assert is_permission_denied("[PARSE_SYNTAX_ERROR] near 'SELCT'") is False


def test_permission_denied_summary_is_single_line_no_stack() -> None:
    summary = permission_denied_summary(TABLE_DENIAL)
    assert summary == (
        "You lack Unity Catalog access to an object this query requires: "
        "User does not have SELECT on Table 'cat.sch.tbl'. SQLSTATE: 42501"
    )
    assert "\n" not in summary
    assert "org.apache.spark" not in summary

NATIVE_SQL = "SELECT c, name FROM books WHERE c = 'secret'"

CONSTRUCT_REFORMULATE = f"""\
ans1(c, name)
   CONSTRUCT [c, name] [c/RDF(STRINGToSTRING(c),IRI), name/RDF(STRINGToSTRING(name),xsd:string)]
      NATIVE [c, name]
{NATIVE_SQL}
"""


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


def _ontop_running() -> MagicMock:
    manager = MagicMock()
    manager.is_running = True
    return manager


def _error_has_no_native_sql(result: SparqlExecuteError) -> None:
    assert isinstance(result, SparqlExecuteError)
    assert NATIVE_SQL not in result.message
    assert "SELECT c, name FROM books" not in result.message
    assert not hasattr(result, "sql")
    assert not hasattr(result, "native_sql")
    assert set(result.__dataclass_fields__) == {"message", "status_code"}


def test_ontop_not_running_returns_503() -> None:
    manager = MagicMock()
    manager.is_running = False
    client = AsyncMock(spec=httpx.AsyncClient)

    result = asyncio.run(
        execute_sparql_query(
            "SELECT ?s WHERE { ?s ?p ?o }",
            "tok",
            _settings(),
            client,
            manager,
        )
    )

    assert isinstance(result, SparqlExecuteError)
    assert result.status_code == 503
    assert result.message == "Ontop is not running"
    client.post.assert_not_called()


def test_ontop_unreachable_returns_502_without_sql() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.side_effect = httpx.ConnectError("connection refused")

    result = asyncio.run(
        execute_sparql_query(
            "SELECT ?s WHERE { ?s ?p ?o }",
            "tok",
            _settings(),
            client,
            _ontop_running(),
        )
    )

    assert isinstance(result, SparqlExecuteError)
    assert result.status_code == 502
    assert "Failed to reach Ontop" in result.message
    _error_has_no_native_sql(result)


def test_ontop_reformulate_error_surfaces_message_not_sql() -> None:
    upstream = MagicMock()
    upstream.status_code = 400
    upstream.text = (
        '{"timestamp":"2026-01-01","status":400,"error":"Bad Request",'
        '"message":"Unsupported SPARQL feature: MINUS","path":"/ontop/reformulate"}'
    )
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = upstream

    result = asyncio.run(
        execute_sparql_query(
            "SELECT ?s WHERE { ?s ?p ?o MINUS { ?s a ?t } }",
            "tok",
            _settings(),
            client,
            _ontop_running(),
        )
    )

    assert isinstance(result, SparqlExecuteError)
    assert result.status_code == 400
    assert "Unsupported SPARQL feature: MINUS" in result.message
    _error_has_no_native_sql(result)


def test_dbsql_failure_surfaces_message_without_native_sql() -> None:
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.text = CONSTRUCT_REFORMULATE
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = upstream

    with patch(
        "sparql_execute.run_sql",
        side_effect=RuntimeError("Warehouse is stopped"),
    ):
        result = asyncio.run(
            execute_sparql_query(
                "SELECT ?c ?name WHERE { ?c <ex:name> ?name }",
                "tok",
                _settings(),
                client,
                _ontop_running(),
            )
        )

    assert isinstance(result, SparqlExecuteError)
    assert result.status_code == 502
    assert result.message == "Warehouse is stopped"
    _error_has_no_native_sql(result)


def test_success_returns_sparql_json_not_sql() -> None:
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.text = CONSTRUCT_REFORMULATE
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = upstream

    with patch(
        "sparql_execute.run_sql",
        return_value=(["c", "name"], [("http://ex/1", "Alice")]),
    ):
        result = asyncio.run(
            execute_sparql_query(
                "SELECT ?c ?name WHERE { ?c <ex:name> ?name }",
                "tok",
                _settings(),
                client,
                _ontop_running(),
            )
        )

    assert isinstance(result, SparqlExecuteSuccess)
    assert "head" in result.data
    assert "results" in result.data
    assert NATIVE_SQL not in str(result.data)
