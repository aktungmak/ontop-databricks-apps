"""Tests for the validate_shacl MCP adapter, HTTP /shacl, and app orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SH

import main
from config import Settings
from mcp_server import McpAuthError, McpRuntime, configure, validate_shacl
from ontology_store import OntologyStore
from shacl.report import VKG
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

SHAPE_IRI = "http://example.org/PersonShape"
ALICE = URIRef("http://example.org/Alice")
NAME_SHAPE = URIRef("http://example.org/NameShape")
NAME = URIRef("http://example.org/name")


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


def _call(shapes: str = SHAPES, shape: str = SHAPE_IRI) -> str:
    return asyncio.run(validate_shacl(shapes, shape))


def _report(turtle: str) -> Graph:
    graph = Graph()
    graph.parse(data=turtle, format="turtle")
    return graph


def _report_node(graph: Graph):
    reports = list(graph.subjects(RDF.type, SH.ValidationReport))
    assert len(reports) == 1
    return reports[0]


def _literal_bool(graph: Graph, subject, predicate) -> bool:
    value = graph.value(subject, predicate)
    assert value == Literal(True) or value == Literal(False)
    return bool(value)


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
        turtle = _call(shapes, "http://example.org/Shape")

    graph = _report(turtle)
    report = _report_node(graph)
    assert _literal_bool(graph, report, SH.conforms) is True
    assert _literal_bool(graph, report, VKG.truncated) is False
    assert _literal_bool(graph, report, VKG.hasTargets) is False
    assert list(graph.objects(report, SH.result)) == []
    execute.assert_not_awaited()


def test_violation_payload_and_limit_wrapper() -> None:
    _runtime()
    binding = {"focus_node": {"type": "uri", "value": str(ALICE)}}
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=_success([binding]),
        ) as execute,
    ):
        turtle = _call()

    graph = _report(turtle)
    report = _report_node(graph)
    assert _literal_bool(graph, report, SH.conforms) is False
    assert _literal_bool(graph, report, VKG.truncated) is False
    result = next(graph.objects(report, SH.result))
    assert graph.value(result, SH.focusNode) == ALICE
    assert graph.value(result, SH.sourceShape) == NAME_SHAPE
    assert graph.value(result, SH.resultPath) == NAME
    assert graph.value(result, SH.value) is None
    query = execute.await_args.args[0]
    assert query.startswith("SELECT * WHERE")
    assert query.endswith(f"LIMIT {MAX_SHACL_VIOLATIONS + 1}")
    assert query not in turtle


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
        turtle = _call()

    graph = _report(turtle)
    report = _report_node(graph)
    assert _literal_bool(graph, report, SH.conforms) is True
    assert _literal_bool(graph, report, VKG.truncated) is False
    assert _literal_bool(graph, report, VKG.hasTargets) is True
    assert list(graph.objects(report, SH.result)) == []


def test_second_query_error_discards_prior_rows() -> None:
    _runtime()
    shapes = SHAPES.replace("sh:minCount 1", "sh:minCount 1 ; sh:maxCount 2")
    first = _success(
        [{"focus_node": {"type": "uri", "value": str(ALICE)}}]
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
        turtle = _call(shapes)

    graph = _report(turtle)
    report = _report_node(graph)
    assert _literal_bool(graph, report, VKG.truncated) is True
    assert _literal_bool(graph, report, SH.conforms) is False
    assert len(list(graph.objects(report, SH.result))) == MAX_SHACL_VIOLATIONS
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
        turtle = _call(shapes)

    graph = _report(turtle)
    report = _report_node(graph)
    assert _literal_bool(graph, report, VKG.truncated) is True
    assert len(list(graph.objects(report, SH.result))) == MAX_SHACL_VIOLATIONS
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
            SHAPES.replace("sh:minCount 1", "sh:not ex:OtherShape"),
            SHAPE_IRI,
            "NotConstraintComponent",
        ),
        ("not valid turtle", SHAPE_IRI, "Invalid Turtle"),
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


def _http_client(*, running: bool = True) -> TestClient:
    manager = MagicMock()
    manager.is_running = running
    main.ontop_manager = manager
    main.app.state.http_client = AsyncMock(spec=httpx.AsyncClient)
    return TestClient(main.app, raise_server_exceptions=False)


def test_http_shacl_returns_turtle_validation_report() -> None:
    binding = {"focus_node": {"type": "uri", "value": str(ALICE)}}
    with patch(
        "shacl_validate.execute_sparql_query",
        new_callable=AsyncMock,
        return_value=_success([binding]),
    ):
        response = _http_client().post(
            f"/shacl?shape={quote(SHAPE_IRI, safe='')}",
            headers={
                "x-forwarded-access-token": "tok",
                "content-type": "text/turtle",
            },
            content=SHAPES,
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/turtle")
    graph = _report(response.text)
    report = _report_node(graph)
    assert _literal_bool(graph, report, SH.conforms) is False
    result = next(graph.objects(report, SH.result))
    assert graph.value(result, SH.focusNode) == ALICE


def test_http_and_mcp_share_the_same_report() -> None:
    _runtime()
    binding = {"focus_node": {"type": "uri", "value": str(ALICE)}}
    with (
        patch("mcp_server.get_mcp_user_token", return_value="tok"),
        patch(
            "shacl_validate.execute_sparql_query",
            new_callable=AsyncMock,
            return_value=_success([binding]),
        ),
    ):
        mcp_turtle = _call()
        http = _http_client()
        response = http.post(
            f"/shacl?shape={quote(SHAPE_IRI, safe='')}",
            headers={
                "x-forwarded-access-token": "tok",
                "content-type": "text/turtle",
            },
            content=SHAPES,
        )

    assert response.status_code == 200
    assert _report(mcp_turtle).isomorphic(_report(response.text))


def test_http_missing_shape_param_is_400() -> None:
    response = _http_client().post(
        "/shacl",
        headers={"x-forwarded-access-token": "tok", "content-type": "text/turtle"},
        content=SHAPES,
    )
    assert response.status_code == 400
    assert "shape" in response.text


def test_http_empty_body_is_400() -> None:
    response = _http_client().post(
        f"/shacl?shape={quote(SHAPE_IRI, safe='')}",
        headers={
            "x-forwarded-access-token": "tok",
            "content-type": "text/turtle",
        },
        content="  \n",
    )
    assert response.status_code == 400
    assert "Turtle shapes graph" in response.text


def test_http_missing_token_is_401() -> None:
    response = _http_client().post(
        f"/shacl?shape={quote(SHAPE_IRI, safe='')}",
        headers={"content-type": "text/turtle"},
        content=SHAPES,
    )
    assert response.status_code == 401


def test_http_ontop_down_is_503() -> None:
    with patch("shacl_validate.execute_sparql_query", new_callable=AsyncMock) as execute:
        response = _http_client(running=False).post(
            f"/shacl?shape={quote(SHAPE_IRI, safe='')}",
            headers={
                "x-forwarded-access-token": "tok",
                "content-type": "text/turtle",
            },
            content=SHAPES,
        )

    assert response.status_code == 503
    assert "Ontop is not running" in response.text
    execute.assert_not_awaited()
