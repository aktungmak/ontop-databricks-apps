"""Connection default catalog and schema tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import Settings
from ontop_manager import OntopProcessManager
from sparql_execute import run_sql


def _settings(work_dir: Path) -> Settings:
    return Settings(
        warehouse_id="wh",
        mappings_volume_path="/Volumes/test/mappings",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog="catalog_name",
        default_schema="schema_name",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=work_dir,
        fm_model_name="test-model",
    )


def test_jdbc_url_includes_connection_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        OntopProcessManager,
        "_workspace_host",
        staticmethod(lambda: "test.cloud.databricks.com"),
    )

    manager = OntopProcessManager(_settings(tmp_path))
    manager.write_jdbc_properties()

    content = manager.properties_path.read_text()
    assert (
        "OAuth2Secret=client-secret;"
        "ConnCatalog=catalog_name;ConnSchema=schema_name"
    ) in content


@pytest.mark.parametrize(
    ("catalog", "schema"),
    [
        (None, "schema_name"),
        ("catalog_name", None),
        ("", "schema_name"),
        ("catalog_name", ""),
        (" \t", "schema_name"),
        ("catalog_name", "\n"),
    ],
)
def test_startup_rejects_missing_connection_defaults(
    monkeypatch, catalog: str | None, schema: str | None
) -> None:
    for name, value in (
        ("VKG_DEFAULT_CATALOG", catalog),
        ("VKG_DEFAULT_SCHEMA", schema),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(
        RuntimeError,
        match="VKG_DEFAULT_CATALOG and VKG_DEFAULT_SCHEMA are required",
    ):
        Settings.from_env()


def test_run_sql_passes_connection_defaults(monkeypatch, tmp_path: Path) -> None:
    connect = MagicMock()
    connection = connect.return_value.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("id",)]
    cursor.fetchall.return_value = [(1,)]

    with patch("databricks.sql.connect", connect):
        assert run_sql("SELECT 1", "token", _settings(tmp_path)) == (
            ["id"],
            [(1,)],
        )

    connect.assert_called_once_with(
        server_hostname="https://test.cloud.databricks.com",
        http_path="/sql/1.0/warehouses/wh",
        access_token="token",
        catalog="catalog_name",
        schema="schema_name",
    )
