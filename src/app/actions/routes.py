"""REST endpoints for governed VKG actions."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from actions.models import ConfirmActionRequest, PrepareActionRequest
from actions.service import ActionError, ActionNotFoundError
from obo import get_user_token


def _action_error_response(error: ActionError) -> JSONResponse:
    """Serialize public action errors without leaking execution details."""
    payload: dict[str, object] = {
        "error_code": error.error_code,
        "message": error.message,
        "reason_codes": list(error.reason_codes),
    }
    if error.audit_id is not None:
        payload["audit_id"] = error.audit_id
    return JSONResponse(status_code=error.status_code, content=payload)


def create_action_router() -> APIRouter:
    router = APIRouter()

    @router.get("")
    async def list_actions(
        request: Request,
        class_iri: str | None = None,
        subject_iri: str | None = None,
        property_iri: str | None = None,
        kind: str | None = None,
    ) -> dict[str, object]:
        return request.app.state.action_service.list_actions(
            class_iri=class_iri,
            subject_iri=subject_iri,
            property_iri=property_iri,
            kind=kind,
        )

    @router.get("/describe", response_model=None)
    async def describe_action(
        request: Request, action_iri: str
    ) -> dict[str, object] | JSONResponse:
        description = request.app.state.action_service.describe_action(action_iri)
        if description is None:
            return _action_error_response(ActionNotFoundError())
        return description

    @router.post("/prepare")
    async def prepare_action(
        request: Request, action_request: PrepareActionRequest
    ) -> object:
        try:
            return await request.app.state.action_service.prepare(
                action_request, get_user_token(request)
            )
        except ActionError as error:
            return _action_error_response(error)

    @router.post("/confirm")
    async def confirm_action(
        request: Request, action_request: ConfirmActionRequest
    ) -> object:
        try:
            return await request.app.state.action_service.confirm(
                action_request, get_user_token(request)
            )
        except ActionError as error:
            return _action_error_response(error)

    return router
