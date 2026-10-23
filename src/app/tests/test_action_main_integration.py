"""Integration coverage for action artifacts and application wiring."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import main
import ontop_manager as ontop_manager_module
from config import Settings
from ontop_manager import OntopProcessManager


class _ActionService:
    def list_actions(self, **_filters):
        return {
            "available": True,
            "actions": [{"iri": "https://example.com/actions/update-name"}],
            "properties": [],
        }


def test_main_exposes_action_catalog_over_rest() -> None:
    main.app.state.action_service = _ActionService()

    response = TestClient(main.app).get("/api/actions")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "actions": [{"iri": "https://example.com/actions/update-name"}],
        "properties": [],
    }


def _settings(work_dir: Path) -> Settings:
    return Settings(
        warehouse_id="wh",
        mappings_volume_path="/Volumes/test/mappings",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog="test_catalog",
        default_schema="test_schema",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=work_dir,
        fm_model_name="test-model",
        actions_file="actions.ttl",
    )


def _prepare_manager(monkeypatch, tmp_path: Path, *, actions_available: bool):
    manager = OntopProcessManager(_settings(tmp_path))
    launcher = tmp_path / "fake-ontop"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    java_home = tmp_path / "fake-java"
    downloaded = []

    monkeypatch.setattr(
        manager,
        "_find_ontop_artifact_names",
        lambda _client, _remote: ("ontop-protege-bundle-linux.tgz", None),
    )
    monkeypatch.setattr(manager, "_extract_archive", lambda _archive, _target: None)
    monkeypatch.setattr(manager, "_locate_java_home", lambda _root: java_home)
    monkeypatch.setattr(manager, "_locate_ontop_script", lambda _root: launcher)
    monkeypatch.setattr(
        manager, "_find_jdbc_artifact_name", lambda _client, _remote: "driver.jar"
    )
    monkeypatch.setattr(manager, "_install_jdbc_driver", lambda _path: None)

    def download(_client, remote: str, local: Path) -> None:
        downloaded.append(remote)
        if remote.endswith("/actions.ttl") and not actions_available:
            raise FileNotFoundError(remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("@prefix ex: <https://example.com/> .\n", encoding="utf-8")

    monkeypatch.setattr(ontop_manager_module, "download_volume_file", download)
    manager.prepare(MagicMock())
    return manager, downloaded


def test_prepare_downloads_optional_action_catalog(monkeypatch, tmp_path: Path) -> None:
    manager, downloaded = _prepare_manager(
        monkeypatch, tmp_path, actions_available=True
    )

    assert manager.actions_path is not None
    assert manager.actions_path.read_text(encoding="utf-8").startswith("@prefix")
    assert any(
        remote.endswith("/mappings/.internal/actions.ttl") for remote in downloaded
    )


def test_prepare_remains_read_only_when_action_catalog_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    manager, _downloaded = _prepare_manager(
        monkeypatch, tmp_path, actions_available=False
    )

    assert manager.actions_path is None


def test_lifespan_loads_actions_and_configures_execution(
    monkeypatch, tmp_path: Path
) -> None:
    actions_path = tmp_path / "actions.ttl"
    actions_path.write_text(
        """
        @prefix act: <https://databricks.com/ontology/vkg/actions#> .
        @prefix ont: <https://example.com/ontology#> .
        ont:updateName a act:WriteBackAction ;
            act:logicalKey "updateName" ;
            act:boundClass ont:Supplier ;
            act:targetProperty ont:supplierName ;
            act:status "PUBLISHED" .
        """,
        encoding="utf-8",
    )

    class _Manager:
        ontology_path = None
        mapping_path = None

        def __init__(self, action_catalog_path: Path) -> None:
            self.actions_path = action_catalog_path

        def prepare(self, _client) -> None:
            pass

        def write_jdbc_properties(self) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    settings = _settings(tmp_path)
    settings = Settings(
        **{
            **settings.__dict__,
            "action_audit_table": "catalog.schema.vkg_action_audit",
            "action_confirm_signing_key": "test-signing-key",
        }
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "ontop_manager", _Manager(actions_path))
    monkeypatch.setattr(main, "WorkspaceClient", lambda: object())

    async def exercise_lifespan() -> None:
        async with main.ontop_lifespan(main.app):
            assert main.app.state.action_catalog.available is True
            assert [
                action.logical_key for action in main.app.state.action_catalog.actions
            ] == ["updateName"]
            assert main.app.state.action_service.list_actions()["available"] is True

    asyncio.run(exercise_lifespan())


def test_health_reports_action_catalog_status(monkeypatch) -> None:
    manager = MagicMock()
    manager.is_running = True
    monkeypatch.setattr(main, "ontop_manager", manager)
    main.app.state.ontology_store = MagicMock()
    main.app.state.ontology_store.is_available.return_value = True
    upstream = MagicMock(status_code=200)
    main.app.state.http_client = AsyncMock()
    main.app.state.http_client.post.return_value = upstream
    main.app.state.action_catalog = MagicMock(
        available=True,
        actions=[MagicMock()],
    )

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json()["actions_loaded"] is True
    assert response.json()["action_count"] == 1
