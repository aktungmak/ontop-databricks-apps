"""Prepare and confirm runtime service for governed VKG actions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
import threading
import time
from uuid import uuid4

from actions.audit import ActionAuditLogger
from actions.catalog import ActionCatalog
from actions.dbsql import (
    quote_fqn,
    quote_identifier,
    resolve_effective_user,
    run_user_sql,
)
from actions.models import (
    ActionAuditRow,
    ActionAuditRecord,
    ActionDefinition,
    ConfirmActionRequest,
    ConfirmActionResponse,
    PrepareActionRequest,
    PrepareActionResponse,
    PrepareTokenPayload,
    WriteBackTarget,
)
from actions.tokens import PrepareTokenSigner
from config import Settings

SqlRunner = Callable[
    [str, str, Settings, Mapping[str, object] | None], tuple[list[str], list[tuple]]
]
AuditRecorder = Callable[[ActionAuditRow, str, int | None], str]
AuditLookup = Callable[[str, str, int | None], ActionAuditRecord | None]
AuditHistoryLookup = Callable[[str, str, int | None], list[ActionAuditRecord]]
ActorResolver = Callable[[str, Settings, int | None], str]
SubjectChecker = Callable[[str, str, str, int], Awaitable[bool]]
_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


class ActionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        status_code: int,
        reason_codes: tuple[str, ...] = (),
        audit_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.reason_codes = reason_codes
        self.audit_id = audit_id


class ActionNotFoundError(ActionError):
    def __init__(self, message: str = "action not found") -> None:
        super().__init__(message, error_code="ACTION_NOT_FOUND", status_code=404)


class ActionReadOnlyError(ActionError):
    def __init__(self, message: str, reason_codes: tuple[str, ...]) -> None:
        super().__init__(
            message,
            error_code="ACTION_READ_ONLY",
            status_code=422,
            reason_codes=reason_codes,
        )


class ActionConflictError(ActionError):
    def __init__(self, message: str, audit_id: str | None = None) -> None:
        super().__init__(
            message, error_code="ACTION_CONFLICT", status_code=409, audit_id=audit_id
        )


class ActionValidationError(ActionError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message, error_code="ACTION_VALIDATION_FAILED", status_code=400
        )


class ActionAuthorizationError(ActionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="ACTION_FORBIDDEN", status_code=403)


class ActionUnavailableError(ActionError):
    def __init__(self, message: str, audit_id: str | None = None) -> None:
        super().__init__(
            message, error_code="ACTION_UNAVAILABLE", status_code=503, audit_id=audit_id
        )


@dataclass(frozen=True)
class _PreparedAction:
    prepare_id: str
    action: ActionDefinition
    request: PrepareActionRequest
    effective_user: str
    invocation_id: str
    request_hash: str
    preview: dict[str, object]
    old_value: object | None
    prepare_audit_id: str


class ActionService:
    def __init__(
        self,
        *,
        catalog: ActionCatalog,
        settings: Settings,
        token_signer: PrepareTokenSigner,
        sql_runner: SqlRunner = run_user_sql,
        audit_logger: ActionAuditLogger | None = None,
        audit_recorder: AuditRecorder | None = None,
        audit_lookup: AuditLookup | None = None,
        audit_history_lookup: AuditHistoryLookup | None = None,
        actor_resolver: ActorResolver = resolve_effective_user,
        subject_checker: SubjectChecker | None = None,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._token_signer = token_signer
        self._sql_runner = sql_runner
        self._actor_resolver = actor_resolver
        self._subject_checker = subject_checker
        self._prepared: dict[str, _PreparedAction] = {}
        self._confirmed: dict[str, ConfirmActionResponse] = {}
        # The audit DDL has no unique claim key. This lock closes same-process
        # races; terminal-history reads close retries after a recorded outcome.
        # Separate app replicas can still race before either CONFIRMING row is visible.
        self._confirmation_locks: dict[str, threading.Lock] = {}
        self._confirmation_locks_guard = threading.Lock()
        if audit_recorder is not None:
            self._audit_recorder = audit_recorder
        elif audit_logger is not None:
            self._audit_recorder = audit_logger.record
        else:
            audit_logger = ActionAuditLogger(settings, sql_runner)
            self._audit_recorder = audit_logger.record
        self._audit_lookup = audit_lookup or (
            audit_logger.get_prepared if audit_logger is not None else None
        )
        self._audit_history_lookup = audit_history_lookup or (
            audit_logger.get_confirmation_history if audit_logger is not None else None
        )

    def list_actions(
        self,
        *,
        class_iri: str | None = None,
        subject_iri: str | None = None,
        property_iri: str | None = None,
        kind: str | None = None,
    ) -> dict[str, object]:
        return self._catalog.list_actions(
            class_iri=class_iri,
            subject_iri=subject_iri,
            property_iri=property_iri,
            kind=kind,
        )

    def describe_action(self, action_iri: str) -> dict[str, object] | None:
        return self._catalog.describe_action(action_iri)

    async def prepare(
        self, request: PrepareActionRequest, token: str
    ) -> PrepareActionResponse:
        action = self._action(request.action_iri)
        effective_user = await asyncio.to_thread(
            self._resolve_actor,
            token,
            action.timeout_seconds,
        )
        if action.kind == "EXTERNAL":
            await self._check_external_subject(
                action,
                request.subject_iri,
                token,
                confirmation=False,
            )
        return await asyncio.to_thread(
            self._prepare_sync,
            request,
            token,
            action,
            effective_user,
        )

    def _prepare_sync(
        self,
        request: PrepareActionRequest,
        token: str,
        action: ActionDefinition,
        effective_user: str,
    ) -> PrepareActionResponse:
        invocation_id = f"v1:{uuid4().hex}"
        request_hash = _request_hash(request)
        prepare_id = _prepare_id(invocation_id)
        pending = _PreparedAction(
            prepare_id=prepare_id,
            action=action,
            request=request,
            effective_user=effective_user,
            invocation_id=invocation_id,
            request_hash=request_hash,
            preview={},
            old_value=None,
            prepare_audit_id="",
        )
        try:
            self._require_published(action)
            request = self._normalized_request(action, request)
            if action.kind == "EXTERNAL":
                self._validate_external_params(action, request.params)
            request_hash = _request_hash(request)
            invocation_id = _invocation_id(action, request, effective_user)
            prepare_id = _prepare_id(invocation_id)
            pending = _PreparedAction(
                prepare_id=prepare_id,
                action=action,
                request=request,
                effective_user=effective_user,
                invocation_id=invocation_id,
                request_hash=request_hash,
                preview={},
                old_value=None,
                prepare_audit_id="",
            )
            with self._confirmation_lock(prepare_id):
                existing = self._find_existing_prepared(
                    action,
                    request,
                    effective_user,
                    invocation_id,
                    token,
                )
                if existing is not None:
                    self._require_matching_retry(existing, pending)
                    return self._prepare_response(existing)

                if action.kind == "WRITE_BACK":
                    preview, old_value = self._prepare_writeback(action, request, token)
                else:
                    preview = {
                        "function_fqn": action.function_fqn or "",
                        "subject_iri": request.subject_iri,
                        "params": request.params,
                        "idempotency_key": request.idempotency_key,
                    }
                    old_value = None

                pending = _PreparedAction(
                    prepare_id=prepare_id,
                    action=action,
                    request=request,
                    effective_user=effective_user,
                    invocation_id=invocation_id,
                    request_hash=request_hash,
                    preview=preview,
                    old_value=old_value,
                    prepare_audit_id="",
                )
                audit_id = self._record(
                    self._audit_row("PREPARE", "PREPARED", action, pending, None),
                    token,
                )
                prepared = _PreparedAction(
                    prepare_id=prepare_id,
                    action=action,
                    request=request,
                    effective_user=effective_user,
                    invocation_id=invocation_id,
                    request_hash=request_hash,
                    preview=preview,
                    old_value=old_value,
                    prepare_audit_id=audit_id,
                )
                self._prepared[prepare_id] = prepared
                return self._prepare_response(prepared)
        except ActionError as exc:
            self._record(
                self._audit_row(
                    "PREPARE", "FAILED", action, pending, None, exc.message
                ),
                token,
            )
            raise

    def _prepare_response(self, prepared: _PreparedAction) -> PrepareActionResponse:
        issued_at = int(time.time())
        ttl_seconds = (
            self._settings.action_prepare_ttl_seconds or self._token_signer.ttl_seconds
        )
        expires_at = issued_at + ttl_seconds
        payload = PrepareTokenPayload(
            prepare_id=prepared.prepare_id,
            action_iri=prepared.action.iri,
            action_kind=prepared.action.kind,
            subject_iri=prepared.request.subject_iri,
            effective_user=prepared.effective_user,
            invocation_id=prepared.invocation_id,
            request_hash=prepared.request_hash,
            params_hash=_hash(prepared.request.params),
            preview_hash=_hash(prepared.preview),
            old_value_hash=(
                _hash(prepared.old_value)
                if prepared.action.kind == "WRITE_BACK"
                else ""
            ),
            issued_at=issued_at,
            expires_at=expires_at,
            catalog_fingerprint=self._catalog_fingerprint(),
        )
        return PrepareActionResponse(
            prepare_id=prepared.prepare_id,
            action_iri=prepared.action.iri,
            action_kind=prepared.action.kind,
            subject_iri=prepared.request.subject_iri,
            preview=prepared.preview,
            expires_at=datetime.fromtimestamp(expires_at, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            preparation_token=self._token_signer.sign(payload),
        )

    def _normalized_request(
        self,
        action: ActionDefinition,
        request: PrepareActionRequest,
    ) -> PrepareActionRequest:
        if action.kind == "EXTERNAL" and action.idempotency_strategy != "REQUEST_KEY":
            raise ActionReadOnlyError(
                "external action has no supported durable idempotency strategy",
                ("UNSUPPORTED_IDEMPOTENCY_STRATEGY",),
            )
        if action.idempotency_strategy != "REQUEST_KEY":
            return request
        if not (
            isinstance(request.idempotency_key, str) and request.idempotency_key.strip()
        ):
            raise ActionValidationError(
                "idempotency_key is required for REQUEST_KEY actions"
            )
        return request.model_copy(
            update={"idempotency_key": request.idempotency_key.strip()}
        )

    def _find_existing_prepared(
        self,
        action: ActionDefinition,
        request: PrepareActionRequest,
        effective_user: str,
        invocation_id: str,
        token: str,
    ) -> _PreparedAction | None:
        if action.idempotency_strategy != "REQUEST_KEY":
            return None
        existing = self._prepared.get(_prepare_id(invocation_id))
        if existing is not None or self._audit_lookup is None:
            return existing
        try:
            record = self._audit_lookup(
                _prepare_id(invocation_id),
                token,
                action.timeout_seconds,
            )
        except Exception as exc:
            raise ActionUnavailableError(
                "could not read prepared action audit"
            ) from exc
        if record is None:
            return None
        existing = self._prepared_from_record(
            action,
            record,
            invocation_id=invocation_id,
        )
        if existing.effective_user != effective_user:
            raise ActionAuthorizationError(
                "prepared action belongs to a different preparing user"
            )
        return existing

    def _require_matching_retry(
        self,
        existing: _PreparedAction,
        requested: _PreparedAction,
    ) -> None:
        matches = (
            existing.prepare_id == requested.prepare_id
            and existing.action.iri == requested.action.iri
            and existing.action.kind == requested.action.kind
            and existing.effective_user == requested.effective_user
            and existing.invocation_id == requested.invocation_id
            and existing.request_hash == requested.request_hash
            and existing.request.subject_iri == requested.request.subject_iri
            and existing.request.idempotency_key == requested.request.idempotency_key
            and _hash(existing.request.params) == _hash(requested.request.params)
        )
        if existing.action.kind == "EXTERNAL":
            matches = matches and (
                existing.preview.get("function_fqn") == requested.action.function_fqn
            )
        if not matches:
            raise ActionConflictError(
                "idempotency key was already used with a different request"
            )

    async def confirm(
        self, request: ConfirmActionRequest, token: str
    ) -> ConfirmActionResponse:
        try:
            payload = self._token_signer.verify(request.preparation_token)
        except ValueError as exc:
            raise ActionValidationError("invalid or expired preparation token") from exc
        action = self._action(payload.action_iri)
        effective_user = await asyncio.to_thread(
            self._resolve_actor,
            token,
            action.timeout_seconds,
        )
        if effective_user != payload.effective_user:
            error = ActionAuthorizationError(
                "preparation token belongs to a different preparing user"
            )
            await asyncio.to_thread(
                self._record_confirm_refusal,
                action,
                payload,
                None,
                error,
                token,
            )
            raise error
        if action.kind == "EXTERNAL":
            try:
                await self._check_external_subject(
                    action,
                    payload.subject_iri,
                    token,
                    confirmation=True,
                )
            except ActionError as error:
                await asyncio.to_thread(
                    self._record_confirm_refusal,
                    action,
                    payload,
                    None,
                    error,
                    token,
                )
                raise
        return await asyncio.to_thread(
            self._confirm_sync,
            action,
            payload,
            token,
        )

    def _confirm_sync(
        self,
        action: ActionDefinition,
        payload: PrepareTokenPayload,
        token: str,
    ) -> ConfirmActionResponse:
        try:
            self._require_published(action)
        except ActionError as exc:
            self._record_confirm_refusal(action, payload, None, exc, token)
            raise
        with self._confirmation_lock(payload.prepare_id):
            return self._confirm_locked(action, payload, token)

    def _confirm_locked(
        self, action: ActionDefinition, payload: PrepareTokenPayload, token: str
    ) -> ConfirmActionResponse:
        if action.kind != payload.action_kind:
            error = ActionConflictError("action definition changed since prepare")
            self._record_confirm_refusal(action, payload, None, error, token)
            raise error
        prepared = self._prepared.get(payload.prepare_id)
        if prepared is None:
            try:
                prepared = self._load_prepared(payload, action, token)
            except ActionError as exc:
                self._record_confirm_refusal(action, payload, None, exc, token)
                raise
        if not self._matches(payload, prepared):
            error = ActionConflictError(
                "persisted preparation does not match signed token"
            )
            self._record_confirm_refusal(action, payload, prepared, error, token)
            raise error

        replayed = self._replayed_confirmation(action, prepared, payload, token)
        if replayed is not None:
            return replayed
        if self._catalog_fingerprint() != payload.catalog_fingerprint:
            error = ActionConflictError("action definition changed since prepare")
            self._record_confirm_refusal(action, payload, prepared, error, token)
            raise error

        if action.kind == "WRITE_BACK":
            result, audit_ids = self._confirm_writeback(
                action, prepared, payload, token
            )
            response = ConfirmActionResponse(
                action_iri=action.iri,
                status="COMPLETED",
                audit_ids=audit_ids,
                result=result,
            )
        else:
            status, result, audit_ids = self._confirm_external(
                action, prepared, payload, token
            )
            response = ConfirmActionResponse(
                action_iri=action.iri,
                status=status,
                audit_ids=audit_ids,
                result=result,
            )
        self._confirmed[payload.prepare_id] = response
        return response

    def _prepare_writeback(
        self, action: ActionDefinition, request: PrepareActionRequest, token: str
    ) -> tuple[dict[str, object], object | None]:
        target = self._writeback_target(action)
        key_value = _subject_key(target, request.subject_iri)
        new_value = _validate_writeback_value(
            request.params.get("newValue"), target.datatype_iri
        )
        old_value = self._read_old_value(
            target,
            key_value,
            token,
            action.timeout_seconds,
        )
        return {
            "subject_iri": request.subject_iri,
            "property_iri": target.property_iri,
            "old_value": old_value,
            "new_value": new_value,
            "source": {
                "table": ".".join(target.table_fqn),
                "key_column": target.key_column,
                "value_column": target.value_column,
            },
        }, old_value

    def _confirm_writeback(
        self,
        action: ActionDefinition,
        prepared: _PreparedAction,
        payload: PrepareTokenPayload,
        token: str,
    ) -> tuple[dict[str, object], list[str]]:
        try:
            target = self._writeback_target(action)
            key_value = _subject_key(target, prepared.request.subject_iri)
            current_value = self._read_old_value(
                target,
                key_value,
                token,
                action.timeout_seconds,
            )
        except ActionError as exc:
            self._record_confirm_refusal(action, payload, prepared, exc, token)
            raise
        if (
            _hash(current_value) != payload.old_value_hash
            or current_value != prepared.old_value
        ):
            error = ActionConflictError(
                "source value changed since prepare", prepared.prepare_audit_id
            )
            self._record_confirm_refusal(action, payload, prepared, error, token)
            raise error
        confirming_id = self._record(
            self._audit_row("CONFIRM", "CONFIRMING", action, prepared, payload), token
        )
        new_value = prepared.preview["new_value"]
        statement = (
            f"UPDATE {quote_fqn(target.table_fqn)} SET {quote_identifier(target.value_column)} = :new_value "
            f"WHERE {quote_identifier(target.key_column)} = :key_value "
            f"AND ({quote_identifier(target.value_column)} <=> :old_value)"
        )
        try:
            columns, rows = self._sql_runner(
                statement,
                token,
                self._settings,
                {
                    "new_value": new_value,
                    "key_value": key_value,
                    "old_value": prepared.old_value,
                },
                timeout_seconds=action.timeout_seconds,
            )
        except Exception as exc:
            self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    "action execution failed",
                ),
                token,
            )
            raise ActionUnavailableError("action execution failed") from exc
        affected_rows = _affected_rows(columns, rows)
        if affected_rows is None:
            self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    "could not verify affected rows",
                ),
                token,
            )
            raise ActionUnavailableError(
                "could not verify action execution", confirming_id
            )
        if affected_rows != 1:
            self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    "guarded update did not affect exactly one row",
                ),
                token,
            )
            raise ActionConflictError(
                "source value changed during update", confirming_id
            )
        result = {"rows_affected": affected_rows}
        try:
            completed_id = self._record(
                self._audit_row(
                    "CONFIRM", "COMPLETED", action, prepared, payload, result=result
                ),
                token,
            )
        except ActionUnavailableError as exc:
            raise ActionUnavailableError(
                "action completed but final audit could not be written", confirming_id
            ) from exc
        return result, [prepared.prepare_audit_id, confirming_id, completed_id]

    def _confirm_external(
        self,
        action: ActionDefinition,
        prepared: _PreparedAction,
        payload: PrepareTokenPayload,
        token: str,
    ) -> tuple[str, dict[str, object], list[str]]:
        if not action.function_fqn:
            raise ActionReadOnlyError(
                "external action has no UC function", ("MISSING_FUNCTION",)
            )
        confirming_id = self._record(
            self._audit_row("CONFIRM", "CONFIRMING", action, prepared, payload),
            token,
        )
        try:
            columns, rows = self._sql_runner(
                f"SELECT {quote_fqn(tuple(action.function_fqn.split('.')))}(:object_uid, :params_json, :invocation_id, :request_hash) AS result",
                token,
                self._settings,
                {
                    "object_uid": prepared.request.subject_iri,
                    "params_json": json.dumps(prepared.request.params, sort_keys=True),
                    "invocation_id": prepared.invocation_id,
                    "request_hash": prepared.request_hash,
                },
                timeout_seconds=action.timeout_seconds,
            )
        except Exception as exc:
            self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    "action execution failed",
                ),
                token,
            )
            raise ActionUnavailableError("action execution failed") from exc
        result = _result(columns, rows)
        if (
            result.get("invocation_id") != prepared.invocation_id
            or result.get("request_hash") != prepared.request_hash
        ):
            failed_id = self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    "external function returned an invalid idempotency envelope",
                    result,
                ),
                token,
            )
            raise ActionUnavailableError(
                "external function returned an invalid idempotency envelope",
                failed_id,
            )
        envelope_status = result.get("status")
        normalized_status = (
            envelope_status.upper() if isinstance(envelope_status, str) else None
        )
        if normalized_status == "CONFLICT":
            failed_id = self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    _string_or_none(result.get("message"))
                    or "external action idempotency conflict",
                    result,
                ),
                token,
            )
            raise ActionConflictError(
                "external action idempotency conflict",
                failed_id,
            )
        if normalized_status in {"FAILED", "ERROR"}:
            failed_id = self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    _string_or_none(result.get("message")) or "external action failed",
                    result,
                ),
                token,
            )
            return (
                "FAILED",
                result,
                [
                    prepared.prepare_audit_id,
                    confirming_id,
                    failed_id,
                ],
            )
        if normalized_status not in {"COMPLETED", "SUCCESS"}:
            failed_id = self._record(
                self._audit_row(
                    "CONFIRM",
                    "FAILED",
                    action,
                    prepared,
                    payload,
                    "external function returned an invalid result envelope",
                    result,
                ),
                token,
            )
            raise ActionUnavailableError(
                "external function returned an invalid result envelope", failed_id
            )
        try:
            completed_id = self._record(
                self._audit_row(
                    "CONFIRM", "COMPLETED", action, prepared, payload, result=result
                ),
                token,
            )
        except ActionUnavailableError as exc:
            raise ActionUnavailableError(
                "action completed but final audit could not be written", confirming_id
            ) from exc
        return (
            "COMPLETED",
            result,
            [prepared.prepare_audit_id, confirming_id, completed_id],
        )

    def _resolve_actor(self, token: str, timeout_seconds: int) -> str:
        try:
            effective_user = self._actor_resolver(
                token,
                self._settings,
                timeout_seconds,
            )
        except ActionError:
            raise
        except Exception as exc:
            raise ActionUnavailableError("could not resolve effective user") from exc
        if not isinstance(effective_user, str) or not effective_user.strip():
            raise ActionUnavailableError("could not resolve effective user")
        return effective_user

    async def _check_external_subject(
        self,
        action: ActionDefinition,
        subject_iri: str,
        token: str,
        *,
        confirmation: bool,
    ) -> None:
        if not action.bound_class_iri:
            raise ActionReadOnlyError(
                "external action has no bound class",
                ("MISSING_BOUND_CLASS",),
            )
        if self._subject_checker is None:
            raise ActionUnavailableError("VKG subject validation is unavailable")
        try:
            matches = await self._subject_checker(
                subject_iri,
                action.bound_class_iri,
                token,
                action.timeout_seconds,
            )
        except ActionError:
            raise
        except ValueError as exc:
            raise ActionValidationError(
                "subject or bound class is not a safe absolute IRI"
            ) from exc
        except Exception as exc:
            raise ActionUnavailableError("VKG subject validation failed") from exc
        if matches:
            return
        if confirmation:
            raise ActionConflictError("subject is no longer in the action bound class")
        raise ActionValidationError("subject is not in the action bound class")

    def _action(self, action_iri: str) -> ActionDefinition:
        if not self._catalog.available:
            raise ActionUnavailableError("actions are unavailable")
        action = next(
            (item for item in self._catalog.actions if item.iri == action_iri), None
        )
        if action is None:
            raise ActionNotFoundError()
        return action

    def _require_published(self, action: ActionDefinition) -> None:
        if action.status != "PUBLISHED":
            raise ActionReadOnlyError(
                "action is not published", ("ACTION_NOT_PUBLISHED",)
            )

    def _confirmation_lock(self, prepare_id: str) -> threading.Lock:
        with self._confirmation_locks_guard:
            return self._confirmation_locks.setdefault(prepare_id, threading.Lock())

    def _replayed_confirmation(
        self,
        action: ActionDefinition,
        prepared: _PreparedAction,
        payload: PrepareTokenPayload,
        token: str,
    ) -> ConfirmActionResponse | None:
        cached = self._confirmed.get(payload.prepare_id)
        if cached is not None:
            return cached
        if self._audit_history_lookup is None:
            return None
        try:
            history = self._audit_history_lookup(
                payload.prepare_id,
                token,
                action.timeout_seconds,
            )
        except Exception as exc:
            raise ActionUnavailableError(
                "could not read confirmation audit history"
            ) from exc
        for record in history:
            row = record.row
            if (
                record.effective_user != payload.effective_user
                or prepared.effective_user != payload.effective_user
                or row.phase != "CONFIRM"
                or row.prepare_id != payload.prepare_id
                or row.action_iri != action.iri
                or row.action_kind != action.kind
                or row.subject_iri != prepared.request.subject_iri
                or row.params_hash != payload.params_hash
                or row.preview_hash != payload.preview_hash
                or (row.old_value_hash or "") != payload.old_value_hash
                or row.idempotency_key != prepared.request.idempotency_key
            ):
                raise ActionConflictError(
                    "confirmation audit does not match signed token"
                )
        terminal_index = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if history[index].row.status in {"COMPLETED", "FAILED"}
            ),
            None,
        )
        if terminal_index is None:
            confirming = next(
                (
                    record
                    for record in reversed(history)
                    if record.row.status == "CONFIRMING"
                ),
                None,
            )
            if confirming is not None:
                raise ActionConflictError(
                    "action confirmation is already in progress",
                    confirming.audit_id,
                )
            return None
        terminal = history[terminal_index]
        try:
            result = (
                _json_object(terminal.row.result_json)
                if terminal.row.result_json is not None
                else {"error": terminal.row.error_message or "action failed"}
            )
        except (TypeError, ValueError) as exc:
            raise ActionConflictError("confirmation audit result is invalid") from exc
        if action.kind == "EXTERNAL" and (
            result.get("invocation_id") != payload.invocation_id
            or result.get("request_hash") != payload.request_hash
        ):
            raise ActionConflictError(
                "confirmation audit idempotency envelope does not match signed token"
            )
        response = ConfirmActionResponse(
            action_iri=action.iri,
            status=terminal.row.status,
            audit_ids=[prepared.prepare_audit_id]
            + [record.audit_id for record in history[: terminal_index + 1]],
            result=result,
        )
        self._confirmed[payload.prepare_id] = response
        return response

    def _record_confirm_refusal(
        self,
        action: ActionDefinition,
        payload: PrepareTokenPayload,
        prepared: _PreparedAction | None,
        error: ActionError,
        token: str,
    ) -> str:
        if prepared is not None:
            row = self._audit_row(
                "CONFIRM", "FAILED", action, prepared, payload, error.message
            )
        else:
            row = ActionAuditRow(
                phase="CONFIRM",
                status="FAILED",
                action_iri=action.iri,
                action_kind=action.kind,
                subject_iri=payload.subject_iri,
                prepare_id=payload.prepare_id,
                params_hash=payload.params_hash,
                preview_hash=payload.preview_hash,
                old_value_hash=payload.old_value_hash or None,
                error_message=error.message,
            )
        audit_id = self._record(row, token)
        error.audit_id = audit_id
        return audit_id

    def _writeback_target(self, action: ActionDefinition) -> WriteBackTarget:
        classification = None
        if (
            self._catalog.writeback_catalog
            and action.bound_class_iri
            and action.target_property_iri
        ):
            classification = self._catalog.writeback_catalog.property_for(
                action.bound_class_iri, action.target_property_iri
            )
        if (
            classification is None
            or not classification.writable
            or classification.target is None
        ):
            raise ActionReadOnlyError(
                "write-back action is not writable",
                classification.reasons if classification else ("NO_R2RML_MAPPING",),
            )
        return classification.target

    def _read_old_value(
        self,
        target: WriteBackTarget,
        key_value: str,
        token: str,
        timeout_seconds: int,
    ) -> object | None:
        statement = f"SELECT {quote_identifier(target.value_column)} AS old_value FROM {quote_fqn(target.table_fqn)} WHERE {quote_identifier(target.key_column)} = :key_value"
        try:
            _, rows = self._sql_runner(
                statement,
                token,
                self._settings,
                {"key_value": key_value},
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            raise ActionUnavailableError("could not read action source") from exc
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ActionConflictError("source row is missing or ambiguous")
        return rows[0][0]

    def _record(self, row: ActionAuditRow, token: str) -> str:
        try:
            action = next(
                (item for item in self._catalog.actions if item.iri == row.action_iri),
                None,
            )
            timeout_seconds = action.timeout_seconds if action is not None else None
            return self._audit_recorder(row, token, timeout_seconds)
        except Exception as exc:
            raise ActionUnavailableError("could not record action audit event") from exc

    def _audit_row(
        self,
        phase: str,
        status: str,
        action: ActionDefinition,
        prepared: _PreparedAction,
        payload: PrepareTokenPayload | None,
        error_message: str | None = None,
        result: dict[str, object] | None = None,
    ) -> ActionAuditRow:
        source = prepared.preview.get("source")
        source_values = source if isinstance(source, dict) else {}
        return ActionAuditRow(
            phase=phase,
            status=status,
            action_iri=action.iri,
            action_kind=action.kind,
            subject_iri=prepared.request.subject_iri,
            prepare_id=payload.prepare_id if payload else prepared.prepare_id,
            params_hash=_hash(prepared.request.params),
            preview_hash=_hash(prepared.preview),
            old_value_hash=_hash(prepared.old_value)
            if action.kind == "WRITE_BACK"
            else None,
            idempotency_key=prepared.request.idempotency_key,
            source_table=_string_or_none(source_values.get("table")),
            source_key_column=_string_or_none(source_values.get("key_column")),
            source_value_column=_string_or_none(source_values.get("value_column")),
            old_value=prepared.old_value,
            new_value=prepared.preview.get("new_value"),
            params_json=_json(prepared.request.params),
            preview_json=_json(prepared.preview),
            result_json=_json(result) if result is not None else None,
            error_message=error_message,
        )

    def _load_prepared(
        self, payload: PrepareTokenPayload, action: ActionDefinition, token: str
    ) -> _PreparedAction:
        if self._audit_lookup is None:
            raise ActionUnavailableError("prepared action audit lookup is unavailable")
        try:
            record = self._audit_lookup(
                payload.prepare_id,
                token,
                action.timeout_seconds,
            )
        except Exception as exc:
            raise ActionUnavailableError(
                "could not read prepared action audit"
            ) from exc
        if record is None:
            raise ActionConflictError("prepared action audit is unavailable")
        row = record.row
        if (
            record.effective_user != payload.effective_user
            or row.phase != "PREPARE"
            or row.status != "PREPARED"
            or row.action_iri != payload.action_iri
            or row.action_kind != payload.action_kind
            or row.subject_iri != payload.subject_iri
        ):
            raise ActionConflictError(
                "prepared action audit does not match signed token"
            )
        return self._prepared_from_record(
            action,
            record,
            invocation_id=payload.invocation_id,
        )

    def _prepared_from_record(
        self,
        action: ActionDefinition,
        record: ActionAuditRecord,
        *,
        invocation_id: str,
    ) -> _PreparedAction:
        row = record.row
        if (
            row.phase != "PREPARE"
            or row.status != "PREPARED"
            or row.action_iri != action.iri
            or row.action_kind != action.kind
        ):
            raise ActionConflictError("prepared action audit is invalid")
        try:
            params = _json_object(row.params_json)
            preview = _json_object(row.preview_json)
            old_value = _preview_old_value(preview, action.kind)
            prepared_request = PrepareActionRequest(
                action_iri=row.action_iri,
                subject_iri=row.subject_iri,
                params=params,
                idempotency_key=row.idempotency_key,
            )
        except (TypeError, ValueError) as exc:
            raise ActionConflictError("prepared action audit is invalid") from exc
        return _PreparedAction(
            prepare_id=row.prepare_id or "",
            action=action,
            request=prepared_request,
            effective_user=record.effective_user,
            invocation_id=invocation_id,
            request_hash=_request_hash(prepared_request),
            preview=preview,
            old_value=old_value,
            prepare_audit_id=record.audit_id,
        )

    def _matches(self, payload: PrepareTokenPayload, prepared: _PreparedAction) -> bool:
        return (
            payload.prepare_id == prepared.prepare_id
            and payload.action_iri == prepared.action.iri
            and payload.subject_iri == prepared.request.subject_iri
            and payload.effective_user == prepared.effective_user
            and payload.invocation_id == prepared.invocation_id
            and (
                prepared.action.idempotency_strategy != "REQUEST_KEY"
                or payload.invocation_id
                == _invocation_id(
                    prepared.action,
                    prepared.request,
                    prepared.effective_user,
                )
            )
            and payload.request_hash == prepared.request_hash
            and payload.request_hash == _request_hash(prepared.request)
            and payload.params_hash == _hash(prepared.request.params)
            and payload.preview_hash == _hash(prepared.preview)
            and payload.old_value_hash
            == (
                _hash(prepared.old_value)
                if prepared.action.kind == "WRITE_BACK"
                else ""
            )
        )

    def _catalog_fingerprint(self) -> str:
        targets = []
        if self._catalog.writeback_catalog is not None:
            for action in self._catalog.actions:
                if action.kind != "WRITE_BACK":
                    continue
                classification = (
                    self._catalog.writeback_catalog.property_for(
                        action.bound_class_iri, action.target_property_iri
                    )
                    if action.bound_class_iri and action.target_property_iri
                    else None
                )
                targets.append(
                    {
                        "action_iri": action.iri,
                        "writable": classification.writable
                        if classification
                        else False,
                        "reasons": classification.reasons
                        if classification
                        else ("NO_R2RML_MAPPING",),
                        "target": asdict(classification.target)
                        if classification and classification.target
                        else None,
                    }
                )
        return _hash(
            {
                "actions": [
                    self._catalog._as_dict(action) for action in self._catalog.actions
                ],
                "writeback_targets": targets,
            }
        )

    def _validate_external_params(
        self, action: ActionDefinition, params: dict[str, object]
    ) -> None:
        if not action.function_fqn:
            raise ActionReadOnlyError(
                "external action has no UC function", ("MISSING_FUNCTION",)
            )
        undeclared = sorted(set(params) - set(action.input_schema))
        if undeclared:
            raise ActionValidationError(f"undeclared parameter: {undeclared[0]}")
        for name, datatype in action.input_schema.items():
            if name not in params:
                raise ActionValidationError(f"missing required parameter: {name}")
            _validate_writeback_value(params[name], datatype)


def _subject_key(target: WriteBackTarget, subject_iri: str) -> str:
    pattern = (
        "^"
        + re.escape(target.subject_template).replace(
            re.escape(_PLACEHOLDER.search(target.subject_template).group(0)), "(.+)"
        )
        + "$"
    )
    match = re.fullmatch(pattern, subject_iri)
    if match is None:
        raise ActionValidationError("subject IRI does not match the action target")
    return match.group(1)


def _validate_writeback_value(value: object, datatype: str | None) -> object:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        raise ActionValidationError("newValue must be a scalar literal")
    datatype = (datatype or "").rsplit("#", 1)[-1]
    if datatype in {"integer", "int", "long"} and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ActionValidationError("value must be an integer")
    if datatype in {"boolean", "bool"} and not isinstance(value, bool):
        raise ActionValidationError("value must be a boolean")
    if datatype == "string" and not isinstance(value, str):
        raise ActionValidationError("value must be a string")
    return value


def _invocation_id(
    action: ActionDefinition,
    request: PrepareActionRequest,
    effective_user: str,
) -> str:
    if action.idempotency_strategy != "REQUEST_KEY":
        return f"v1:{uuid4().hex}"
    if not isinstance(request.idempotency_key, str) or not request.idempotency_key:
        raise ActionValidationError(
            "idempotency_key is required for REQUEST_KEY actions"
        )
    material = "\0".join((effective_user, action.iri, request.idempotency_key)).encode(
        "utf-8"
    )
    return f"v1:{hashlib.sha256(material).hexdigest()}"


def _request_hash(request: PrepareActionRequest) -> str:
    return _hash(
        {
            "subject_iri": request.subject_iri,
            "params": request.params,
        }
    )


def _prepare_id(invocation_id: str) -> str:
    digest = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()
    return f"prepare_{digest}"


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_object(value: str | None) -> dict[str, object]:
    decoded = json.loads(value or "")
    if not isinstance(decoded, dict):
        raise ValueError("expected an object")
    return decoded


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _preview_old_value(preview: dict[str, object], action_kind: str) -> object | None:
    if action_kind != "WRITE_BACK":
        return None
    if "old_value" not in preview:
        raise ValueError("prepared preview has no old value")
    return preview["old_value"]


def _affected_rows(columns: list[str], rows: list[tuple]) -> int | None:
    try:
        index = [column.lower() for column in columns].index("num_affected_rows")
    except ValueError:
        return None
    if len(rows) != 1 or len(rows[0]) <= index:
        return None
    value = rows[0][index]
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _result(columns: list[str], rows: list[tuple]) -> dict[str, object]:
    if not rows:
        return {}
    value = rows[0][0] if rows[0] else None
    return (
        value
        if isinstance(value, dict)
        else {columns[0] if columns else "result": value}
    )
