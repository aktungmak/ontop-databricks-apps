"""Tests for the REST VKG action API."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from actions.catalog import ActionCatalog
from actions.models import (
    ActionDefinition,
    ConfirmActionResponse,
    PrepareActionResponse,
    PropertyClassification,
    WriteBackTarget,
)
from actions.r2rml_classifier import WriteBackCatalog
from actions.routes import create_action_router
from actions.service import ActionConflictError, ActionService
from actions.tokens import PrepareTokenSigner
from config import Settings


class FakeActionService:
    def __init__(self) -> None:
        self.list_filters: dict[str, str | None] | None = None

    def list_actions(self, **filters: str | None) -> dict[str, object]:
        self.list_filters = filters
        return {
            "available": True,
            "actions": [{"iri": "https://example.com/action"}],
            "properties": [],
        }

    def describe_action(self, action_iri: str) -> dict[str, object] | None:
        if action_iri == "https://example.com/action":
            return {"iri": action_iri}
        return None


def _client() -> TestClient:
    app = FastAPI()
    app.state.action_service = FakeActionService()
    app.include_router(create_action_router(), prefix="/api/actions")
    return TestClient(app)


def test_prepare_token_signer_uses_action_settings() -> None:
    settings = Settings(
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
        action_confirm_signing_key="secret",
        action_prepare_ttl_seconds=42,
    )

    assert PrepareTokenSigner.from_settings(settings).ttl_seconds == 42


def test_list_and_describe_endpoints_return_action_catalog() -> None:
    client = _client()

    listed = client.get(
        "/api/actions",
        params={
            "class_iri": "https://example.com/Supplier",
            "subject_iri": "https://example.com/Supplier/1",
            "property_iri": "https://example.com/name",
            "kind": "WRITE_BACK",
        },
    )
    described = client.get(
        "/api/actions/describe", params={"action_iri": "https://example.com/action"}
    )

    assert listed.status_code == 200
    assert listed.json()["available"] is True
    assert client.app.state.action_service.list_filters == {
        "class_iri": "https://example.com/Supplier",
        "subject_iri": "https://example.com/Supplier/1",
        "property_iri": "https://example.com/name",
        "kind": "WRITE_BACK",
    }
    assert described.status_code == 200
    assert described.json() == {"iri": "https://example.com/action"}


def test_action_service_filters_catalog_actions_by_metadata() -> None:
    supplier_class = "https://example.com/Supplier"
    name_property = "https://example.com/name"
    legacy_property = "https://example.com/legacyName"
    external_action = "https://example.com/action/external"
    write_action = "https://example.com/action/write"
    legacy_action = "https://example.com/action/legacy"
    service = ActionService(
        catalog=ActionCatalog(
            available=True,
            actions=[
                ActionDefinition(
                    iri=write_action,
                    logical_key="write",
                    kind="WRITE_BACK",
                    bound_class_iri=supplier_class,
                    target_property_iri=name_property,
                ),
                ActionDefinition(
                    iri=external_action,
                    logical_key="external",
                    kind="EXTERNAL",
                    bound_class_iri=supplier_class,
                ),
                ActionDefinition(
                    iri=legacy_action,
                    logical_key="legacy",
                    kind="WRITE_BACK",
                    bound_class_iri=supplier_class,
                    target_property_iri=legacy_property,
                ),
                ActionDefinition(
                    iri="https://example.com/action/unmapped",
                    logical_key="unmapped",
                    kind="WRITE_BACK",
                    bound_class_iri="https://example.com/Unmapped",
                    target_property_iri="https://example.com/unmappedProperty",
                ),
            ],
            writeback_catalog=WriteBackCatalog(
                [
                    PropertyClassification(
                        class_iri=supplier_class,
                        property_iri=name_property,
                        writable=True,
                        reasons=(),
                        action_iri=write_action,
                        target=WriteBackTarget(
                            action_iri=write_action,
                            class_iri=supplier_class,
                            property_iri=name_property,
                            table_fqn=("catalog", "schema", "suppliers"),
                            key_column="supplier_id",
                            value_column="name",
                            key_alias="supplier_id",
                            value_alias="name",
                            subject_template="https://example.com/Supplier/{supplier_id}",
                            datatype_iri=None,
                        ),
                    ),
                    PropertyClassification(
                        class_iri=supplier_class,
                        property_iri=legacy_property,
                        writable=True,
                        reasons=(),
                        action_iri=legacy_action,
                        target=WriteBackTarget(
                            action_iri=legacy_action,
                            class_iri=supplier_class,
                            property_iri=legacy_property,
                            table_fqn=("catalog", "schema", "legacy_suppliers"),
                            key_column="supplier_id",
                            value_column="name",
                            key_alias="supplier_id",
                            value_alias="name",
                            subject_template="https://example.com/LegacySupplier/{supplier_id}",
                            datatype_iri=None,
                        ),
                    ),
                ]
            ),
        ),
        settings=Settings(
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
        ),
        token_signer=PrepareTokenSigner("secret"),
        audit_recorder=lambda _row, _token: "audit_1",
    )

    assert [
        action["iri"] for action in service.list_actions(kind="EXTERNAL")["actions"]
    ] == [external_action]
    assert [
        action["iri"]
        for action in service.list_actions(class_iri=supplier_class)["actions"]
    ] == [write_action, external_action, legacy_action]
    assert [
        action["iri"]
        for action in service.list_actions(property_iri=name_property)["actions"]
    ] == [write_action]
    matching_subject = service.list_actions(
        subject_iri="https://example.com/Supplier/1"
    )
    assert [action["iri"] for action in matching_subject["actions"]] == [write_action]
    assert [
        property_["property_iri"] for property_ in matching_subject["properties"]
    ] == [name_property]
    assert (
        service.list_actions(subject_iri="https://example.com/Other/1")["actions"] == []
    )
    assert (
        service.list_actions(subject_iri="https://example.com/Other/1")["properties"]
        == []
    )
    assert "https://example.com/action/unmapped" not in [
        action["iri"] for action in matching_subject["actions"]
    ]


def test_prepare_endpoint_uses_forwarded_token(monkeypatch) -> None:
    client = _client()
    observed = {}

    async def prepare(request, token):
        observed["token"] = token
        return PrepareActionResponse(
            prepare_id="prepare_1",
            action_iri=request.action_iri,
            action_kind="WRITE_BACK",
            subject_iri=request.subject_iri,
            preview={"old_value": "old", "new_value": "new"},
            expires_at="2026-09-12T12:00:00Z",
            preparation_token="signed",
        )

    monkeypatch.setattr(
        client.app.state.action_service, "prepare", prepare, raising=False
    )
    response = client.post(
        "/api/actions/prepare",
        headers={"x-forwarded-access-token": "Bearer user-token"},
        json={
            "action_iri": "https://example.com/action",
            "subject_iri": "https://example.com/Supplier/1",
            "params": {"newValue": "new"},
        },
    )

    assert response.status_code == 200
    assert observed["token"] == "user-token"
    assert response.json()["prepare_id"] == "prepare_1"


def test_confirm_endpoint_uses_forwarded_token(monkeypatch) -> None:
    client = _client()
    observed = {}

    async def confirm(request, token):
        observed["token"] = token
        return ConfirmActionResponse(
            action_iri="https://example.com/action",
            status="COMPLETED",
            audit_ids=["audit_1"],
            result={"updated": True},
        )

    monkeypatch.setattr(
        client.app.state.action_service, "confirm", confirm, raising=False
    )
    response = client.post(
        "/api/actions/confirm",
        headers={"x-forwarded-access-token": "Bearer user-token"},
        json={"preparation_token": "signed"},
    )

    assert response.status_code == 200
    assert observed["token"] == "user-token"
    assert response.json()["status"] == "COMPLETED"


def test_prepare_endpoint_maps_action_errors_without_sql(monkeypatch) -> None:
    client = _client()

    async def prepare(request, token):
        raise ActionConflictError("source value changed", audit_id="audit_1")

    monkeypatch.setattr(
        client.app.state.action_service, "prepare", prepare, raising=False
    )
    response = client.post(
        "/api/actions/prepare",
        headers={"x-forwarded-access-token": "user-token"},
        json={
            "action_iri": "https://example.com/action",
            "subject_iri": "https://example.com/Supplier/1",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "error_code": "ACTION_CONFLICT",
        "message": "source value changed",
        "reason_codes": [],
        "audit_id": "audit_1",
    }
