"""Tests for runtime action settings and read-only action catalog loading."""

from __future__ import annotations

from actions.catalog import ActionCatalog
from config import Settings


def test_settings_reads_action_defaults(monkeypatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh")
    monkeypatch.setenv("MAPPINGS_VOLUME_PATH", "/Volumes/test/mappings")
    monkeypatch.setenv("FM_MODEL_NAME", "model")
    monkeypatch.setenv("VKG_DEFAULT_CATALOG", "catalog")
    monkeypatch.setenv("VKG_DEFAULT_SCHEMA", "schema")
    monkeypatch.delenv("VKG_ACTIONS_FILE", raising=False)
    monkeypatch.delenv("VKG_ACTION_AUDIT_TABLE", raising=False)
    monkeypatch.delenv("VKG_ACTION_CONFIRM_SIGNING_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.actions_file == "actions.ttl"
    assert settings.action_audit_table is None
    assert settings.action_confirm_signing_key is None
    assert settings.action_prepare_ttl_seconds == 600


def test_settings_reads_action_env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh")
    monkeypatch.setenv("MAPPINGS_VOLUME_PATH", "/Volumes/test/mappings")
    monkeypatch.setenv("FM_MODEL_NAME", "model")
    monkeypatch.setenv("VKG_DEFAULT_CATALOG", "catalog")
    monkeypatch.setenv("VKG_DEFAULT_SCHEMA", "schema")
    monkeypatch.setenv("VKG_ACTIONS_FILE", "department-actions.ttl")
    monkeypatch.setenv("VKG_ACTION_AUDIT_TABLE", "catalog.schema.audit")
    monkeypatch.setenv("VKG_ACTION_CONFIRM_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("VKG_ACTION_PREPARE_TTL_SECONDS", "42")

    settings = Settings.from_env()

    assert settings.actions_file == "department-actions.ttl"
    assert settings.action_audit_table == "catalog.schema.audit"
    assert settings.action_confirm_signing_key == "test-signing-key"
    assert settings.action_prepare_ttl_seconds == 42


def test_action_catalog_missing_file_is_read_only(tmp_path):
    catalog = ActionCatalog.load(
        actions_path=tmp_path / "missing.ttl",
        ontology_path=None,
        mapping_path=None,
    )

    assert catalog.available is False
    assert catalog.actions == []
    assert catalog.list_actions() == {
        "available": False,
        "actions": [],
        "properties": [],
    }


def test_action_catalog_loads_parseable_turtle(tmp_path):
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

    catalog = ActionCatalog.load(
        actions_path=actions_path, ontology_path=None, mapping_path=None
    )

    assert catalog.available is True
    assert [action.logical_key for action in catalog.actions] == ["updateName"]
