"""Tests for the prepare/confirm VKG action service."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest
from rdflib import Graph

from actions.catalog import ActionCatalog
from actions.models import ActionAuditRecord, ActionDefinition
from actions.r2rml_classifier import classify_writeback
from actions.service import (
    ActionConflictError,
    ActionError,
    ActionReadOnlyError,
    ActionService,
    ActionUnavailableError,
    ActionValidationError,
    ConfirmActionRequest,
    PrepareActionRequest,
)
from actions.tokens import PrepareTokenSigner
from config import Settings


MAPPING = """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ont: <https://example.com/ontology#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ont:SupplierMap a rr:TriplesMap ;
  rr:logicalTable [ rr:tableName "cat.sch.supplier" ] ;
  rr:subjectMap [
    rr:class ont:Supplier ;
    rr:template "https://example.com/ontology/Supplier/{supplier_id}"
  ] ;
  rr:predicateObjectMap [
    rr:predicate ont:supplierName ;
    rr:objectMap [ rr:column "supplier_name" ; rr:datatype xsd:string ]
  ] .
"""


def _settings() -> Settings:
    return Settings(
        warehouse_id="wh",
        mappings_volume_path="/Volumes/test/mappings",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog="cat",
        default_schema="sch",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=Path("/tmp/ontop-vkg-test"),
        fm_model_name="test-model",
        action_prepare_ttl_seconds=42,
    )


def _writeback_action() -> ActionDefinition:
    return ActionDefinition(
        iri="https://example.com/ontology#updateSupplierName",
        logical_key="updateSupplierName",
        kind="WRITE_BACK",
        bound_class_iri="https://example.com/ontology#Supplier",
        target_property_iri="https://example.com/ontology#supplierName",
        status="PUBLISHED",
    )


def _test_actor_resolver(token, settings, timeout_seconds=None):
    return "user@example.com"


async def _test_subject_checker(subject_iri, class_iri, token, timeout_seconds):
    return True


def _external_result(parameters, status="COMPLETED", **values):
    return {
        "status": status,
        "invocation_id": parameters["invocation_id"],
        "request_hash": parameters["request_hash"],
        **values,
    }


def _service(
    sql_runner,
    audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
        "audit"
    ),
    audit_lookup=None,
    audit_history_lookup=None,
    actor_resolver=_test_actor_resolver,
) -> ActionService:
    action = _writeback_action()
    catalog = ActionCatalog(
        available=True,
        actions=[action],
        writeback_catalog=classify_writeback(
            Graph().parse(data=MAPPING, format="turtle"), None, [action]
        ),
    )
    return ActionService(
        catalog=catalog,
        settings=_settings(),
        token_signer=PrepareTokenSigner("secret"),
        sql_runner=sql_runner,
        audit_recorder=audit_recorder,
        audit_lookup=audit_lookup,
        audit_history_lookup=audit_history_lookup,
        actor_resolver=actor_resolver,
    )


def _external_service(
    sql_runner,
    audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
        "audit"
    ),
    audit_lookup=None,
    audit_history_lookup=None,
    actor_resolver=_test_actor_resolver,
    bound_class_iri="https://example.com/ontology#SourcingBundle",
    subject_checker=_test_subject_checker,
    idempotency_strategy="REQUEST_KEY",
) -> ActionService:
    action = ActionDefinition(
        iri="https://example.com/ontology#createPurchaseOrder",
        logical_key="createPurchaseOrder",
        kind="EXTERNAL",
        bound_class_iri=bound_class_iri,
        function_fqn="cat.sch.create_purchase_order",
        status="PUBLISHED",
        input_schema={"quantity": "integer"},
        idempotency_strategy=idempotency_strategy,
    )
    return ActionService(
        catalog=ActionCatalog(available=True, actions=[action]),
        settings=_settings(),
        token_signer=PrepareTokenSigner("secret"),
        sql_runner=sql_runner,
        audit_recorder=audit_recorder,
        audit_lookup=audit_lookup,
        audit_history_lookup=audit_history_lookup,
        actor_resolver=actor_resolver,
        subject_checker=subject_checker,
    )


def _prepare_request() -> PrepareActionRequest:
    return PrepareActionRequest(
        action_iri="https://example.com/ontology#updateSupplierName",
        subject_iri="https://example.com/ontology/Supplier/S1",
        params={"newValue": "new"},
        idempotency_key="request-1",
    )


class _AuditStore:
    def __init__(self) -> None:
        self.records: list[ActionAuditRecord] = []
        self._lock = threading.Lock()

    def record(self, row, token, effective_user=None, timeout_seconds=None):
        assert token == "user-token"
        with self._lock:
            record = ActionAuditRecord(
                audit_id=f"audit_{len(self.records) + 1}",
                row=row,
                effective_user="user@example.com",
            )
            self.records.append(record)
        return record.audit_id

    def prepared(self, prepare_id, token, timeout_seconds=None):
        assert token == "user-token"
        return next(
            (
                record
                for record in self.records
                if record.row.prepare_id == prepare_id
                and record.row.phase == "PREPARE"
                and record.row.status == "PREPARED"
            ),
            None,
        )

    def confirmation(self, prepare_id, token, timeout_seconds=None):
        assert token == "user-token"
        return [
            record
            for record in self.records
            if record.row.prepare_id == prepare_id and record.row.phase == "CONFIRM"
        ]


def test_prepare_writeback_reads_old_value_and_writes_audit():
    sql_calls = []
    audit_rows = []
    service = _service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            sql_calls.append((sql, token, parameters)) or (["old_value"], [("old",)])
        ),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append((row, token)) or "audit_prepare"
        ),
    )

    response = asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    assert response.action_kind == "WRITE_BACK"
    assert response.preview["old_value"] == "old"
    assert response.preview["new_value"] == "new"
    assert sql_calls[0][1] == "user-token"
    assert audit_rows[0][1] == "user-token"
    audit_row = audit_rows[0][0]
    assert audit_row.old_value == "old"
    assert audit_row.new_value == "new"
    assert audit_row.preview_json is not None
    assert audit_row.params_json == '{"newValue":"new"}'


def test_prepare_does_not_block_event_loop_during_slow_sql():
    def slow_runner(sql, token, settings, parameters=None, **_):
        time.sleep(0.2)
        return ["old_value"], [("old",)]

    service = _service(sql_runner=slow_runner)

    async def exercise():
        started = time.perf_counter()

        async def heartbeat():
            await asyncio.sleep(0.02)
            return time.perf_counter() - started

        _, heartbeat_elapsed = await asyncio.gather(
            service.prepare(_prepare_request(), token="user-token"),
            heartbeat(),
        )
        return heartbeat_elapsed

    assert asyncio.run(exercise()) < 0.1


def test_confirm_writeback_revalidates_and_updates_with_user_token():
    calls = []

    def runner(sql, token, settings, parameters=None, **_):
        calls.append((sql, token, parameters))
        if sql.lstrip().upper().startswith("SELECT"):
            return ["old_value"], [("old",)]
        return ["num_affected_rows"], [(1,)]

    service = _service(sql_runner=runner)
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    result = asyncio.run(
        service.confirm(
            ConfirmActionRequest(preparation_token=prepared.preparation_token),
            token="user-token",
        )
    )

    assert result.status == "COMPLETED"
    assert any(
        call[0].lstrip().upper().startswith("UPDATE") and call[1] == "user-token"
        for call in calls
    )


def test_action_timeout_applies_to_sql_and_audit_operations():
    sql_timeouts = []
    audit_timeouts = []

    def runner(sql, token, settings, parameters=None, *, timeout_seconds=None):
        sql_timeouts.append(timeout_seconds)
        if sql.lstrip().upper().startswith("SELECT"):
            return ["old_value"], [("old",)]
        return ["num_affected_rows"], [(1,)]

    def recorder(row, token, effective_user=None, timeout_seconds=None):
        audit_timeouts.append(timeout_seconds)
        return f"audit_{row.status}"

    service = _service(runner, audit_recorder=recorder)
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    result = asyncio.run(
        service.confirm(
            ConfirmActionRequest(preparation_token=prepared.preparation_token),
            token="user-token",
        )
    )

    assert result.status == "COMPLETED"
    assert sql_timeouts == [60, 60, 60]
    assert audit_timeouts == [60, 60, 60]


def test_action_audits_are_signed_for_the_resolved_actor():
    observed = []

    def recorder(row, token, effective_user=None, timeout_seconds=None):
        observed.append((row.status, token, effective_user, timeout_seconds))
        return f"audit_{row.status}"

    service = _service(
        lambda sql, token, settings, parameters=None, **_: (
            ["old_value"],
            [("old",)],
        ),
        audit_recorder=recorder,
        actor_resolver=lambda *_: "user@example.com",
    )

    asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    assert observed == [("PREPARED", "user-token", "user@example.com", 60)]


def test_confirm_writeback_rejects_stale_value():
    reads = iter([(["old_value"], [("old",)]), (["old_value"], [("changed",)])])
    audit_rows = []
    service = _service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: next(reads),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append((row, token)) or f"audit_{len(audit_rows)}"
        ),
    )
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    with pytest.raises(ActionConflictError, match="changed since prepare"):
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )

    assert [(row.phase, row.status, token) for row, token in audit_rows][-1] == (
        "CONFIRM",
        "REFUSED",
        "user-token",
    )


def test_confirm_external_invokes_uc_function_with_user_token():
    calls = []
    service = _external_service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            calls.append((sql, token, parameters))
            or (["result"], [(_external_result(parameters),)])
        )
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )

    result = asyncio.run(
        service.confirm(
            ConfirmActionRequest(preparation_token=prepared.preparation_token),
            token="user-token",
        )
    )

    assert result.status == "COMPLETED"
    assert calls[-1][1] == "user-token"
    assert "SELECT `cat`.`sch`.`create_purchase_order`" in calls[-1][0]


def test_external_prepare_rejects_subject_outside_bound_class():
    async def subject_checker(subject_iri, class_iri, token, timeout_seconds):
        return False

    service = _external_service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: ([], []),
        subject_checker=subject_checker,
    )

    with pytest.raises(ActionValidationError, match="bound class"):
        asyncio.run(
            service.prepare(
                PrepareActionRequest(
                    action_iri="https://example.com/ontology#createPurchaseOrder",
                    subject_iri="https://example.com/ontology/SourcingBundle/B1",
                    params={"quantity": 2},
                    idempotency_key="request-1",
                ),
                token="user-token",
            )
        )


def test_external_confirm_rechecks_subject_before_function_execution():
    checks = iter([True, False])
    function_calls = []

    async def subject_checker(subject_iri, class_iri, token, timeout_seconds):
        return next(checks)

    service = _external_service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            function_calls.append(sql) or (["result"], [({"status": "COMPLETED"},)])
        ),
        subject_checker=subject_checker,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )

    with pytest.raises(ActionConflictError, match="bound class"):
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )

    assert function_calls == []


def test_external_prepare_requires_bound_class():
    service = _external_service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: ([], []),
        bound_class_iri=None,
    )

    with pytest.raises(ActionReadOnlyError) as error:
        asyncio.run(
            service.prepare(
                PrepareActionRequest(
                    action_iri="https://example.com/ontology#createPurchaseOrder",
                    subject_iri="https://example.com/ontology/SourcingBundle/B1",
                    params={"quantity": 2},
                    idempotency_key="request-1",
                ),
                token="user-token",
            )
        )

    assert "MISSING_BOUND_CLASS" in error.value.reason_codes


def test_confirm_rejects_token_from_another_effective_user():
    function_calls = []

    def actor_resolver(token, settings, timeout_seconds=None):
        return {
            "token-a": "user-a@example.com",
            "token-b": "user-b@example.com",
        }[token]

    service = _external_service(
        sql_runner=lambda sql, token, settings, parameters=None, **_: (
            function_calls.append((sql, token))
            or (["result"], [({"status": "COMPLETED"},)])
        ),
        actor_resolver=actor_resolver,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="token-a",
        )
    )

    with pytest.raises(ActionError, match="preparing user") as error:
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="token-b",
            )
        )

    assert error.value.status_code == 403
    assert function_calls == []


def test_prepare_requires_idempotency_key_for_request_key_actions():
    audit_rows = []
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], []),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append((row, token)) or "audit_failed"
        ),
    )

    with pytest.raises(ActionValidationError, match="idempotency_key"):
        asyncio.run(
            service.prepare(
                PrepareActionRequest(
                    action_iri="https://example.com/ontology#createPurchaseOrder",
                    subject_iri="https://example.com/ontology/SourcingBundle/B1",
                    params={"quantity": 2},
                ),
                token="user-token",
            )
        )

    assert [(row.phase, row.status, token) for row, token in audit_rows] == [
        ("PREPARE", "FAILED", "user-token")
    ]


def test_repeated_prepare_converges_on_one_external_invocation():
    function_calls = []

    def runner(sql, token, settings, parameters=None, **_):
        function_calls.append(parameters)
        return ["result"], [
            (
                {
                    "status": "COMPLETED",
                    "invocation_id": parameters.get("invocation_id"),
                    "request_hash": parameters.get("request_hash"),
                },
            )
        ]

    service = _external_service(runner)
    request = PrepareActionRequest(
        action_iri="https://example.com/ontology#createPurchaseOrder",
        subject_iri="https://example.com/ontology/SourcingBundle/B1",
        params={"quantity": 2},
        idempotency_key=" request-1 ",
    )

    first = asyncio.run(service.prepare(request, token="user-token"))
    second = asyncio.run(service.prepare(request, token="user-token"))
    first_payload = PrepareTokenSigner("secret").verify(first.preparation_token)
    second_payload = PrepareTokenSigner("secret").verify(second.preparation_token)

    assert first.prepare_id == second.prepare_id
    assert first_payload.invocation_id == second_payload.invocation_id
    assert first_payload.invocation_id.startswith("v1:")
    assert first_payload.request_hash == second_payload.request_hash

    first_result = asyncio.run(
        service.confirm(
            ConfirmActionRequest(preparation_token=first.preparation_token),
            token="user-token",
        )
    )
    second_result = asyncio.run(
        service.confirm(
            ConfirmActionRequest(preparation_token=second.preparation_token),
            token="user-token",
        )
    )

    assert first_result == second_result
    assert len(function_calls) == 1


def test_prepare_rejects_request_key_reuse_with_different_payload():
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], [])
    )
    original = PrepareActionRequest(
        action_iri="https://example.com/ontology#createPurchaseOrder",
        subject_iri="https://example.com/ontology/SourcingBundle/B1",
        params={"quantity": 2},
        idempotency_key="request-1",
    )
    conflicting = original.model_copy(update={"params": {"quantity": 3}})

    asyncio.run(service.prepare(original, token="user-token"))

    with pytest.raises(ActionConflictError, match="idempotency key"):
        asyncio.run(service.prepare(conflicting, token="user-token"))


def test_two_replicas_share_target_enforced_invocation_identity():
    stored_results = {}
    side_effect_count = 0
    target_lock = threading.Lock()

    def target(sql, token, settings, parameters=None, **_):
        nonlocal side_effect_count
        invocation_id = parameters["invocation_id"]
        request_hash = parameters["request_hash"]
        with target_lock:
            stored = stored_results.get(invocation_id)
            if stored is None:
                side_effect_count += 1
                stored = _external_result(
                    parameters,
                    external_request_id="PO-1",
                )
                stored_results[invocation_id] = stored
            elif stored["request_hash"] != request_hash:
                stored = _external_result(
                    parameters,
                    status="CONFLICT",
                    message="invocation request hash changed",
                )
        return ["result"], [(stored,)]

    first_service = _external_service(target)
    second_service = _external_service(target)
    request = PrepareActionRequest(
        action_iri="https://example.com/ontology#createPurchaseOrder",
        subject_iri="https://example.com/ontology/SourcingBundle/B1",
        params={"quantity": 2},
        idempotency_key="request-1",
    )
    first_prepared = asyncio.run(first_service.prepare(request, token="user-token"))
    second_prepared = asyncio.run(second_service.prepare(request, token="user-token"))
    results = []

    def confirm(service, preparation_token):
        results.append(
            asyncio.run(
                service.confirm(
                    ConfirmActionRequest(preparation_token=preparation_token),
                    token="user-token",
                )
            )
        )

    threads = [
        threading.Thread(
            target=confirm,
            args=(first_service, first_prepared.preparation_token),
        ),
        threading.Thread(
            target=confirm,
            args=(second_service, second_prepared.preparation_token),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert first_prepared.prepare_id == second_prepared.prepare_id
    assert len(results) == 2
    assert results[0].result == results[1].result
    assert side_effect_count == 1
    assert len(stored_results) == 1


def test_repeated_prepare_after_restart_reuses_persisted_preparation():
    store = _AuditStore()
    request = PrepareActionRequest(
        action_iri="https://example.com/ontology#createPurchaseOrder",
        subject_iri="https://example.com/ontology/SourcingBundle/B1",
        params={"quantity": 2},
        idempotency_key="request-1",
    )
    first_service = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], []),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )
    first = asyncio.run(first_service.prepare(request, token="user-token"))
    restarted = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], []),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )

    repeated = asyncio.run(restarted.prepare(request, token="user-token"))

    assert repeated.prepare_id == first.prepare_id
    assert repeated.preview == first.preview
    assert [
        record
        for record in store.records
        if record.row.phase == "PREPARE" and record.row.status == "PREPARED"
    ] == [store.records[0]]


def test_repeated_prepare_rejects_recovered_preview_that_differs_from_request():
    store = _AuditStore()
    request = PrepareActionRequest(
        action_iri="https://example.com/ontology#createPurchaseOrder",
        subject_iri="https://example.com/ontology/SourcingBundle/B1",
        params={"quantity": 2},
        idempotency_key="request-1",
    )
    first_service = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], []),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )
    asyncio.run(first_service.prepare(request, token="user-token"))
    stored = store.records[0]
    store.records[0] = replace(
        stored,
        row=replace(
            stored.row,
            preview_json=(
                '{"function_fqn":"cat.sch.create_purchase_order",'
                '"idempotency_key":"request-1","params":{"quantity":999},'
                '"subject_iri":"https://example.com/ontology/SourcingBundle/B1"}'
            ),
            preview_hash=(
                "061d338ff6f9500c467e8d0d47c4850f84860797e81f01c7653a91c647240b41"
            ),
        ),
    )
    restarted = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], []),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )

    with pytest.raises(ActionConflictError, match="prepared action audit is invalid"):
        asyncio.run(restarted.prepare(request, token="user-token"))


def test_external_result_must_echo_invocation_and_request_hashes():
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: (
            ["result"],
            [({"status": "COMPLETED"},)],
        )
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )

    with pytest.raises(ActionUnavailableError, match="idempotency envelope"):
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )


def test_published_external_action_rejects_unknown_idempotency_strategy():
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], []),
        idempotency_strategy="BEST_EFFORT",
    )

    with pytest.raises(ActionReadOnlyError) as error:
        asyncio.run(
            service.prepare(
                PrepareActionRequest(
                    action_iri="https://example.com/ontology#createPurchaseOrder",
                    subject_iri="https://example.com/ontology/SourcingBundle/B1",
                    params={"quantity": 2},
                    idempotency_key="request-1",
                ),
                token="user-token",
            )
        )

    assert "UNSUPPORTED_IDEMPOTENCY_STRATEGY" in error.value.reason_codes


def test_failed_prepare_read_writes_failure_audit():
    audit_rows = []
    service = _service(
        lambda sql, token, settings, parameters=None, **_: (_ for _ in ()).throw(
            RuntimeError("read failed")
        ),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append((row, token)) or "audit_failed"
        ),
    )

    with pytest.raises(ActionUnavailableError, match="read action source"):
        asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    assert [(row.phase, row.status, token) for row, token in audit_rows] == [
        ("PREPARE", "FAILED", "user-token")
    ]


def test_repeated_external_confirm_returns_recorded_result_without_reinvocation():
    store = _AuditStore()
    invocations = 0

    def runner(sql, token, settings, parameters=None, **_):
        nonlocal invocations
        invocations += 1
        return ["result"], [(_external_result(parameters, external_request_id="PO-1"),)]

    service = _external_service(
        runner,
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )
    request = ConfirmActionRequest(preparation_token=prepared.preparation_token)

    first = asyncio.run(service.confirm(request, token="user-token"))
    repeated = asyncio.run(service.confirm(request, token="user-token"))

    assert repeated == first
    assert invocations == 1


def test_completed_external_retry_replays_before_subject_revalidation():
    store = _AuditStore()
    subject_checks = []
    invocations = 0

    async def subject_checker(subject_iri, class_iri, token, timeout_seconds):
        subject_checks.append((subject_iri, class_iri, token, timeout_seconds))
        return True

    def runner(sql, token, settings, parameters=None, **_):
        nonlocal invocations
        invocations += 1
        return ["result"], [(_external_result(parameters),)]

    service = _external_service(
        runner,
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
        subject_checker=subject_checker,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )
    request = ConfirmActionRequest(preparation_token=prepared.preparation_token)

    first = asyncio.run(service.confirm(request, token="user-token"))
    retry_checks = []

    async def missing_subject(subject_iri, class_iri, token, timeout_seconds):
        retry_checks.append((subject_iri, class_iri, token, timeout_seconds))
        return False

    restarted = _external_service(
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed external action must not be reinvoked")
        ),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
        subject_checker=missing_subject,
    )
    repeated = asyncio.run(restarted.confirm(request, token="user-token"))

    assert repeated == first
    assert invocations == 1
    assert len(subject_checks) == 2
    assert retry_checks == []


def test_external_retry_recovers_after_transient_subject_check_failure():
    store = _AuditStore()
    subject_checks = 0
    invocations = 0

    async def subject_checker(subject_iri, class_iri, token, timeout_seconds):
        nonlocal subject_checks
        subject_checks += 1
        if subject_checks == 2:
            raise RuntimeError("Ontop temporarily unavailable")
        return True

    def runner(sql, token, settings, parameters=None, **_):
        nonlocal invocations
        invocations += 1
        return ["result"], [(_external_result(parameters),)]

    service = _external_service(
        runner,
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
        subject_checker=subject_checker,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )
    request = ConfirmActionRequest(preparation_token=prepared.preparation_token)

    with pytest.raises(ActionUnavailableError, match="subject validation failed"):
        asyncio.run(service.confirm(request, token="user-token"))
    recovered = asyncio.run(service.confirm(request, token="user-token"))

    assert recovered.status == "COMPLETED"
    assert invocations == 1
    assert [record.row.status for record in store.records] == [
        "PREPARED",
        "REFUSED",
        "CONFIRMING",
        "COMPLETED",
    ]


def test_repeated_external_confirm_after_restart_uses_audit_outcome():
    store = _AuditStore()
    calls = []
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: (
            calls.append(sql)
            or (
                ["result"],
                [
                    (
                        _external_result(
                            parameters,
                            status="SUCCESS",
                            external_request_id="PO-1",
                        ),
                    )
                ],
            )
        ),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )
    request = ConfirmActionRequest(preparation_token=prepared.preparation_token)
    first = asyncio.run(service.confirm(request, token="user-token"))
    restarted = _external_service(
        lambda sql, token, settings, parameters=None, **_: (_ for _ in ()).throw(
            AssertionError("external function must not be reinvoked")
        ),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )

    repeated = asyncio.run(restarted.confirm(request, token="user-token"))

    assert repeated == first
    assert len(calls) == 1


def test_confirmation_replay_rejects_history_with_different_request_hashes():
    store = _AuditStore()
    function_calls = []
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: (
            function_calls.append(sql)
            or (["result"], [(_external_result(parameters),)])
        ),
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )
    store.records.append(
        ActionAuditRecord(
            audit_id="audit_conflicting_result",
            row=replace(
                store.records[0].row,
                phase="CONFIRM",
                status="COMPLETED",
                params_hash="different-request",
                result_json='{"status":"COMPLETED"}',
            ),
            effective_user="user@example.com",
        )
    )

    with pytest.raises(ActionConflictError, match="audit does not match"):
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )

    assert function_calls == []


def test_concurrent_external_confirms_execute_once_in_process():
    store = _AuditStore()
    invocation_count = 0
    invocation_lock = threading.Lock()

    def runner(sql, token, settings, parameters=None, **_):
        nonlocal invocation_count
        with invocation_lock:
            invocation_count += 1
        time.sleep(0.05)
        return ["result"], [(_external_result(parameters),)]

    service = _external_service(
        runner,
        audit_recorder=store.record,
        audit_lookup=store.prepared,
        audit_history_lookup=store.confirmation,
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )
    request = ConfirmActionRequest(preparation_token=prepared.preparation_token)
    results = []

    def confirm():
        results.append(asyncio.run(service.confirm(request, token="user-token")))

    threads = [threading.Thread(target=confirm), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0] == results[1]
    assert invocation_count == 1


@pytest.mark.parametrize("envelope_status", ["FAILED", "ERROR"])
def test_external_failure_envelope_is_audited_and_returned(envelope_status):
    audit_rows = []
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: (
            ["result"],
            [
                (
                    _external_result(
                        parameters,
                        status=envelope_status,
                        message="upstream refused",
                    ),
                )
            ],
        ),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append(row) or f"audit_{len(audit_rows)}"
        ),
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )

    response = asyncio.run(
        service.confirm(
            ConfirmActionRequest(preparation_token=prepared.preparation_token),
            token="user-token",
        )
    )

    assert response.status == "FAILED"
    assert response.result["status"] == envelope_status
    assert [row.status for row in audit_rows] == ["PREPARED", "CONFIRMING", "FAILED"]


@pytest.mark.parametrize("result", [{}, {"status": "WAITING"}, "not-an-envelope"])
def test_external_malformed_or_unknown_envelope_fails_closed(result):
    audit_rows = []

    def runner(sql, token, settings, parameters=None, **_):
        envelope = (
            {
                **result,
                "invocation_id": parameters["invocation_id"],
                "request_hash": parameters["request_hash"],
            }
            if isinstance(result, dict)
            else result
        )
        return ["result"], [(envelope,)]

    service = _external_service(
        runner,
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append(row) or f"audit_{len(audit_rows)}"
        ),
    )
    prepared = asyncio.run(
        service.prepare(
            PrepareActionRequest(
                action_iri="https://example.com/ontology#createPurchaseOrder",
                subject_iri="https://example.com/ontology/SourcingBundle/B1",
                params={"quantity": 2},
                idempotency_key="request-1",
            ),
            token="user-token",
        )
    )

    with pytest.raises(ActionUnavailableError, match="invalid .* envelope"):
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )

    assert [row.status for row in audit_rows] == ["PREPARED", "CONFIRMING", "FAILED"]


def test_confirm_reconstructs_prepared_writeback_from_audit_after_restart():
    audit_rows = []

    def runner(sql, token, settings, parameters=None, **_):
        if sql.lstrip().upper().startswith("SELECT"):
            return ["old_value"], [("old",)]
        return ["num_affected_rows"], [(1,)]

    service = _service(
        runner,
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append((row, token)) or "audit_prepare"
        ),
    )
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))
    persisted = ActionAuditRecord(
        audit_id="audit_prepare",
        row=audit_rows[0][0],
        effective_user="user@example.com",
    )
    restarted = _service(
        runner, audit_lookup=lambda prepare_id, token, timeout_seconds=None: persisted
    )

    result = asyncio.run(
        restarted.confirm(
            ConfirmActionRequest(preparation_token=prepared.preparation_token),
            token="user-token",
        )
    )

    assert result.status == "COMPLETED"


def test_confirm_rejects_audit_data_that_does_not_match_signed_hashes():
    audit_rows = []
    service = _service(
        lambda sql, token, settings, parameters=None, **_: (["old_value"], [("old",)]),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append((row, token)) or "audit_prepare"
        ),
    )
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))
    tampered = ActionAuditRecord(
        audit_id="audit_prepare",
        row=audit_rows[0][0].__class__(
            **{**audit_rows[0][0].__dict__, "params_json": '{"newValue":"changed"}'}
        ),
        effective_user="user@example.com",
    )
    restarted = _service(
        lambda sql, token, settings, parameters=None, **_: (["old_value"], [("old",)]),
        audit_lookup=lambda prepare_id, token, timeout_seconds=None: tampered,
    )

    with pytest.raises(ActionConflictError):
        asyncio.run(
            restarted.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )


def test_confirm_rejects_audit_idempotency_key_that_does_not_match_token():
    audit_rows = []
    service = _service(
        lambda sql, token, settings, parameters=None, **_: (
            ["old_value"],
            [("old",)],
        ),
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append(row) or "audit_prepare"
        ),
    )
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))
    tampered = ActionAuditRecord(
        audit_id="audit_prepare",
        row=replace(audit_rows[0], idempotency_key="another-request"),
        effective_user="user@example.com",
    )
    restarted = _service(
        lambda sql, token, settings, parameters=None, **_: (
            ["old_value"],
            [("old",)],
        ),
        audit_lookup=lambda prepare_id, token, timeout_seconds=None: tampered,
    )

    with pytest.raises(ActionConflictError):
        asyncio.run(
            restarted.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )


def test_confirm_rejects_zero_row_guarded_update_without_completed_audit():
    audit_rows = []

    def runner(sql, token, settings, parameters=None, **_):
        if sql.lstrip().upper().startswith("SELECT"):
            return ["old_value"], [("old",)]
        return ["num_affected_rows"], [(0,)]

    service = _service(
        runner,
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append(row) or f"audit_{row.status}"
        ),
    )
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    with pytest.raises(ActionConflictError, match="changed during update"):
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )

    assert "COMPLETED" not in [row.status for row in audit_rows]


def test_restart_recovery_uses_typed_old_value_from_preview_json():
    audit_rows = []

    def runner(sql, token, settings, parameters=None, **_):
        if sql.lstrip().upper().startswith("SELECT"):
            return ["old_value"], [(7,)]
        return ["num_affected_rows"], [(1,)]

    service = _service(
        runner,
        audit_recorder=lambda row, token, effective_user=None, timeout_seconds=None: (
            audit_rows.append(row) or "audit_prepare"
        ),
    )
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))
    persisted = ActionAuditRecord(
        audit_id="audit_prepare",
        row=replace(audit_rows[0], old_value="7"),
        effective_user="user@example.com",
    )
    restarted = _service(
        runner, audit_lookup=lambda prepare_id, token, timeout_seconds=None: persisted
    )

    result = asyncio.run(
        restarted.confirm(
            ConfirmActionRequest(preparation_token=prepared.preparation_token),
            token="user-token",
        )
    )

    assert result.result == {"rows_affected": 1}


def test_catalog_fingerprint_includes_writeback_target():
    service = _service(
        lambda sql, token, settings, parameters=None, **_: (["old_value"], [("old",)])
    )
    before = service._catalog_fingerprint()
    writeback_catalog = service._catalog.writeback_catalog
    assert writeback_catalog is not None
    action = _writeback_action()
    classification = writeback_catalog.property_for(
        action.bound_class_iri, action.target_property_iri
    )
    assert classification is not None and classification.target is not None
    changed = replace(
        classification,
        target=replace(classification.target, value_column="supplier_display_name"),
    )
    writeback_catalog._by_property[(changed.class_iri, changed.property_iri)] = changed

    assert service._catalog_fingerprint() != before


def test_completed_audit_failure_includes_confirming_audit_id():
    def recorder(row, token, effective_user=None, timeout_seconds=None):
        if row.status == "COMPLETED":
            raise RuntimeError("audit unavailable")
        return f"audit_{row.status}"

    def runner(sql, token, settings, parameters=None, **_):
        if sql.lstrip().upper().startswith("SELECT"):
            return ["old_value"], [("old",)]
        return ["num_affected_rows"], [(1,)]

    service = _service(runner, audit_recorder=recorder)
    prepared = asyncio.run(service.prepare(_prepare_request(), token="user-token"))

    with pytest.raises(ActionUnavailableError) as error:
        asyncio.run(
            service.confirm(
                ConfirmActionRequest(preparation_token=prepared.preparation_token),
                token="user-token",
            )
        )

    assert error.value.audit_id == "audit_CONFIRMING"


def test_external_prepare_rejects_undeclared_parameters():
    service = _external_service(
        lambda sql, token, settings, parameters=None, **_: ([], [])
    )

    with pytest.raises(ActionValidationError, match="undeclared"):
        asyncio.run(
            service.prepare(
                PrepareActionRequest(
                    action_iri="https://example.com/ontology#createPurchaseOrder",
                    subject_iri="https://example.com/ontology/SourcingBundle/B1",
                    params={"quantity": 2, "unrecognized": "value"},
                    idempotency_key="request-1",
                ),
                token="user-token",
            )
        )
