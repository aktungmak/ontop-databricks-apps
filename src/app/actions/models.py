from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


ActionKind = Literal["WRITE_BACK", "EXTERNAL"]


@dataclass(frozen=True)
class ActionDefinition:
    iri: str
    logical_key: str
    kind: ActionKind
    bound_class_iri: str | None = None
    target_property_iri: str | None = None
    function_fqn: str | None = None
    status: str = "DRAFT"
    input_schema: dict[str, str] = field(default_factory=dict)
    output_schema: dict[str, str] = field(default_factory=dict)
    approval_policy: str = "HumanConfirm"
    timeout_seconds: int = 60
    max_attempts: int = 1
    idempotency_strategy: str = "REQUEST_KEY"


@dataclass(frozen=True)
class WriteBackTarget:
    action_iri: str
    class_iri: str
    property_iri: str
    table_fqn: tuple[str, str, str]
    key_column: str
    value_column: str
    key_alias: str
    value_alias: str
    subject_template: str
    datatype_iri: str | None


@dataclass(frozen=True)
class PropertyClassification:
    class_iri: str
    property_iri: str
    writable: bool
    reasons: tuple[str, ...]
    action_iri: str | None = None
    target: WriteBackTarget | None = None


@dataclass(frozen=True)
class PrepareTokenPayload:
    prepare_id: str
    action_iri: str
    action_kind: ActionKind
    subject_iri: str
    effective_user: str
    params_hash: str
    preview_hash: str
    old_value_hash: str
    issued_at: int
    expires_at: int
    catalog_fingerprint: str
    version: int = 2


@dataclass(frozen=True)
class ActionAuditRow:
    phase: str
    status: str
    action_iri: str
    action_kind: ActionKind
    subject_iri: str
    prepare_id: str | None = None
    params_hash: str | None = None
    preview_hash: str | None = None
    old_value_hash: str | None = None
    idempotency_key: str | None = None
    source_table: str | None = None
    source_key_column: str | None = None
    source_value_column: str | None = None
    old_value: object | None = None
    new_value: object | None = None
    params_json: str | None = None
    preview_json: str | None = None
    result_json: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ActionAuditRecord:
    audit_id: str
    row: ActionAuditRow
    effective_user: str


class PrepareActionRequest(BaseModel):
    action_iri: str
    subject_iri: str
    params: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ConfirmActionRequest(BaseModel):
    preparation_token: str


class PrepareActionResponse(BaseModel):
    prepare_id: str
    action_iri: str
    action_kind: ActionKind
    subject_iri: str
    preview: dict[str, object]
    expires_at: str
    preparation_token: str


class ConfirmActionResponse(BaseModel):
    action_iri: str
    status: str
    audit_ids: list[str]
    result: dict[str, object]
