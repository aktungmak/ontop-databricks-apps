"""User-token audit writes for VKG action lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from uuid import uuid4

from actions.dbsql import quote_fqn, run_user_sql
from actions.models import ActionAuditRecord, ActionAuditRow
from config import Settings

SqlRunner = Callable[
    [str, str, Settings, Mapping[str, object] | None], tuple[list[str], list[tuple]]
]


class ActionAuditConfigError(RuntimeError):
    """Action auditing was requested without an audit table configuration."""


class ActionAuditLogger:
    def __init__(
        self, settings: Settings, sql_runner: SqlRunner = run_user_sql
    ) -> None:
        if not settings.action_audit_table:
            raise ActionAuditConfigError(
                "VKG_ACTION_AUDIT_TABLE is required for action audit"
            )
        self._settings = settings
        self._sql_runner = sql_runner
        self._table = quote_fqn(tuple(settings.action_audit_table.split(".")))

    def record(
        self,
        row: ActionAuditRow,
        token: str,
        timeout_seconds: int | None = None,
    ) -> str:
        audit_id = f"audit_{uuid4().hex}"
        statement = f"""
            INSERT INTO {self._table} (
                audit_id, phase, status, action_iri, action_kind, subject_iri,
                prepare_id, params_hash, preview_hash, old_value_hash,
                idempotency_key, source_table, source_key_column, source_value_column,
                old_value, new_value, params_json, preview_json, result_json,
                error_message, effective_user, created_at
            ) VALUES (
                :audit_id, :phase, :status, :action_iri, :action_kind, :subject_iri,
                :prepare_id, :params_hash, :preview_hash, :old_value_hash,
                :idempotency_key, :source_table, :source_key_column, :source_value_column,
                :old_value, :new_value, :params_json, :preview_json, :result_json,
                :error_message, session_user(), current_timestamp()
            )
        """
        self._sql_runner(
            statement,
            token,
            self._settings,
            {"audit_id": audit_id, **asdict(row)},
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
                error_message, effective_user
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
        return _record(columns, rows[0])

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
                error_message, effective_user
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
        return [_record(columns, row) for row in rows]


def _record(columns: list[str], row: tuple) -> ActionAuditRecord:
    values = dict(zip(columns, row, strict=True))
    audit_id = values.pop("audit_id")
    effective_user = values.pop("effective_user")
    return ActionAuditRecord(
        audit_id=str(audit_id),
        row=ActionAuditRow(**values),
        effective_user=str(effective_user),
    )
