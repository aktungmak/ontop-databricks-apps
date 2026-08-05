"""Unit tests for autogenerate prompts built from Unity Catalog metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Set as AbstractSet
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from routes.autogenerate import (
    AutogenerateJob,
    AutogenerateRequest,
    _build_prompt,
    _fragment_iri_from_fqn,
    _join_lines,
    _jobs,
    _primary_key_columns,
    _resolve_generation_set,
    _run_job,
)


def _column(name: str, type_name: str = "STRING", comment: str | None = None) -> Any:
    """Stand-in for databricks.sdk.service.catalog.ColumnInfo."""
    return SimpleNamespace(
        name=name, type_name=type_name, type_text=None, comment=comment
    )


def _pk(*child_columns: str) -> Any:
    """TableConstraint carrying a PrimaryKeyConstraint."""
    return SimpleNamespace(
        primary_key_constraint=SimpleNamespace(
            name="pk", child_columns=list(child_columns)
        ),
        foreign_key_constraint=None,
        named_table_constraint=None,
    )


def _fk(child_columns: list[str], parent_table: str, parent_columns: list[str]) -> Any:
    """TableConstraint carrying a ForeignKeyConstraint."""
    return SimpleNamespace(
        primary_key_constraint=None,
        foreign_key_constraint=SimpleNamespace(
            name="fk",
            child_columns=child_columns,
            parent_table=parent_table,
            parent_columns=parent_columns,
        ),
        named_table_constraint=None,
    )


def _named_constraint() -> Any:
    """TableConstraint that is neither a primary nor a foreign key."""
    return SimpleNamespace(
        primary_key_constraint=None,
        foreign_key_constraint=None,
        named_table_constraint=SimpleNamespace(name="positive_total"),
    )


def _table_info(
    fqn: str,
    *,
    columns: list[Any] | None = None,
    constraints: list[Any] | None = None,
    comment: str | None = None,
) -> Any:
    """Stand-in for databricks.sdk.service.catalog.TableInfo."""
    catalog, schema, table = fqn.split(".")
    return SimpleNamespace(
        full_name=fqn,
        catalog_name=catalog,
        schema_name=schema,
        name=table,
        comment=comment,
        columns=columns or [],
        table_constraints=constraints or [],
    )


def _user_message(messages: list[dict[str, str]]) -> str:
    return messages[1]["content"]


def _prompt(
    info: Any, generation_set: AbstractSet[str], **overrides: Any
) -> list[dict[str, str]]:
    kwargs: dict[str, Any] = {
        "prefixes": {},
        "ontology_turtle": "",
        "mapping_turtle": "",
        "prior_error": None,
    }
    kwargs.update(overrides)
    return _build_prompt(info, generation_set, **kwargs)


def test_fragment_iri_keeps_the_whole_qualified_name() -> None:
    assert _fragment_iri_from_fqn("main.sales.orders") == "main.sales.orders"
    assert _fragment_iri_from_fqn("main.sales.order_items") == "main.sales.order_items"
    assert _fragment_iri_from_fqn("") == ""


def test_fragment_iri_distinguishes_same_table_name_across_schemas() -> None:
    eu = _fragment_iri_from_fqn("main.eu_sales.orders")
    us = _fragment_iri_from_fqn("main.us_sales.orders")

    assert eu != us
    assert _fragment_iri_from_fqn("prod.sales.orders") != _fragment_iri_from_fqn(
        "dev.sales.orders"
    )


def test_fragment_iri_percent_encodes_characters_illegal_in_an_iri() -> None:
    assert _fragment_iri_from_fqn("main.sales.order items") == (
        "main.sales.order%20items"
    )
    assert _fragment_iri_from_fqn("main.sales.a#b") == "main.sales.a%23b"
    assert _fragment_iri_from_fqn('main.sales.a<b>c"d') == "main.sales.a%3Cb%3Ec%22d"
    assert _fragment_iri_from_fqn("main.sales.a{b}c|d^e`f\\g") == (
        "main.sales.a%7Bb%7Dc%7Cd%5Ee%60f%5Cg"
    )


def test_declared_fk_produces_join_line() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("id"), _column("customer_id"), _column("region_id")],
        constraints=[
            _pk("id"),
            _fk(["customer_id"], "main.sales.customers", ["id"]),
        ],
    )
    generation_set = {
        "main.sales.orders",
        "main.sales.customers",
        "main.sales.regions",
    }

    lines = _join_lines(orders, generation_set)

    assert lines == ["- customer_id → main.sales.customers.id (#main.sales.customers)"]
    # region_id is not a declared FK — no naming heuristic.
    assert all("region_id" not in line for line in lines)


def test_declared_fk_to_another_schema_keeps_the_parent_qualification() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("customer_id")],
        constraints=[_fk(["customer_id"], "other.crm.customers", ["id"])],
    )

    lines = _join_lines(orders, {"main.sales.orders", "other.crm.customers"})

    assert lines == ["- customer_id → other.crm.customers.id (#other.crm.customers)"]


def test_composite_declared_fk_lists_all_columns() -> None:
    line_items = _table_info(
        "main.sales.line_items",
        columns=[_column("order_id"), _column("order_line")],
        constraints=[
            _fk(
                ["order_id", "order_line"],
                "main.sales.order_lines",
                ["id", "line_number"],
            )
        ],
    )

    lines = _join_lines(line_items, {"main.sales.line_items", "main.sales.order_lines"})

    assert lines == [
        "- order_id, order_line → main.sales.order_lines.id, "
        "main.sales.order_lines.line_number (#main.sales.order_lines)"
    ]


def test_no_declared_fks_produces_no_join_lines() -> None:
    """Column-name patterns alone never produce joins."""
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("id"), _column("customer_id")],
        constraints=[_pk("id")],
    )

    assert _join_lines(orders, {"main.sales.orders", "main.sales.customers"}) == []


def test_parent_outside_generation_set_dropped() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("id"), _column("customer_id")],
        constraints=[_pk("id"), _fk(["customer_id"], "main.sales.customers", ["id"])],
    )

    # Multi-table set that excludes the declared parent → join dropped.
    assert _join_lines(orders, {"main.sales.orders", "main.sales.regions"}) == []


def test_parent_table_that_is_not_fully_qualified_is_dropped() -> None:
    """An unqualified parent matches nothing; its catalog/schema are not guessed."""
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("customer_id"), _column("region_id")],
        constraints=[
            _fk(["customer_id"], "customers", ["id"]),
            _fk(["region_id"], "sales.regions", ["id"]),
        ],
    )

    assert (
        _join_lines(
            orders,
            {"main.sales.orders", "main.sales.customers", "main.sales.regions"},
        )
        == []
    )


def test_single_table_generation_set_emits_no_joins() -> None:
    """Even self-FKs are omitted when the generation set has one table."""
    employees = _table_info(
        "main.hr.employees",
        columns=[_column("id"), _column("manager_id")],
        constraints=[_pk("id"), _fk(["manager_id"], "main.hr.employees", ["id"])],
    )

    assert _join_lines(employees, {"main.hr.employees"}) == []
    # Multi-table sets still describe declared self-FKs.
    assert _join_lines(employees, {"main.hr.employees", "main.hr.departments"}) == [
        "- manager_id → main.hr.employees.id (#main.hr.employees)"
    ]


def test_declared_pk_columns_returned() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("order_id"), _column("line_number")],
        constraints=[_named_constraint(), _pk("order_id", "line_number")],
    )

    assert _primary_key_columns(orders) == ["order_id", "line_number"]


def test_id_column_without_constraint_is_not_a_primary_key() -> None:
    """A column named `id` is never inferred as a PK — only UC declarations count."""
    orders = _table_info("main.sales.orders", columns=[_column("id")])

    assert _primary_key_columns(orders) == []


def test_prompt_includes_joins_and_parent_triplesmap_instructions() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("id"), _column("customer_id")],
        constraints=[_pk("id"), _fk(["customer_id"], "main.sales.customers", ["id"])],
    )

    messages = _prompt(
        orders,
        {"main.sales.orders", "main.sales.customers"},
        prefixes={"ex": "http://example.org/"},
    )
    system = messages[0]["content"]
    user = _user_message(messages)

    assert "rr:parentTriplesMap" in system
    assert "rr:joinCondition" in system
    assert "do not invent joins" in system
    # The fragment is supplied, not derived by the model.
    assert "exactly the fragment IRI given in the user message" in system
    assert "PascalCase" not in system
    assert "Generate an R2RML TriplesMap for table: main.sales.orders" in user
    assert "Use fragment IRI: <#main.sales.orders>" in user
    assert "Primary key: id" in user
    assert "Joins from this table:" in user
    assert "- customer_id → main.sales.customers.id (#main.sales.customers)" in user
    assert "@prefix ex: <http://example.org/> ." in user
    assert "[declared]" not in user
    assert "[inferred]" not in user


def test_prompt_fragment_is_derived_from_the_full_name() -> None:
    eu = _table_info("main.eu_sales.orders", columns=[_column("id")])
    us = _table_info("main.us_sales.orders", columns=[_column("id")])
    spaced = _table_info("main.sales.order items", columns=[_column("id")])

    assert "Use fragment IRI: <#main.eu_sales.orders>" in _user_message(
        _prompt(eu, {"main.eu_sales.orders"})
    )
    assert "Use fragment IRI: <#main.us_sales.orders>" in _user_message(
        _prompt(us, {"main.us_sales.orders"})
    )
    assert "Use fragment IRI: <#main.sales.order%20items>" in _user_message(
        _prompt(spaced, {"main.sales.order items"})
    )


def test_prompt_omits_join_section_without_declared_fks() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("id")],
        constraints=[_pk("id")],
    )

    user = _user_message(_prompt(orders, {"main.sales.orders", "main.sales.customers"}))

    assert "Joins from this table:" not in user
    assert "→" not in user


def test_prompt_omits_primary_key_section_when_none_declared() -> None:
    orders = _table_info("main.sales.orders", columns=[_column("id"), _column("name")])

    user = _user_message(_prompt(orders, {"main.sales.orders"}))

    assert "Primary key" not in user
    assert "(none declared)" not in user
    assert "id-like" not in user
    # Columns are still described.
    assert "- id: STRING" in user


def test_prompt_omits_optional_sections_when_metadata_absent() -> None:
    orders = _table_info("main.sales.orders")

    user = _user_message(_prompt(orders, {"main.sales.orders"}))

    assert "Columns:" not in user
    assert "Table description" not in user
    assert "Prefix declarations to use:" not in user
    assert "(none)" not in user
    assert "(none provided)" not in user


def test_prompt_includes_comments_when_present() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("total", "DECIMAL", comment="Order total in USD")],
        comment="One row per customer order",
    )

    user = _user_message(_prompt(orders, {"main.sales.orders"}))

    assert "Table description: One row per customer order" in user
    assert "- total: DECIMAL — Order total in USD" in user


def test_prompt_includes_context_and_prior_error() -> None:
    orders = _table_info("main.sales.orders", columns=[_column("id")])

    user = _user_message(
        _prompt(
            orders,
            {"main.sales.orders"},
            ontology_turtle="ex:Order a owl:Class .",
            mapping_turtle="<#main.sales.customers> a rr:TriplesMap .",
            prior_error="missing rr:logicalTable",
        )
    )

    assert "Reference ontology (for class/property IRIs):" in user
    assert "ex:Order a owl:Class ." in user
    assert "Existing mapping context" in user
    assert "<#main.sales.customers> a rr:TriplesMap ." in user
    assert "Previous attempt failed with: missing rr:logicalTable" in user


def _listed_table(fqn: str | None) -> Any:
    """Listing stand-in carrying the fully qualified name Unity Catalog reports.

    `tables.list` never populates `table_constraints`, so a listing entry offers
    nothing but names and every constraint must come from `tables.get`.
    """
    if fqn is None:
        return SimpleNamespace(full_name=None, name=None)
    return SimpleNamespace(full_name=fqn, name=fqn.rsplit(".", 1)[-1])


def _client(*, listed: list[str | None], fetched: dict[str, Any]) -> Any:
    """Client whose `tables.list` yields fully qualified names, as UC does."""
    client = MagicMock()
    client.tables.list.return_value = [_listed_table(fqn) for fqn in listed]

    def _get(fqn: str) -> Any:
        info = fetched.get(fqn)
        if info is None:
            raise RuntimeError(f"cannot fetch {fqn}")
        return info

    client.tables.get.side_effect = _get
    return client


def test_generation_set_table_mode_fetches_the_single_table() -> None:
    orders = _table_info("main.sales.orders", constraints=[_pk("id")])
    client = _client(listed=[], fetched={"main.sales.orders": orders})
    body = AutogenerateRequest(
        mode="table", catalog="main", schema="sales", table="orders"
    )

    generation_set, errors = _resolve_generation_set(client, body)

    assert generation_set == {"main.sales.orders": orders}
    assert errors == []
    client.tables.list.assert_not_called()
    client.tables.get.assert_called_once_with("main.sales.orders")


def test_generation_set_schema_mode_maps_every_table_to_its_fetched_info() -> None:
    customers = _table_info("main.sales.customers", constraints=[_pk("id")])
    orders = _table_info(
        "main.sales.orders",
        constraints=[_fk(["customer_id"], "main.sales.customers", ["id"])],
    )
    client = _client(
        listed=["main.sales.orders", "main.sales.customers", None],
        fetched={"main.sales.orders": orders, "main.sales.customers": customers},
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    generation_set, errors = _resolve_generation_set(client, body)

    assert list(generation_set) == ["main.sales.customers", "main.sales.orders"]
    assert generation_set["main.sales.orders"] is orders
    assert errors == []
    # Constraints only exist on the fetched objects, so joins survive.
    assert _join_lines(orders, generation_set.keys()) == [
        "- customer_id → main.sales.customers.id (#main.sales.customers)"
    ]


def test_generation_set_schema_mode_takes_the_listed_full_name_verbatim() -> None:
    """The listing's `full_name` is used as-is, never rebuilt from request parts.

    Unity Catalog quotes an identifier that needs it, so interpolating
    `{catalog}.{schema}.{name}` would name a different — and unfetchable —
    table.
    """
    quoted = _table_info("main.sales.orders")
    quoted.full_name = "main.sales.`order items`"
    quoted.name = "order items"
    client = _client(
        listed=["main.sales.`order items`"],
        fetched={"main.sales.`order items`": quoted},
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    generation_set, errors = _resolve_generation_set(client, body)

    assert list(generation_set) == ["main.sales.`order items`"]
    assert errors == []
    client.tables.get.assert_called_once_with("main.sales.`order items`")


def test_generation_set_skips_a_listing_entry_without_a_full_name() -> None:
    """A listing entry that reports no `full_name` is dropped, not reassembled."""
    client = _client(listed=[], fetched={})
    client.tables.list.return_value = [SimpleNamespace(full_name=None, name="orders")]
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    generation_set, errors = _resolve_generation_set(client, body)

    assert generation_set == {}
    assert errors == []
    client.tables.get.assert_not_called()


def test_generation_set_fetches_each_table_exactly_once() -> None:
    client = _client(
        listed=["main.sales.orders", "main.sales.customers"],
        fetched={
            "main.sales.orders": _table_info("main.sales.orders"),
            "main.sales.customers": _table_info("main.sales.customers"),
        },
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    _resolve_generation_set(client, body)

    assert client.tables.get.call_count == 2


def test_generation_set_retry_errors_narrow_to_failing_tables() -> None:
    regions = _table_info("main.sales.regions")
    client = _client(
        listed=["main.sales.orders", "main.sales.customers", "main.sales.regions"],
        fetched={
            "main.sales.orders": _table_info("main.sales.orders"),
            "main.sales.customers": _table_info("main.sales.customers"),
            "main.sales.regions": regions,
        },
    )
    body = AutogenerateRequest(
        mode="schema",
        catalog="main",
        schema="sales",
        retryErrors=[{"table": "main.sales.regions", "error": "boom"}],
    )

    generation_set, errors = _resolve_generation_set(client, body)

    assert generation_set == {"main.sales.regions": regions}
    assert errors == []
    client.tables.get.assert_called_once_with("main.sales.regions")


def test_generation_set_retry_errors_match_on_the_qualified_name() -> None:
    """A short name is ambiguous across schemas and matches nothing."""
    client = _client(
        listed=["main.sales.regions"],
        fetched={"main.sales.regions": _table_info("main.sales.regions")},
    )
    body = AutogenerateRequest(
        mode="schema",
        catalog="main",
        schema="sales",
        retryErrors=[{"table": "regions", "error": "boom"}],
    )

    generation_set, errors = _resolve_generation_set(client, body)

    assert generation_set == {}
    assert errors == []
    client.tables.get.assert_not_called()


def test_generation_set_reports_an_unfetchable_table_and_keeps_the_others() -> None:
    customers = _table_info("main.sales.customers")
    orders = _table_info("main.sales.orders")
    client = _client(
        listed=["main.sales.orders", "main.sales.customers", "main.sales.regions"],
        fetched={"main.sales.orders": orders, "main.sales.customers": customers},
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    generation_set, errors = _resolve_generation_set(client, body)

    assert list(generation_set) == ["main.sales.customers", "main.sales.orders"]
    assert [(e.table, e.error) for e in errors] == [
        ("main.sales.regions", "cannot fetch main.sales.regions")
    ]


def test_generation_set_table_mode_requires_a_table() -> None:
    body = AutogenerateRequest(mode="table", catalog="main", schema="sales")

    with pytest.raises(ValueError):
        _resolve_generation_set(MagicMock(), body)


def test_prompt_accepts_the_generation_set_dict_keys() -> None:
    orders = _table_info(
        "main.sales.orders",
        columns=[_column("customer_id")],
        constraints=[_fk(["customer_id"], "main.sales.customers", ["id"])],
    )
    generation_set = {
        "main.sales.orders": orders,
        "main.sales.customers": _table_info("main.sales.customers"),
    }

    user = _user_message(_prompt(orders, generation_set.keys()))

    assert "- customer_id → main.sales.customers.id (#main.sales.customers)" in user


async def _fake_mapping(
    token: str, fm_model_name: str, messages: list[dict[str, str]]
) -> str:
    table = _user_message(messages).split("\n")[0].rsplit(": ", 1)[-1]
    return f"<#{table}> a rr:TriplesMap ."


def _run(
    client: Any, body: AutogenerateRequest, mapping: Any = _fake_mapping
) -> AutogenerateJob:
    job = AutogenerateJob(job_id="job-1")
    _jobs["job-1"] = job
    get_client = "routes.autogenerate.get_obo_client_from_token"
    try:
        with (
            patch(get_client, return_value=client),
            patch("routes.autogenerate._request_mapping", new=mapping),
        ):
            asyncio.run(_run_job("job-1", "tok", "test-model", body))
    finally:
        del _jobs["job-1"]
    return job


def test_job_fetches_each_table_once_and_completes() -> None:
    client = _client(
        listed=["main.sales.orders", "main.sales.customers"],
        fetched={
            "main.sales.orders": _table_info("main.sales.orders"),
            "main.sales.customers": _table_info("main.sales.customers"),
        },
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    job = _run(client, body)

    assert job.status == "complete"
    assert (job.tables_total, job.tables_completed) == (2, 2)
    assert job.errors == []
    assert client.tables.get.call_count == 2
    assert "<#main.sales.orders> a rr:TriplesMap ." in job.turtle
    assert "<#main.sales.customers> a rr:TriplesMap ." in job.turtle


def test_job_reports_an_unfetchable_table_but_maps_the_rest() -> None:
    client = _client(
        listed=["main.sales.orders", "main.sales.customers", "main.sales.regions"],
        fetched={
            "main.sales.orders": _table_info("main.sales.orders"),
            "main.sales.customers": _table_info("main.sales.customers"),
        },
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    job = _run(client, body)

    assert job.status == "partial"
    assert (job.tables_total, job.tables_completed) == (3, 3)
    assert [e.table for e in job.errors] == ["main.sales.regions"]
    assert "<#main.sales.orders> a rr:TriplesMap ." in job.turtle
    assert "<#main.sales.customers> a rr:TriplesMap ." in job.turtle
    assert "regions" not in job.turtle


def test_job_reports_a_generation_failure_with_the_qualified_name() -> None:
    client = _client(
        listed=["main.sales.orders"],
        fetched={"main.sales.orders": _table_info("main.sales.orders")},
    )
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    async def _fail(token: str, model: str, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("model refused")

    job = _run(client, body, mapping=_fail)

    assert job.status == "failed"
    assert [(e.table, e.error) for e in job.errors] == [
        ("main.sales.orders", "model refused")
    ]
    assert job.current_table is None


def test_job_passes_the_prior_error_matched_on_the_qualified_name() -> None:
    seen: list[str] = []

    async def _capture(token: str, model: str, messages: list[dict[str, str]]) -> str:
        seen.append(_user_message(messages))
        return await _fake_mapping(token, model, messages)

    client = _client(
        listed=["main.sales.orders"],
        fetched={"main.sales.orders": _table_info("main.sales.orders")},
    )
    body = AutogenerateRequest(
        mode="schema",
        catalog="main",
        schema="sales",
        retryErrors=[
            {"table": "main.sales.orders", "error": "missing rr:logicalTable"}
        ],
    )

    job = _run(client, body, mapping=_capture)

    assert job.status == "complete"
    assert "Previous attempt failed with: missing rr:logicalTable" in seen[0]


def test_job_fails_when_no_table_can_be_fetched() -> None:
    client = _client(listed=["main.sales.orders"], fetched={})
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    job = _run(client, body)

    assert job.status == "failed"
    assert [e.table for e in job.errors] == ["main.sales.orders"]
    assert job.turtle == ""


def test_job_fails_with_a_wildcard_error_when_listing_fails() -> None:
    client = MagicMock()
    client.tables.list.side_effect = RuntimeError("no permission")
    body = AutogenerateRequest(mode="schema", catalog="main", schema="sales")

    job = _run(client, body)

    assert job.status == "failed"
    assert [(e.table, e.error) for e in job.errors] == [("*", "no permission")]
