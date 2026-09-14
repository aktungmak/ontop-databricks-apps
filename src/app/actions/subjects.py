"""VKG-backed subject/class preconditions for external actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import re
from urllib.parse import urlsplit

import httpx

from config import Settings
from ontop_manager import OntopProcessManager
from sparql_execute import (
    SparqlExecuteError,
    SparqlExecuteResult,
    execute_sparql_query,
)

SubjectQueryExecutor = Callable[
    [str, str, Settings, httpx.AsyncClient, OntopProcessManager, int | None],
    Awaitable[SparqlExecuteResult],
]
_INVALID_IRI_CHARACTER = re.compile(r'[\x00-\x20\x7f<>"{}|^`\\]')


class SubjectCheckUnavailableError(RuntimeError):
    """The active VKG could not validate an action subject."""


class VkgSubjectChecker:
    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient,
        ontop_manager: OntopProcessManager,
        executor: SubjectQueryExecutor = execute_sparql_query,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._ontop_manager = ontop_manager
        self._executor = executor

    async def __call__(
        self,
        subject_iri: str,
        class_iri: str,
        token: str,
        timeout_seconds: int,
    ) -> bool:
        subject = _sparql_iri(subject_iri)
        bound_class = _sparql_iri(class_iri)
        query = (
            "SELECT ?subject WHERE {\n"
            f"  VALUES ?subject {{ {subject} }}\n"
            f"  ?subject a {bound_class} .\n"
            "}\n"
            "LIMIT 1"
        )
        result = await self._executor(
            query,
            token,
            self._settings,
            self._http_client,
            self._ontop_manager,
            statement_timeout_seconds=timeout_seconds,
        )
        if isinstance(result, SparqlExecuteError):
            raise SubjectCheckUnavailableError("VKG subject validation failed")
        bindings = result.data.get("results", {}).get("bindings", [])
        return isinstance(bindings, list) and bool(bindings)


def _sparql_iri(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not urlsplit(value).scheme
        or _INVALID_IRI_CHARACTER.search(value)
    ):
        raise ValueError("subject and class must be safe absolute IRIs")
    return f"<{value}>"
