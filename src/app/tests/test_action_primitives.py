"""Unit tests for runtime VKG action execution primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from actions.audit import ActionAuditConfigError, ActionAuditLogger
from actions.dbsql import quote_fqn, run_user_sql
from actions.models import ActionAuditRow, PrepareTokenPayload
from actions.tokens import PrepareTokenSigner
from config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "warehouse_id": "wh",
        "mappings_volume_path": "/Volumes/test/mappings",
        "mapping_file": "mapping.ttl",
        "ontology_file": "ontology.ttl",
        "default_catalog": "test_catalog",
        "default_schema": "test_schema",
        "ontop_internal_port": 18080,
        "app_port": 8000,
        "work_dir": Path("/tmp/ontop-vkg-test"),
        "fm_model_name": "test-model",
    }
    values.update(overrides)
    return Settings(**values)


def test_run_user_sql_uses_forwarded_token(monkeypatch):
    observed = {}

    class Cursor:
        description = [("value",)]

        def execute(self, sql, parameters=None):
            observed["sql"] = sql
            observed["parameters"] = parameters

        def fetchall(self):
            return [(1,)]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    def connect(**kwargs):
        observed["kwargs"] = kwargs
        return Connection()

    monkeypatch.setattr("databricks.sql.connect", connect)

    columns, rows = run_user_sql(
        "SELECT :x AS value", "user-token", _settings(), {"x": 1}
    )

    assert observed["kwargs"]["access_token"] == "user-token"
    assert observed["parameters"] == {"x": 1}
    assert columns == ["value"]
    assert rows == [(1,)]


def test_run_user_sql_sets_statement_timeout_on_execution_cursor(monkeypatch):
    calls = []

    class Cursor:
        description = [("value",)]

        def execute(self, sql, parameters=None):
            calls.append((sql, parameters))

        def fetchall(self):
            return [(1,)]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr("databricks.sql.connect", lambda **_: Connection())

    columns, rows = run_user_sql(
        "SELECT 1 AS value",
        "user-token",
        _settings(),
        timeout_seconds=17,
    )

    assert calls == [
        ("SET STATEMENT_TIMEOUT = 17", None),
        ("SELECT 1 AS value", None),
    ]
    assert columns == ["value"]
    assert rows == [(1,)]


@pytest.mark.parametrize("timeout_seconds", [0, -1, True])
def test_run_user_sql_rejects_invalid_statement_timeout(monkeypatch, timeout_seconds):
    def unexpected_connect(**_):
        raise AssertionError("invalid timeout must fail before connecting")

    monkeypatch.setattr("databricks.sql.connect", unexpected_connect)

    with pytest.raises(ValueError, match="positive integer"):
        run_user_sql(
            "SELECT 1",
            "user-token",
            _settings(),
            timeout_seconds=timeout_seconds,
        )


def test_quote_fqn_requires_a_simple_three_part_identifier():
    assert (
        quote_fqn(("cat", "sch", "vkg_action_audit"))
        == "`cat`.`sch`.`vkg_action_audit`"
    )
    with pytest.raises(ValueError, match="three-part"):
        quote_fqn(("cat", "sch"))
    with pytest.raises(ValueError, match="simple Databricks"):
        quote_fqn(("cat", "sch", "audit; DROP TABLE audit"))


def test_prepare_token_round_trips_and_detects_tampering():
    signer = PrepareTokenSigner("secret", ttl_seconds=600)
    payload = PrepareTokenPayload(
        prepare_id="prepare_1",
        action_iri="https://example.com/action",
        action_kind="WRITE_BACK",
        subject_iri="https://example.com/Supplier/1",
        effective_user="user@example.com",
        params_hash="abc",
        preview_hash="def",
        old_value_hash="ghi",
        issued_at=1_000,
        expires_at=1_600,
        catalog_fingerprint="catalog",
    )

    token = signer.sign(payload)

    assert signer.verify(token, now=1_001) == payload
    with pytest.raises(ValueError, match="signature"):
        signer.verify(token[:-1] + "x", now=1_001)
    with pytest.raises(ValueError, match="expired"):
        signer.verify(token, now=1_600)


def test_prepare_token_rejects_malformed_and_unsupported_payloads():
    signer = PrepareTokenSigner("secret")

    with pytest.raises(ValueError, match="malformed"):
        signer.verify("not-a-token", now=1)

    payload = PrepareTokenPayload(
        prepare_id="prepare_1",
        action_iri="https://example.com/action",
        action_kind="WRITE_BACK",
        subject_iri="https://example.com/Supplier/1",
        effective_user="user@example.com",
        params_hash="abc",
        preview_hash="def",
        old_value_hash="ghi",
        issued_at=1_000,
        expires_at=1_600,
        catalog_fingerprint="catalog",
        version=3,
    )
    with pytest.raises(ValueError, match="unsupported"):
        signer.sign(payload)


def test_audit_logger_inserts_with_user_token():
    calls = []
    logger = ActionAuditLogger(
        settings=_settings(action_audit_table="cat.sch.vkg_action_audit"),
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            calls.append((sql, token, parameters)) or ([], [])
        ),
    )

    audit_id = logger.record(
        ActionAuditRow(
            phase="PREPARE",
            status="PREPARED",
            action_iri="https://example.com/action",
            action_kind="WRITE_BACK",
            subject_iri="https://example.com/Supplier/1",
            idempotency_key="request-1",
            source_table="cat.sch.supplier",
            source_key_column="supplier_id",
            source_value_column="supplier_name",
            old_value="old",
            new_value="new",
            params_json='{"newValue":"new"}',
            preview_json='{"old_value":"old","new_value":"new"}',
            result_json='{"rows_affected":1}',
        ),
        token="user-token",
    )

    assert audit_id.startswith("audit_")
    assert calls[0][1] == "user-token"
    assert "INSERT INTO `cat`.`sch`.`vkg_action_audit`" in calls[0][0]
    assert "params_json, preview_json, result_json" in calls[0][0]
    assert calls[0][2]["old_value"] == "old"
    assert calls[0][2]["new_value"] == "new"
    assert "session_user()" in calls[0][0]


def test_audit_logger_reads_prepared_row_with_user_token():
    calls = []
    columns = [
        "audit_id",
        "phase",
        "status",
        "action_iri",
        "action_kind",
        "subject_iri",
        "prepare_id",
        "params_hash",
        "preview_hash",
        "old_value_hash",
        "idempotency_key",
        "source_table",
        "source_key_column",
        "source_value_column",
        "old_value",
        "new_value",
        "params_json",
        "preview_json",
        "result_json",
        "error_message",
        "effective_user",
    ]
    row = (
        "audit_1",
        "PREPARE",
        "PREPARED",
        "https://example.com/action",
        "WRITE_BACK",
        "https://example.com/Supplier/1",
        "prepare_1",
        "params",
        "preview",
        "oldhash",
        "request-1",
        "cat.sch.supplier",
        "supplier_id",
        "supplier_name",
        "old",
        "new",
        '{"newValue":"new"}',
        '{"old_value":"old","new_value":"new"}',
        None,
        None,
        "user@example.com",
    )
    logger = ActionAuditLogger(
        settings=_settings(action_audit_table="cat.sch.vkg_action_audit"),
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            calls.append((sql, token, parameters)) or (columns, [row])
        ),
    )

    record = logger.get_prepared("prepare_1", "user-token")

    assert record is not None
    assert record.audit_id == "audit_1"
    assert record.effective_user == "user@example.com"
    assert record.row.preview_json == '{"old_value":"old","new_value":"new"}'
    assert calls[0][1] == "user-token"
    assert calls[0][2] == {"prepare_id": "prepare_1"}
    assert "effective_user = session_user()" in calls[0][0]


def test_audit_logger_reads_confirmation_history_with_user_token():
    calls = []
    columns = [
        "audit_id",
        "phase",
        "status",
        "action_iri",
        "action_kind",
        "subject_iri",
        "prepare_id",
        "params_hash",
        "preview_hash",
        "old_value_hash",
        "idempotency_key",
        "source_table",
        "source_key_column",
        "source_value_column",
        "old_value",
        "new_value",
        "params_json",
        "preview_json",
        "result_json",
        "error_message",
        "effective_user",
    ]
    rows = [
        (
            "audit_2",
            "CONFIRM",
            "CONFIRMING",
            "https://example.com/action",
            "EXTERNAL",
            "https://example.com/Supplier/1",
            "prepare_1",
            "params",
            "preview",
            None,
            "request-1",
            None,
            None,
            None,
            None,
            None,
            "{}",
            "{}",
            None,
            None,
            "user@example.com",
        ),
        (
            "audit_3",
            "CONFIRM",
            "COMPLETED",
            "https://example.com/action",
            "EXTERNAL",
            "https://example.com/Supplier/1",
            "prepare_1",
            "params",
            "preview",
            None,
            "request-1",
            None,
            None,
            None,
            None,
            None,
            "{}",
            "{}",
            '{"status":"COMPLETED"}',
            None,
            "user@example.com",
        ),
    ]
    logger = ActionAuditLogger(
        settings=_settings(action_audit_table="cat.sch.vkg_action_audit"),
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            calls.append((sql, token, parameters)) or (columns, rows)
        ),
    )

    history = logger.get_confirmation_history("prepare_1", "user-token")

    assert [record.audit_id for record in history] == ["audit_2", "audit_3"]
    assert {record.effective_user for record in history} == {"user@example.com"}
    assert calls[0][1] == "user-token"
    assert calls[0][2] == {"prepare_id": "prepare_1"}
    assert "effective_user = session_user()" in calls[0][0]


def test_audit_logger_requires_a_configured_table():
    with pytest.raises(ActionAuditConfigError, match="VKG_ACTION_AUDIT_TABLE"):
        ActionAuditLogger(_settings())
