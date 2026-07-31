"""Tests for MCP auth and execute_sparql tool adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from config import Settings
from mcp_server import (
    McpAuthError,
    McpRuntime,
    check_sparql,
    configure,
    execute_sparql,
    get_mcp_user_token,
)
from ontology_store import OntologyStore
from sparql_execute import SparqlExecuteError, SparqlExecuteSuccess


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


def _configure_runtime(
    *,
    client: httpx.AsyncClient,
    manager: MagicMock,
) -> None:
    configure(
        McpRuntime(
            ontology_store=OntologyStore(),
            ontop_manager=manager,
            settings=_settings(),
            http_client=client,
        )
    )


def test_get_mcp_user_token_missing_raises() -> None:
    with patch("mcp_server.get_http_headers", return_value={}):
        try:
            get_mcp_user_token()
            raise AssertionError("expected McpAuthError")
        except McpAuthError as exc:
            assert exc.status_code == 401
            assert "authorization" in exc.message.lower()


def test_get_mcp_user_token_strips_bearer() -> None:
    with patch(
        "mcp_server.get_http_headers",
        return_value={"x-forwarded-access-token": "Bearer abc.def"},
    ):
        assert get_mcp_user_token() == "abc.def"


def test_execute_sparql_maps_error_without_sql() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    manager = MagicMock()
    manager.is_running = True
    _configure_runtime(client=client, manager=manager)

    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "mcp_server.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=SparqlExecuteError(
                message="Unsupported SPARQL feature: MINUS",
                status_code=400,
            ),
        ) as exec_mock,
    ):
        result = asyncio.run(execute_sparql("SELECT * WHERE { ?s ?p ?o }"))

    assert isinstance(result, str)
    assert "Error (400)" in result
    assert "MINUS" in result
    assert "SELECT c FROM" not in result
    exec_mock.assert_awaited_once()
    # Must call shared module with query string (not HTTP to /sparql).
    assert exec_mock.await_args.args[0] == "SELECT * WHERE { ?s ?p ?o }"


def test_execute_sparql_returns_json_on_success() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    manager = MagicMock()
    manager.is_running = True
    _configure_runtime(client=client, manager=manager)
    payload = {"head": {"vars": ["s"]}, "results": {"bindings": []}}

    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "mcp_server.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=SparqlExecuteSuccess(data=payload),
        ),
    ):
        result = asyncio.run(execute_sparql("SELECT ?s WHERE { ?s ?p ?o }"))

    assert result == payload


def test_check_sparql_unavailable_ontology() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    manager = MagicMock()
    _configure_runtime(client=client, manager=manager)

    result = check_sparql("SELECT * WHERE { ?s ?p ?o }")

    assert result["ok"] is False
    assert result["ontology_available"] is False
    assert "ontology" in result["message"].lower()
    assert "violations" not in result
