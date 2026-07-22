"""On-behalf-of WorkspaceClient from the user's forwarded access token."""

from __future__ import annotations

import logging
import os

from databricks.sdk import WorkspaceClient
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def get_workspace_host() -> str:
    """Return workspace URL with https:// scheme."""
    host = os.environ.get("DATABRICKS_HOST", "")
    if not host:
        raise RuntimeError("DATABRICKS_HOST is not set")
    return host


def get_user_token(request: Request) -> str:
    """Return the forwarded user OAuth access token or raise 401."""
    raw = request.headers.get("x-forwarded-access-token")
    if not raw:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing user authorization. Open this app in Databricks and "
                "approve the requested permissions (user_api_scopes)."
            ),
        )
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def get_obo_client_from_token(token: str) -> WorkspaceClient:
    """WorkspaceClient authenticated as the end user via forwarded OAuth token.

    Databricks Apps inject service-principal OAuth credentials (CLIENT_ID/SECRET)
    into the process environment. The SDK always loads those from env, so passing
    ``token=`` from ``x-forwarded-access-token`` without pinning auth triggers:
    "validate: more than one authorization method configured: oauth and pat".

    Setting ``auth_type="pat"`` selects bearer-token auth and ignores the SP
    OAuth env credentials. The forwarded user OAuth access token is sent as a
    standard ``Authorization: Bearer`` header on every API call.
    """
    return WorkspaceClient(
        host=get_workspace_host(),
        token=token,
        auth_type="pat",
    )


def get_obo_client(request: Request) -> WorkspaceClient:
    token = get_user_token(request)
    try:
        return get_obo_client_from_token(token)
    except ValueError as exc:
        logger.exception("Failed to create on-behalf-of WorkspaceClient")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to initialize Databricks client: {exc}",
        ) from exc
