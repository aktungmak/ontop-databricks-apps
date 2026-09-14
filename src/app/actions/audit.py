"""User-token audit writes for VKG action lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
import hashlib
import hmac
import json
from uuid import uuid4

from actions.dbsql import quote_fqn, run_user_sql
from actions.models import ActionAuditRecord, ActionAuditRow
from config import Settings

SqlRunner = Callable[
    [str, str, Settings, Mapping[str, object] | None], tuple[list[str], list[tuple]]
]


class ActionAuditConfigError(RuntimeError):
    """Action auditing was requested without an audit table configuration."""


class ActionAuditIntegrityError(RuntimeError):
    """Persisted audit evidence failed authenticity verification."""


class ActionAuditLogger:
    def __init__(
        self, settings: Settings, sql_runner: SqlRunner = run_user_sql
    ) -> None:
        if not settings.action_audit_table:
            raise ActionAuditConfigError(
                "VKG_ACTION_AUDIT_TABLE is required for action audit"
            )
        if not settings.action_confirm_signing_key:
            raise ActionAuditConfigError(
                "VKG_ACTION_CONFIRM_SIGNING_KEY is required for action audit integrity"
            )
        self._settings = settings
        self._sql_runner = sql_runner
        self._table = quote_fqn(tuple(settings.action_audit_table.split(".")))
        self._integrity_secret = settings.action_confirm_signing_key

    def record(
        self,
        row: ActionAuditRow,
        token: str,
        effective_user: str,
        timeout_seconds: int | None = None,
    ) -> str:
        audit_id = f"audit_{uuid4().hex}"
        integrity_tag = _integrity_tag(
            self._integrity_secret,
            audit_id,
            effective_user,
            row,
        )
        statement = f"""
            INSERT INTO {self._table} (
                audit_id, phase, status, action_iri, action_kind, subject_iri,
                prepare_id, params_hash, preview_hash, old_value_hash,
                idempotency_key, source_table, source_key_column, source_value_column,
                old_value, new_value, params_json, preview_json, result_json,
                error_message, effective_user, integrity_tag, created_at
            ) VALUES (
                :audit_id, :phase, :status, :action_iri, :action_kind, :subject_iri,
                :prepare_id, :params_hash, :preview_hash, :old_value_hash,
                :idempotency_key, :source_table, :source_key_column, :source_value_column,
                :old_value, :new_value, :params_json, :preview_json, :result_json,
                :error_message, session_user(), :integrity_tag, current_timestamp()
            )
        """
        self._sql_runner(
            statement,
            token,
            self._settings,
            {
                "audit_id": audit_id,
                **asdict(row),
                "effective_user": effective_user,
                "integrity_tag": integrity_tag,
            },
            timeout_seconds=timeout_seconds,
        )
        return audit_id

    def get_prepared(
        self,
        prepare_id: str,
        token: str,
        timeout_seconds: int | None = None,
    ) -> ActionAuditRecord | None:
        """Read the persisted preparation using the acting user's DBSQL token."""
        statement = f"""
            SELECT
                audit_id, phase, status, action_iri, action_kind, subject_iri,
                prepare_id, params_hash, preview_hash, old_value_hash,
                idempotency_key, source_table, source_key_column, source_value_column,
                old_value, new_value, params_json, preview_json, result_json,
                error_message, effective_user, integrity_tag
            FROM {self._table}
            WHERE prepare_id = :prepare_id
              AND phase = 'PREPARE'
              AND status = 'PREPARED'
              AND effective_user = session_user()
            ORDER BY created_at, audit_id
            LIMIT 1
        """
        columns, rows = self._sql_runner(
            statement,
            token,
            self._settings,
            {"prepare_id": prepare_id},
            timeout_seconds=timeout_seconds,
        )
        if not rows:
            return None
        return self._record(columns, rows[0])

    def get_confirmation_history(
        self,
        prepare_id: str,
        token: str,
        timeout_seconds: int | None = None,
    ) -> list[ActionAuditRecord]:
        """Read ordered confirmation state using the acting user's DBSQL token."""
        statement = f"""
            SELECT
                audit_id, phase, status, action_iri, action_kind, subject_iri,
                prepare_id, params_hash, preview_hash, old_value_hash,
                idempotency_key, source_table, source_key_column, source_value_column,
                old_value, new_value, params_json, preview_json, result_json,
                error_message, effective_user, integrity_tag
            FROM {self._table}
            WHERE prepare_id = :prepare_id
              AND phase = 'CONFIRM'
              AND effective_user = session_user()
            ORDER BY created_at, audit_id
        """
        columns, rows = self._sql_runner(
            statement,
            token,
            self._settings,
            {"prepare_id": prepare_id},
            timeout_seconds=timeout_seconds,
        )
        return [self._record(columns, row) for row in rows]

    def _record(self, columns: list[str], row: tuple) -> ActionAuditRecord:
        values = dict(zip(columns, row, strict=True))
        audit_id = str(values.pop("audit_id"))
        effective_user = str(values.pop("effective_user"))
        integrity_tag = values.pop("integrity_tag", None)
        audit_row = ActionAuditRow(**values)
        expected = _integrity_tag(
            self._integrity_secret,
            audit_id,
            effective_user,
            audit_row,
        )
        if not isinstance(integrity_tag, str) or not hmac.compare_digest(
            integrity_tag, expected
        ):
            raise ActionAuditIntegrityError(
                "action audit integrity verification failed"
            )
        return ActionAuditRecord(
            audit_id=audit_id,
            row=audit_row,
            effective_user=effective_user,
        )


def _integrity_tag(
    secret: str,
    audit_id: str,
    effective_user: str,
    row: ActionAuditRow,
) -> str:
    authenticated_row = asdict(row)
    # These two VARIANT columns are informational projections. Their canonical
    # values are already covered by preview_json/result_json, while connector
    # round-trips may change their Python wrapper types.
    authenticated_row["old_value"] = None
    authenticated_row["new_value"] = None
    payload = json.dumps(
        {
            "audit_id": audit_id,
            "effective_user": effective_user,
            "row": authenticated_row,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        b"ontop-vkg-action-audit:v1\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def _json_default(value: object) -> dict[str, str]:
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "value": str(value),
    }
