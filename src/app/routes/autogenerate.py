"""Async R2RML autogenerate jobs via Foundation Model API (OBO)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from obo import get_obo_client_from_token, get_workspace_host

logger = logging.getLogger(__name__)
router = APIRouter()

JOB_TTL_SECONDS = 3600


@dataclass
class TableError:
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


def _build_prompt(
    *,
    catalog: str,
    schema: str,
    table: str,
    columns: list[dict[str, str | None]],
    prefixes: dict[str, str],
    ontology_turtle: str,
    mapping_turtle: str,
    prior_error: str | None,
) -> list[dict[str, str]]:
    col_lines = []
    for col in columns:
        line = f"- {col['name']}: {col['type']}"
        if col.get("comment"):
            line += f" — {col['comment']}"
        col_lines.append(line)

    full_table = f"{catalog}.{schema}.{table}"
    fragment = table[:1].upper() + table[1:]
    if "_" in fragment:
        parts = fragment.split("_")
        fragment = "".join(p[:1].upper() + p[1:] for p in parts if p)

    system = (
        "You are an R2RML mapping expert. Output ONLY valid Turtle R2RML for a single "
        "rr:TriplesMap. Use fragment IRIs like <#PascalCase> derived from the table name. "
        "Include rr:logicalTable with rr:tableName, rr:subjectMap with rr:template and "
        "rr:class, and rr:predicateObjectMap entries for each column (rr:column object maps). "
        "You may include rr:datatype on columns when appropriate. "
        "Do not wrap output in markdown fences. Output only Turtle."
    )

    user_parts = [
        f"Generate an R2RML TriplesMap for table: {full_table}",
        f"Use fragment IRI: <#{fragment}>",
        "",
        "Columns:",
        *col_lines,
        "",
        "Prefix declarations to use:",
        _prefix_block(prefixes) if prefixes else "(none provided)",
    ]

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


def _resolve_tables(
    client: Any,
    catalog: str,
    schema: str,
    table: str | None,
    mode: str,
    retry_errors: list[dict[str, str]] | None,
) -> list[str]:
    if mode == "table":
        if not table:
            raise ValueError("table is required when mode is 'table'")
        return [table]

    all_tables = sorted(
        t.name
        for t in client.tables.list(catalog_name=catalog, schema_name=schema)
        if t.name
    )
    if retry_errors:
        retry_set = {e.get("table", "") for e in retry_errors}
        return [t for t in all_tables if t in retry_set]
    return all_tables


def _fm_client(token: str) -> OpenAI:
    host = get_workspace_host()
    return OpenAI(
        api_key=token,
        base_url=f"https://{host}/serving-endpoints",
    )


async def _generate_table_mapping(
    token: str,
    fm_model_name: str,
    catalog: str,
    schema: str,
    table: str,
    prefixes: dict[str, str],
    ontology_turtle: str,
    mapping_context: str,
    prior_error: str | None,
) -> str:
    client = get_obo_client_from_token(token)
    full_name = f"{catalog}.{schema}.{table}"
    info = client.tables.get(full_name)
    columns: list[dict[str, str | None]] = []
    for col in info.columns or []:
        entry: dict[str, str | None] = {
            "name": col.name,
            "type": col.type_name or col.type_text or "unknown",
        }
        if col.comment:
            entry["comment"] = col.comment
        columns.append(entry)

    messages = _build_prompt(
        catalog=catalog,
        schema=schema,
        table=table,
        columns=columns,
        prefixes=prefixes,
        ontology_turtle=ontology_turtle,
        mapping_turtle=mapping_context,
        prior_error=prior_error,
    )

    fm = _fm_client(token)

    def _call() -> str:
        response = fm.chat.completions.create(
            model=fm_model_name,
            messages=messages,
            max_tokens=4096,
            temperature=0.2,
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
        tables = _resolve_tables(
            client,
            body.catalog,
            body.uc_schema,
            body.table,
            body.mode,
            body.retryErrors,
        )
    except Exception as exc:
        job.status = "failed"
        job.errors.append(TableError(table="*", error=str(exc)))
        return

    job.tables_total = len(tables)
    if not tables:
        job.status = "failed"
        job.errors.append(TableError(table="*", error="No tables to process"))
        return

    accumulated: list[str] = []
    errors: list[TableError] = []

    for table_name in tables:
        job.current_table = table_name
        try:
            turtle = await _generate_table_mapping(
                token,
                fm_model_name,
                body.catalog,
                body.uc_schema,
                table_name,
                body.prefixes,
                body.ontologyTurtle,
                body.mappingTurtle + "\n".join(accumulated),
                prior_errors.get(table_name),
            )
            accumulated.append(turtle)
            job._success_count += 1
        except Exception as exc:
            logger.warning("Autogenerate failed for %s: %s", table_name, exc)
            errors.append(TableError(table=table_name, error=str(exc)))
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
