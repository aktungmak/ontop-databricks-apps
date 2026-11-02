"""Autogenerate: how a model reply is checked."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from routes.autogenerate import _request_mapping

TURTLE = '@prefix rr: <http://www.w3.org/ns/r2rml#> .\n<#t> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "t" ] .'


def _fm_returning(
    output_text: str, status: str, reason: str | None = None
) -> MagicMock:
    """A Responses API client whose reply has this text and status."""
    response = SimpleNamespace(
        output_text=output_text,
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
    )
    fm = MagicMock()
    fm.responses.create.return_value = response
    return fm


def _request(fm: MagicMock) -> str:
    with patch("routes.autogenerate._fm_client", return_value=fm):
        return asyncio.run(_request_mapping("tok", "test-model", []))


def test_request_mapping_reports_a_cut_off_reply() -> None:
    with pytest.raises(ValueError, match="cut off \\(max_output_tokens"):
        _request(_fm_returning(TURTLE[:20], "incomplete", "max_output_tokens"))


def test_request_mapping_rejects_reasoning_written_into_the_answer() -> None:
    leaked = (
        TURTLE
        + "\n<reasoning>Orders come from the sales table.</reasoning>\n"
        + TURTLE
    )
    with pytest.raises(ValueError, match="not valid Turtle"):
        _request(_fm_returning(leaked, "completed"))


def test_request_mapping_accepts_the_standard_prefixes_undeclared() -> None:
    reply = TURTLE.replace(
        " ] .",
        ' ] ; rr:predicateObjectMap [ rr:predicate rdf:type ; rr:objectMap [ rr:template "x{a}" ] ] .',
    )
    assert _request(_fm_returning(reply, "completed")) == reply
