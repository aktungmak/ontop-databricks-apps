"""Tests for the validate_shacl MCP adapter and app orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp.exceptions import ToolError

from config import Settings
from mcp_server import McpAuthError, McpRuntime, configure, validate_shacl
from ontology_store import OntologyStore
from shacl_validate import MAX_SHACL_VIOLATIONS
from sparql_execute import SparqlExecuteError, SparqlExecuteSuccess

SHAPES = """
@prefix ex: <http://example.org/> .
@prefix sh: <http://www.w3.org/ns/shacl#> .

ex:PersonShape a sh:NodeShape ;
  sh:targetClass ex:Person ;
  sh:property ex:NameShape .

ex:NameShape a sh:PropertyShape ;
  sh:path ex:name ;
  sh:minCount 1 .
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


def _runtime(*, running: bool = True) -> tuple[AsyncMock, MagicMock]:
    client = AsyncMock(spec=httpx.AsyncClient)
    manager = MagicMock()
    manager.is_running = running
    configure(
        McpRuntime(
            ontology_store=OntologyStore(),
            ontop_manager=manager,
            settings=_settings(),
            http_client=client,
        )
    )
    return client, manager


def _success(bindings: list[dict]) -> SparqlExecuteSuccess:
    return SparqlExecuteSuccess(
        data={"head": {"vars": ["focus_node"]}, "results": {"bindings": bindings}}
    )


def _call(shapes: str = SHAPES, shape: str = "http://example.org/PersonShape"):
    return asyncio.run(validate_shacl(shapes, shape))


def test_missing_targets_skips_warehouse() -> None:
    _runtime()
    shapes = """
    @prefix ex: <http://example.org/> .
    @prefix sh: <http://www.w3.org/ns/shacl#> .
    ex:Shape a sh:NodeShape ; sh:nodeKind sh:IRI .
    """
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch("shacl_validate.execute_sparql_query", new_callable=AsyncMock) as execute,
    ):
        result = _call(shapes, "http://example.org/Shape")

    assert result == {
        "conforms": True,
        "violations": [],
        "truncated": False,
        "has_targets": False,
    }
    execute.assert_not_awaited()


def test_violation_payload_and_limit_wrapper() -> None:
    _runtime()
    binding = {"focus_node": {"type": "uri", "value": "http://example.org/Alice"}}
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=_success([binding]),
        ) as execute,
    ):
        result = _call()

    assert result["conforms"] is False
    assert result["truncated"] is False
    assert result["violations"][0]["focus_node"] == "http://example.org/Alice"
    assert "value" not in result["violations"][0]
    query = execute.await_args.args[0]
    assert query.startswith("SELECT * WHERE")
    assert query.endswith(f"LIMIT {MAX_SHACL_VIOLATIONS + 1}")
    assert query not in str(result)


def test_empty_bindings_conform() -> None:
    _runtime()
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=_success([]),
        ),
    ):
        result = _call()

    assert result == {
        "conforms": True,
        "violations": [],
        "truncated": False,
        "has_targets": True,
    }


def test_second_query_error_discards_prior_rows() -> None:
    _runtime()
    shapes = SHAPES.replace("sh:minCount 1", "sh:minCount 1 ; sh:maxCount 2")
    first = _success(
        [{"focus_node": {"type": "uri", "value": "http://example.org/Alice"}}]
    )
    second = SparqlExecuteError("Reformulation failed", 502)
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            side_effect=[first, second],
        ) as execute,
    ):
        with pytest.raises(ToolError, match=r"\(502\): Reformulation failed"):
            _call(shapes)

    assert execute.await_count == 2


def test_truncates_n_plus_one_bindings() -> None:
    _runtime()
    shapes = SHAPES.replace("sh:minCount 1", "sh:minCount 1 ; sh:maxCount 2")
    bindings = [
        {"focus_node": {"type": "uri", "value": f"http://example.org/{index}"}}
        for index in range(MAX_SHACL_VIOLATIONS + 1)
    ]
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            side_effect=[_success(bindings), _success([])],
        ) as execute,
    ):
        result = _call(shapes)

    assert result["truncated"] is True
    assert result["conforms"] is False
    assert len(result["violations"]) == MAX_SHACL_VIOLATIONS
    assert execute.await_count == 1


def test_stops_when_combined_violations_exceed_cap() -> None:
    _runtime()
    shapes = SHAPES.replace(
        "sh:minCount 1", "sh:minCount 1 ; sh:maxCount 2 ; sh:nodeKind sh:IRI"
    )
    first = [
        {"focus_node": {"type": "uri", "value": f"http://example.org/a{index}"}}
        for index in range(MAX_SHACL_VIOLATIONS - 1)
    ]
    second = [
        {"focus_node": {"type": "uri", "value": f"http://example.org/b{index}"}}
        for index in range(2)
    ]
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            side_effect=[_success(first), _success(second), _success([])],
        ) as execute,
    ):
        result = _call(shapes)

    assert result["truncated"] is True
    assert len(result["violations"]) == MAX_SHACL_VIOLATIONS
    assert execute.await_count == 2


def test_missing_token_maps_401() -> None:
    _runtime()
    with patch(
        "mcp_server.get_mcp_user_token",
        side_effect=McpAuthError("Missing authorization", 401),
    ):
        with pytest.raises(ToolError, match=r"\(401\): Missing authorization"):
            _call()


@pytest.mark.parametrize(
    ("shapes", "shape", "message"),
    [
        (
            SHAPES.replace("sh:minCount 1", "sh:minLength 1"),
            "http://example.org/PersonShape",
            "MinLengthConstraintComponent",
        ),
        ("not valid turtle", "http://example.org/PersonShape", "Invalid Turtle"),
        (SHAPES, "http://example.org/MissingShape", "was not found"),
    ],
)
def test_bad_shapes_map_400(shapes: str, shape: str, message: str) -> None:
    _runtime()
    with patch("mcp_server.get_mcp_user_token", return_value="tok"):
        with pytest.raises(ToolError) as exc_info:
            _call(shapes, shape)

    assert str(exc_info.value).startswith("(400):")
    assert message in str(exc_info.value)


def test_ontop_down_maps_503_before_execution() -> None:
    _runtime(running=False)
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch("shacl_validate.execute_sparql_query", new_callable=AsyncMock) as execute,
    ):
        with pytest.raises(ToolError, match=r"\(503\): Ontop is not running"):
            _call()

    execute.assert_not_awaited()


def test_permission_denied_maps_403() -> None:
    _runtime()
    error = SparqlExecuteError(
        "You lack Unity Catalog access to an object this query requires", 403
    )
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=error,
        ),
    ):
        with pytest.raises(ToolError) as exc_info:
            _call()

    assert str(exc_info.value).startswith("(403):")
    assert "Unity Catalog access" in str(exc_info.value)
