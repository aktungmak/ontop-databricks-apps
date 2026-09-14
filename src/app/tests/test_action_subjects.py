from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from actions.subjects import SubjectCheckUnavailableError, VkgSubjectChecker
from config import Settings
from sparql_execute import SparqlExecuteError, SparqlExecuteSuccess


def _settings() -> Settings:
    return Settings(
        warehouse_id="wh",
        mappings_volume_path="/Volumes/test/mappings",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog="cat",
        default_schema="sch",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=Path("/tmp/ontop-vkg-test"),
        fm_model_name="test-model",
    )


def test_subject_checker_executes_bound_vkg_query_as_user():
    calls = []

    async def executor(
        query,
        token,
        settings,
        http_client,
        ontop_manager,
        statement_timeout_seconds=None,
    ):
        calls.append((query, token, statement_timeout_seconds))
        return SparqlExecuteSuccess(
            data={
                "head": {"vars": ["subject"]},
                "results": {
                    "bindings": [
                        {
                            "subject": {
                                "type": "uri",
                                "value": "https://example.com/ontology/Bundle/B1",
                            }
                        }
                    ]
                },
            }
        )

    checker = VkgSubjectChecker(
        settings=_settings(),
        http_client=MagicMock(spec=httpx.AsyncClient),
        ontop_manager=MagicMock(),
        executor=executor,
    )

    result = asyncio.run(
        checker(
            "https://example.com/ontology/Bundle/B1",
            "https://example.com/ontology#SourcingBundle",
            "user-token",
            19,
        )
    )

    assert result is True
    assert calls == [
        (
            "SELECT ?subject WHERE {\n"
            "  VALUES ?subject { <https://example.com/ontology/Bundle/B1> }\n"
            "  ?subject a <https://example.com/ontology#SourcingBundle> .\n"
            "}\n"
            "LIMIT 1",
            "user-token",
            19,
        )
    ]


def test_subject_checker_rejects_unsafe_iri_before_execution():
    async def unexpected_executor(*args, **kwargs):
        raise AssertionError("unsafe IRI must not reach the executor")

    checker = VkgSubjectChecker(
        settings=_settings(),
        http_client=MagicMock(spec=httpx.AsyncClient),
        ontop_manager=MagicMock(),
        executor=unexpected_executor,
    )

    with pytest.raises(ValueError, match="absolute IRI"):
        asyncio.run(
            checker(
                "https://example.com/Bundle/B1> } UNION { ?s ?p ?o",
                "https://example.com/ontology#SourcingBundle",
                "user-token",
                19,
            )
        )


def test_subject_checker_sanitizes_vkg_execution_failure():
    async def executor(*args, **kwargs):
        return SparqlExecuteError(
            message="warehouse failure mentioning SELECT secret FROM internal",
            status_code=502,
        )

    checker = VkgSubjectChecker(
        settings=_settings(),
        http_client=MagicMock(spec=httpx.AsyncClient),
        ontop_manager=MagicMock(),
        executor=executor,
    )

    with pytest.raises(SubjectCheckUnavailableError) as error:
        asyncio.run(
            checker(
                "https://example.com/ontology/Bundle/B1",
                "https://example.com/ontology#SourcingBundle",
                "user-token",
                19,
            )
        )

    assert str(error.value) == "VKG subject validation failed"
    assert "SELECT secret" not in str(error.value)
