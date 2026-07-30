"""Tests for Ontop reformulate request shape from ``execute_sparql_query``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs

import httpx

from config import Settings
from sparql_execute import execute_sparql_query

QUERY = (
    "PREFIX ex: <http://example.org/tpch/>\n"
    "SELECT ?name (COUNT(?order) AS ?numOrders)\n"
    "WHERE {\n"
    "  ?customer ex:name ?name .\n"
    "  ?customer ex:placesOrder ?order .\n"
    "}\n"
    "GROUP BY ?name\nORDER BY DESC(?numOrders)\nLIMIT 10"
)


def _settings() -> Settings:
    return Settings(
        warehouse_id="wh",
        mappings_volume_path="/Volumes/test/mappings",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=Path("/tmp/ontop-vkg-test"),
        fm_model_name="test-model",
    )


def _ontop_running() -> MagicMock:
    manager = MagicMock()
    manager.is_running = True
    return manager


def test_posts_form_encoded_query_to_reformulate() -> None:
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.text = "SELECT name FROM customer"
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = upstream

    with patch("sparql_execute.run_sql", return_value=(["name"], [("Alice",)])):
        asyncio.run(
            execute_sparql_query(QUERY, "tok", _settings(), client, _ontop_running())
        )

    client.post.assert_awaited_once()
    call = client.post.call_args
    assert call.args[0].endswith("/ontop/reformulate")
    assert call.kwargs["headers"] == {
        "content-type": "application/x-www-form-urlencoded"
    }
    assert parse_qs(call.kwargs["content"].decode("utf-8")) == {"query": [QUERY]}
