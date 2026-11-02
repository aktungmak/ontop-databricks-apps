"""HTTP /sql: the SQL a SPARQL query runs as, without running it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import main

REFORMULATE = (
    "ans1(c)\n"
    "   CONSTRUCT [c] [c/RDF(STRINGToSTRING(c),IRI)]\n"
    "      NATIVE [c]\n"
    "SELECT c FROM t\n"
)
SIGNED_IN = {"x-forwarded-access-token": "tok"}
QUERY = {"query": "SELECT ?c WHERE { ?c <ex:name> ?o }"}


@pytest.fixture
def ontop(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Ontop running, and the HTTP client it answers through."""
    manager = MagicMock()
    manager.is_running = True
    monkeypatch.setattr(main, "ontop_manager", manager)
    client = AsyncMock()
    client.post.return_value = MagicMock(status_code=200, text=REFORMULATE)
    main.app.state.http_client = client
    return client


def test_returns_native_sql(ontop: AsyncMock) -> None:
    response = TestClient(main.app).post("/sql", headers=SIGNED_IN, data=QUERY)
    assert response.status_code == 200
    assert response.text == "SELECT c FROM t"


def test_requires_sign_in(ontop: AsyncMock) -> None:
    response = TestClient(main.app).post("/sql", data=QUERY)
    assert response.status_code == 401
    ontop.post.assert_not_awaited()


def test_ontop_error_keeps_its_status(ontop: AsyncMock) -> None:
    ontop.post.return_value = MagicMock(
        status_code=400, text="Ontop could not parse the query"
    )
    response = TestClient(main.app).post("/sql", headers=SIGNED_IN, data=QUERY)
    assert response.status_code == 400
    assert response.text == "Ontop could not parse the query"


def test_query_ontop_proves_empty_is_422(ontop: AsyncMock) -> None:
    ontop.post.return_value = MagicMock(
        status_code=200, text="ans1(c)\n   EMPTY [c]\n"
    )
    response = TestClient(main.app).post("/sql", headers=SIGNED_IN, data=QUERY)
    assert response.status_code == 422
