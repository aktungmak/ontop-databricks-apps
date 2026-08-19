"""ONTOP_JAVA_ARGS / JVM max-heap construction tests."""

from __future__ import annotations

from pathlib import Path

from config import Settings
from ontop_manager import (
    _ARROW_JAVA_OPENS,
    _DEFAULT_MAX_HEAP,
    _SPRING_ERROR_MESSAGE,
    OntopProcessManager,
)


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


def _java_args(monkeypatch, tmp_path: Path) -> str:
    monkeypatch.delenv("ONTOP_JAVA_ARGS", raising=False)
    return OntopProcessManager(_settings(tmp_path))._ontop_env()["ONTOP_JAVA_ARGS"]


def test_default_heap_applied(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ONTOP_MAX_HEAP", raising=False)
    args = _java_args(monkeypatch, tmp_path)
    assert f"-Xmx{_DEFAULT_MAX_HEAP}" in args
    assert _DEFAULT_MAX_HEAP == "2g"
    # The Arrow opens and Spring error-message flags are still passed through.
    assert _ARROW_JAVA_OPENS in args
    assert _SPRING_ERROR_MESSAGE in args


def test_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONTOP_MAX_HEAP", "4g")
    args = _java_args(monkeypatch, tmp_path)
    assert "-Xmx4g" in args
    assert "-Xmx2g" not in args


def test_blank_override_falls_back_to_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONTOP_MAX_HEAP", "   ")
    args = _java_args(monkeypatch, tmp_path)
    assert f"-Xmx{_DEFAULT_MAX_HEAP}" in args


def test_preexisting_xmx_is_respected(monkeypatch, tmp_path: Path) -> None:
    # A caller-supplied -Xmx must not be overridden or duplicated, mirroring how
    # Ontop's own launcher only defaults -Xmx when none is present.
    monkeypatch.setenv("ONTOP_MAX_HEAP", "2g")
    monkeypatch.setenv("ONTOP_JAVA_ARGS", "-Xmx8g -Dfoo=bar")
    args = OntopProcessManager(_settings(tmp_path))._ontop_env()["ONTOP_JAVA_ARGS"]
    assert "-Xmx8g" in args
    assert "-Xmx2g" not in args
    assert args.count("-Xmx") == 1
    assert "-Dfoo=bar" in args
