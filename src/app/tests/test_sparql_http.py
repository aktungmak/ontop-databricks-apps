"""HTTP /sparql adapter: permission denials return 403, not 500."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from databricks.sql.exc import ServerOperationError
from fastapi.testclient import TestClient

import main

TABLE_DENIAL = (
    "[INSUFFICIENT_PERMISSIONS] Insufficient privileges:\n"
    "User does not have SELECT on Table 'cat.sch.tbl'. SQLSTATE: 42501"
)

# IQ-tree reformulate response the core parses to native SQL before calling run_sql.
CONSTRUCT_REFORMULATE = (
    "ans1(c)\n"
    "   CONSTRUCT [c] [c/RDF(STRINGToSTRING(c),IRI)]\n"
    "      NATIVE [c]\n"
    "SELECT c FROM t\n"
)


def _reformulate_client() -> AsyncMock:
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.text = CONSTRUCT_REFORMULATE
    client = AsyncMock()
    client.post.return_value = upstream
    return client


def test_sparql_permission_denied_returns_403_body() -> None:
    manager = MagicMock()
    manager.is_running = True
    # The /sparql handler reads the module-level ontop_manager and app.state.http_client.
    main.ontop_manager = manager
    main.app.state.http_client = _reformulate_client()

    denial = ServerOperationError(
        TABLE_DENIAL,
        context={"operation-id": "z", "diagnostic-info": "org.apache.spark...HUGE"},
    )
    with patch("sparql_execute.run_sql", side_effect=denial):
        client = TestClient(main.app, raise_server_exceptions=False)
        response = client.post(
            "/sparql",
            headers={"x-forwarded-access-token": "tok"},
            data={"query": "SELECT ?c WHERE { ?c ?p ?o }"},
        )

    assert response.status_code == 403
    assert "You lack Unity Catalog access" in response.text
    assert "org.apache.spark" not in response.text
    assert response.text != "Internal Server Error"
