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
) -> tuple[list[str], list[tuple]]:
    """Execute parameterized SQL as the forwarded end user.

    Returns the result columns and rows exposed by DBSQL. Write-back callers require
    a single ``num_affected_rows`` result value before they can report DML success.
    """
    connection_options = {
        "server_hostname": get_workspace_host(),
        "http_path": settings.warehouse_http_path,
        "access_token": token,
        "catalog": settings.default_catalog,
        "schema": settings.default_schema,
    }
    with dbsql.connect(**connection_options) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, parameters or None)
            columns = (
                [desc[0] for desc in cursor.description] if cursor.description else []
            )
            rows = cursor.fetchall()
    return columns, rows


def quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier is not a simple Databricks identifier")
    return f"`{value}`"


def quote_fqn(parts: Sequence[str]) -> str:
    if len(parts) != 3:
        raise ValueError("expected a three-part name")
    return ".".join(quote_identifier(part) for part in parts)
