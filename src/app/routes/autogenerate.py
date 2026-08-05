"""Async R2RML autogenerate jobs via Foundation Model API (OBO)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from databricks.sdk.service.catalog import TableInfo
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from obo import get_obo_client_from_token, get_workspace_host

logger = logging.getLogger(__name__)
router = APIRouter()

JOB_TTL_SECONDS = 3600


@dataclass
class TableError:
    """A failed table, identified by its fully qualified name or `*` for a
    failure that belongs to no single table.

    The name is what the client sends back as `retryErrors[].table`, and the
    retry filter matches it against fully qualified names from the listing.
    """

    table: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"table": self.table, "error": self.error}


@dataclass
class AutogenerateJob:
    job_id: str
    status: str = "running"
    tables_total: int = 0
    tables_completed: int = 0
    current_table: str | None = None
    turtle: str = ""
    errors: list[TableError] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    _success_count: int = 0

    def to_response(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "status": self.status,
            "tablesTotal": self.tables_total,
            "tablesCompleted": self.tables_completed,
            "currentTable": self.current_table,
            "turtle": self.turtle or None,
            "errors": [e.to_dict() for e in self.errors],
        }


_jobs: dict[str, AutogenerateJob] = {}


def _purge_expired_jobs() -> None:
    now = time.time()
    expired = [
        jid for jid, job in _jobs.items() if now - job.created_at > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del _jobs[jid]


class AutogenerateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str = Field(..., pattern="^(table|schema)$")
    catalog: str
    uc_schema: str = Field(..., alias="schema")
    table: str | None = None
    prefixes: dict[str, str] = Field(default_factory=dict)
    ontologyTurtle: str = ""
    mappingTurtle: str = ""
    retryErrors: list[dict[str, str]] | None = None


def _prefix_block(prefixes: dict[str, str]) -> str:
    lines = []
    for prefix, iri in sorted(prefixes.items()):
        lines.append(f"@prefix {prefix}: <{iri}> .")
    return "\n".join(lines)


def _fragment_iri_from_fqn(fqn: str) -> str:
    """Turtle IRI fragment for a fully qualified table name, percent-encoded.

    Using the whole FQN stops tables of the same name in different catalogs or
    schemas from claiming the same fragment. `safe=""` leaves only the RFC 3986
    unreserved set (letters, digits and `-._~`) as-is, so `main.sales.orders`
    survives intact, while characters Unity Catalog permits in quoted
    identifiers but a Turtle `<#...>` IRI forbids — spaces, `#`, `<`, `>`, `"`,
    `{`, `}`, `|`, `^`, backtick and backslash — are escaped.
    """
    return quote(fqn or "", safe="")


def _column_lines(info: TableInfo) -> list[str]:
    lines: list[str] = []
    for col in info.columns or []:
        line = f"- {col.name}: {col.type_name or col.type_text or 'unknown'}"
        if col.comment:
            line += f" — {col.comment}"
        lines.append(line)
    return lines


def _primary_key_columns(info: TableInfo) -> list[str]:
    """Primary key columns as declared in Unity Catalog."""
    for constraint in info.table_constraints or []:
        pk = constraint.primary_key_constraint
        if pk is not None and pk.child_columns:
            return list(pk.child_columns)
    return []


def _join_lines(info: TableInfo, generation_set: AbstractSet[str]) -> list[str]:
    """Join descriptions for Unity Catalog declared foreign keys of this table.

    A join is described only when its parent table is itself in the generation
    set, so the parent's TriplesMap exists to be referenced. A generation set of
    one table therefore never has joins to describe.

    Unity Catalog reports `parent_table` as a fully qualified name, which is
    matched exactly against the generation set. A parent name that is not fully
    qualified matches nothing and its join is dropped; the parent's catalog and
    schema are never assumed from the child's.
    """
    if len(generation_set) <= 1:
        return []

    lines: list[str] = []
    for constraint in info.table_constraints or []:
        fk = constraint.foreign_key_constraint
        if fk is None or not fk.child_columns or not fk.parent_columns:
            continue
        parent_fqn = fk.parent_table
        if parent_fqn not in generation_set:
            logger.debug(
                "Dropping foreign key on %s: parent %r is not in the generation set",
                info.full_name,
                parent_fqn,
            )
            continue
        child = ", ".join(fk.child_columns)
        parent_columns = ", ".join(f"{parent_fqn}.{col}" for col in fk.parent_columns)
        parent_fragment = _fragment_iri_from_fqn(parent_fqn)
        lines.append(f"- {child} → {parent_columns} (#{parent_fragment})")
    return lines


def _build_prompt(
    info: TableInfo,
    generation_set: AbstractSet[str],
    *,
    prefixes: dict[str, str],
    ontology_turtle: str,
    mapping_turtle: str,
    prior_error: str | None,
) -> list[dict[str, str]]:
    system = (
        "You are an R2RML mapping expert. Output ONLY valid Turtle R2RML for a single "
        "rr:TriplesMap. Name it with exactly the fragment IRI given in the user "
        "message under 'Use fragment IRI'; do not derive a fragment yourself and do "
        "not alter the one supplied. "
        "Include rr:logicalTable with rr:tableName, rr:subjectMap with rr:template and "
        "rr:class, and rr:predicateObjectMap entries for each column (rr:column object maps). "
        "You may include rr:datatype on columns when appropriate. "
        "For listed joins only, also emit rr:predicateObjectMap entries that use "
        "rr:parentTriplesMap and rr:joinCondition (predicate from ontology/naming); "
        "do not invent joins that are not listed. Still emit literal rr:column object "
        "maps for FK columns as well. "
        "Do not wrap output in markdown fences. Output only Turtle."
    )

    user_parts = [
        f"Generate an R2RML TriplesMap for table: {info.full_name}",
        f"Use fragment IRI: <#{_fragment_iri_from_fqn(info.full_name)}>",
    ]

    if info.comment:
        user_parts.extend(["", f"Table description: {info.comment}"])

    pk_columns = _primary_key_columns(info)
    if pk_columns:
        user_parts.extend(["", f"Primary key: {', '.join(pk_columns)}"])

    join_lines = _join_lines(info, generation_set)
    if join_lines:
        user_parts.extend(["", "Joins from this table:", *join_lines])

    column_lines = _column_lines(info)
    if column_lines:
        user_parts.extend(["", "Columns:", *column_lines])

    if prefixes:
        user_parts.extend(["", "Prefix declarations to use:", _prefix_block(prefixes)])

    if ontology_turtle.strip():
        user_parts.extend(
            [
                "",
                "Reference ontology (for class/property IRIs):",
                ontology_turtle[:8000],
            ]
        )

    if mapping_turtle.strip():
        user_parts.extend(
            [
                "",
                "Existing mapping context (match style, avoid duplicate map IDs):",
                mapping_turtle[:8000],
            ]
        )

    if prior_error:
        user_parts.extend(
            [
                "",
                f"Previous attempt failed with: {prior_error}",
                "Fix the issues and try again.",
            ]
        )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _extract_turtle(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _light_validate_turtle(turtle: str) -> None:
    if "rr:TriplesMap" not in turtle and "a rr:TriplesMap" not in turtle:
        raise ValueError("Response does not contain an rr:TriplesMap")
    if "rr:logicalTable" not in turtle:
        raise ValueError("Response missing rr:logicalTable")
    if len(turtle) < 20:
        raise ValueError("Response too short to be valid R2RML")


def _resolve_generation_set(
    client: Any, body: AutogenerateRequest
) -> tuple[dict[str, TableInfo], list[TableError]]:
    """Tables to generate mappings for, keyed by fully qualified name.

    Unity Catalog does not return `table_constraints` from `tables.list`, so the
    listing only enumerates the tables and each one is fetched once with
    `tables.get` to pick up its declared keys. A table that cannot be fetched is
    returned as its own error so it does not sink the rest of the run.

    Table mode is the one place a name arrives in parts, so the fully qualified
    name is assembled there and nowhere else.
    """
    if body.mode == "table":
        if not body.table:
            raise ValueError("table is required when mode is 'table'")
        fqns = [f"{body.catalog}.{body.uc_schema}.{body.table}"]
    else:
        fqns = sorted(
            t.full_name
            for t in client.tables.list(
                catalog_name=body.catalog, schema_name=body.uc_schema
            )
            if t.full_name
        )
        if body.retryErrors:
            retry_set = {e.get("table", "") for e in body.retryErrors}
            fqns = [fqn for fqn in fqns if fqn in retry_set]

    generation_set: dict[str, TableInfo] = {}
    errors: list[TableError] = []
    for fqn in fqns:
        try:
            generation_set[fqn] = client.tables.get(fqn)
        except Exception as exc:
            logger.warning("Autogenerate could not fetch %s: %s", fqn, exc)
            errors.append(TableError(table=fqn, error=str(exc)))
    return generation_set, errors


def _fm_client(token: str) -> OpenAI:
    host = get_workspace_host()
    return OpenAI(
        api_key=token,
        base_url=f"https://{host}/serving-endpoints",
    )


async def _request_mapping(
    token: str, fm_model_name: str, messages: list[dict[str, str]]
) -> str:
    fm = _fm_client(token)

    def _call() -> str:
        response = fm.chat.completions.create(
            model=fm_model_name,
            messages=messages,
            max_tokens=8000,
        )
        content = response.choices[0].message.content or ""
        turtle = _extract_turtle(content)
        _light_validate_turtle(turtle)
        return turtle

    return await asyncio.to_thread(_call)


async def _run_job(
    job_id: str,
    token: str,
    fm_model_name: str,
    body: AutogenerateRequest,
) -> None:
    job = _jobs[job_id]
    prior_errors: dict[str, str] = {}
    if body.retryErrors:
        for item in body.retryErrors:
            prior_errors[item.get("table", "")] = item.get("error", "")

    try:
        client = get_obo_client_from_token(token)
        generation_set, errors = await asyncio.to_thread(
            _resolve_generation_set, client, body
        )
    except Exception as exc:
        job.status = "failed"
        job.errors.append(TableError(table="*", error=str(exc)))
        return

    # Tables that could not be fetched are already accounted for as processed.
    job.tables_total = len(generation_set) + len(errors)
    job.tables_completed = len(errors)
    if not job.tables_total:
        job.status = "failed"
        job.errors.append(TableError(table="*", error="No tables to process"))
        return

    accumulated: list[str] = []

    for fqn, info in generation_set.items():
        job.current_table = fqn
        try:
            messages = _build_prompt(
                info,
                generation_set.keys(),
                prefixes=body.prefixes,
                ontology_turtle=body.ontologyTurtle,
                mapping_turtle=body.mappingTurtle + "\n".join(accumulated),
                prior_error=prior_errors.get(fqn),
            )
            turtle = await _request_mapping(token, fm_model_name, messages)
            accumulated.append(turtle)
            job._success_count += 1
        except Exception as exc:
            logger.warning("Autogenerate failed for %s: %s", fqn, exc)
            errors.append(TableError(table=fqn, error=str(exc)))
        finally:
            job.tables_completed += 1

    job.current_table = None
    job.errors = errors
    job.turtle = "\n\n".join(accumulated)

    if job._success_count == 0:
        job.status = "failed"
    elif errors:
        job.status = "partial"
    else:
        job.status = "complete"


@router.post("")
async def submit_autogenerate(
    request: Request,
    body: AutogenerateRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    _purge_expired_jobs()
    token = request.headers.get("x-forwarded-access-token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing user authorization token")

    job_id = str(uuid.uuid4())
    job = AutogenerateJob(job_id=job_id)
    _jobs[job_id] = job

    settings = request.app.state.settings
    background_tasks.add_task(_run_job, job_id, token, settings.fm_model_name, body)
    return {"jobId": job_id}


@router.get("/{job_id}")
async def poll_autogenerate(job_id: str) -> dict[str, Any]:
    _purge_expired_jobs()
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return job.to_response()
