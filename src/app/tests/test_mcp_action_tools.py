"""Tests for MCP VKG action tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from actions.models import ConfirmActionResponse
from actions.service import ActionValidationError
from config import Settings
from mcp_server import (
    McpRuntime,
    confirm_action,
    configure,
    describe_action,
    list_actions,
    prepare_action,
)
from ontology_store import OntologyStore


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


def _runtime_with_action_service() -> McpRuntime:
    service = MagicMock()
    service.list_actions.return_value = {
        "available": True,
        "actions": [],
        "properties": [],
    }
    service.describe_action.return_value = {"iri": "https://example.com/action"}
    return McpRuntime(
        ontology_store=OntologyStore(),
        ontop_manager=MagicMock(),
        settings=_settings(),
        http_client=MagicMock(spec=httpx.AsyncClient),
        action_service=service,
    )


def test_list_and_describe_action_tools_delegate_to_service() -> None:
    runtime = _runtime_with_action_service()
    configure(runtime)

    listed = list_actions(
        class_iri="https://example.com/Supplier",
        subject_iri="https://example.com/Supplier/1",
        property_iri="https://example.com/name",
        kind="WRITE_BACK",
    )
    described = describe_action("https://example.com/action")

    assert listed["available"] is True
    assert described == {"iri": "https://example.com/action"}
    runtime.action_service.list_actions.assert_called_once_with(
        class_iri="https://example.com/Supplier",
        subject_iri="https://example.com/Supplier/1",
        property_iri="https://example.com/name",
        kind="WRITE_BACK",
    )
    runtime.action_service.describe_action.assert_called_once_with(
        "https://example.com/action"
    )


def test_list_actions_returns_read_only_response_when_actions_are_unavailable() -> None:
    runtime = _runtime_with_action_service()
    runtime.action_service = None
    configure(runtime)

    assert list_actions() == {"available": False, "actions": [], "properties": []}


def test_prepare_action_tool_uses_mcp_user_token(monkeypatch) -> None:
    runtime = _runtime_with_action_service()
    configure(runtime)
    observed = {}

    async def prepare(request, token):
        observed["token"] = token
        return {"prepare_id": "prepare_1", "preparation_token": "signed", "preview": {}}

    runtime.action_service.prepare = prepare
    monkeypatch.setattr("mcp_server.get_mcp_user_token", lambda: "mcp-user-token")

    result = asyncio.run(
        prepare_action(
            "https://example.com/action",
            "https://example.com/Supplier/1",
            {"newValue": "new"},
        )
    )

    assert observed["token"] == "mcp-user-token"
    assert result["prepare_id"] == "prepare_1"


def test_confirm_action_tool_uses_mcp_user_token(monkeypatch) -> None:
    runtime = _runtime_with_action_service()
    configure(runtime)
    observed = {}

    async def confirm(request, token):
        observed["token"] = token
        return ConfirmActionResponse(
            action_iri="https://example.com/action",
            status="COMPLETED",
            audit_ids=["audit_1"],
            result={},
        )

    runtime.action_service.confirm = confirm
    monkeypatch.setattr("mcp_server.get_mcp_user_token", lambda: "mcp-user-token")

    result = asyncio.run(confirm_action("signed"))

    assert observed["token"] == "mcp-user-token"
    assert result["status"] == "COMPLETED"


def test_prepare_action_maps_typed_errors_without_sql(monkeypatch) -> None:
    runtime = _runtime_with_action_service()
    configure(runtime)

    async def prepare(request, token):
        raise ActionValidationError("invalid parameter")

    runtime.action_service.prepare = prepare
    monkeypatch.setattr("mcp_server.get_mcp_user_token", lambda: "mcp-user-token")

    result = asyncio.run(
        prepare_action(
            "https://example.com/action", "https://example.com/Supplier/1", {}
        )
    )

    assert result == "Error (400): invalid parameter"
