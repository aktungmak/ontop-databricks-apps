"""DBSQL primitives used by runtime VKG actions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from databricks import sql as dbsql

from config import Settings
from obo import get_workspace_host

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def run_user_sql(
    sql: str,
    token: str,
    settings: Settings,
    parameters: Mapping[str, object] | None = None,
    *,
    timeout_seconds: int | None = None,
) -> tuple[list[str], list[tuple]]:
    """Execute parameterized SQL as the forwarded end user.

    Returns the result columns and rows exposed by DBSQL. Write-back callers require
    a single ``num_affected_rows`` result value before they can report DML success.
    """
    timeout_statement = statement_timeout_sql(timeout_seconds)
    connection_options = {
        "server_hostname": get_workspace_host(),
        "http_path": settings.warehouse_http_path,
        "access_token": token,
        "catalog": settings.default_catalog,
        "schema": settings.default_schema,
    }
    with dbsql.connect(**connection_options) as conn:
        with conn.cursor() as cursor:
            if timeout_statement is not None:
                cursor.execute(timeout_statement)
            cursor.execute(sql, parameters or None)
            columns = (
                [desc[0] for desc in cursor.description] if cursor.description else []
            )
            rows = cursor.fetchall()
    return columns, rows


def statement_timeout_sql(timeout_seconds: int | None) -> str | None:
    """Return a validated session-level statement timeout command."""
    if timeout_seconds is None:
        return None
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive integer")
    return f"SET STATEMENT_TIMEOUT = {timeout_seconds}"


def resolve_effective_user(
    token: str,
    settings: Settings,
    timeout_seconds: int | None = None,
) -> str:
    """Resolve the Databricks principal represented by a forwarded token."""
    columns, rows = run_user_sql(
        "SELECT session_user() AS effective_user",
        token,
        settings,
        timeout_seconds=timeout_seconds,
    )
    if [column.casefold() for column in columns] != ["effective_user"]:
        raise RuntimeError("effective user query returned an invalid schema")
    if len(rows) != 1 or len(rows[0]) != 1:
        raise RuntimeError("effective user query returned an invalid result")
    effective_user = rows[0][0]
    if not isinstance(effective_user, str) or not effective_user.strip():
        raise RuntimeError("effective user query returned an invalid principal")
    return effective_user


def quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier is not a simple Databricks identifier")
    return f"`{value}`"


def quote_fqn(parts: Sequence[str]) -> str:
    if len(parts) != 3:
        raise ValueError("expected a three-part name")
    return ".".join(quote_identifier(part) for part in parts)
