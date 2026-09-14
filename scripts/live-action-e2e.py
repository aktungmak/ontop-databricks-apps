#!/usr/bin/env python3
"""Live VKG action E2E against a Databricks SQL warehouse.

This is intentionally not part of the default test suite. It creates disposable
Unity Catalog objects, forwards a real Databricks user token through the same
headers used by Databricks Apps, then verifies:

* REST action discovery, prepare, confirm
* MCP action discovery, prepare, confirm
* write-back changed the source Delta table
* external action invoked a named UC function and accepted its result envelope
* audit rows were written with current_user() attribution
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from databricks import sql as dbsql
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
import uvicorn

APP_DIR = Path(__file__).resolve().parents[1] / "src" / "app"
sys.path.insert(0, str(APP_DIR))

from actions.audit import ActionAuditLogger  # noqa: E402
from actions.catalog import ActionCatalog  # noqa: E402
from actions.routes import create_action_router  # noqa: E402
from actions.service import ActionService  # noqa: E402
from actions.tokens import PrepareTokenSigner  # noqa: E402
from config import Settings  # noqa: E402
from mcp_server import (  # noqa: E402
    McpRuntime,
    configure as configure_mcp,
    mcp,
)
from ontology_store import OntologyStore  # noqa: E402


ACTION_NS = "https://example.com/vkg-actions-e2e#"
SUBJECT_BASE = "https://example.com/vkg-actions-e2e/Supplier"


def main() -> None:
    args = _parse_args()
    token = _databricks_user_token(args.profile)
    os.environ["DATABRICKS_HOST"] = args.host

    client = _SqlClient(args.host, args.warehouse_id, token)
    catalog = args.catalog or client.scalar("SELECT current_catalog()")
    schema = args.schema
    run_id = args.run_id
    source_table = f"supplier_action_source_{run_id}"
    audit_table = f"vkg_action_audit_{run_id}"
    external_function = f"submit_supplier_review_{run_id}"

    fqn = _Fqn(catalog, schema)
    print(f"Workspace: {args.host}")
    print(f"Warehouse: {args.warehouse_id}")
    print(f"Actor: {client.scalar('SELECT current_user()')}")
    print(f"Schema: {fqn.schema_fqn}")

    try:
        _setup_uc_objects(
            client,
            fqn,
            source_table=source_table,
            audit_table=audit_table,
            external_function=external_function,
        )
        service = _action_service(
            catalog=catalog,
            schema=schema,
            source_table=source_table,
            audit_table=audit_table,
            external_function=external_function,
            warehouse_id=args.warehouse_id,
        )

        source_fqn = f"{fqn.schema_fqn}.`{source_table}`"
        audit_fqn = f"{fqn.schema_fqn}.`{audit_table}`"
        write_action_iri = f"{ACTION_NS}updateSupplierName"
        external_action_iri = f"{ACTION_NS}requestSupplierReview"
        subject_iri = f"{SUBJECT_BASE}/SUP-001"

        _run_rest_writeback(
            service=service,
            token=token,
            action_iri=write_action_iri,
            subject_iri=subject_iri,
        )
        rest_value = client.scalar(
            f"SELECT supplier_name FROM {source_fqn} WHERE supplier_id = 'SUP-001'"
        )
        assert rest_value == "REST-updated", rest_value
        print("REST write-back confirmed: supplier_name=REST-updated")

        asyncio.run(
            _run_mcp_http_writeback(
                service=service,
                token=token,
                action_iri=write_action_iri,
                subject_iri=subject_iri,
            )
        )
        mcp_value = client.scalar(
            f"SELECT supplier_name FROM {source_fqn} WHERE supplier_id = 'SUP-001'"
        )
        assert mcp_value == "MCP-HTTP-updated", mcp_value
        print("MCP HTTP write-back confirmed: supplier_name=MCP-HTTP-updated")

        asyncio.run(
            _run_mcp_http_external(
                service=service,
                token=token,
                action_iri=external_action_iri,
                subject_iri=subject_iri,
            )
        )
        print(
            f"MCP HTTP external action confirmed via {fqn.schema_fqn}.`{external_function}`"
        )

        _run_direct_mcp_writeback(
            service=service,
            token=token,
            action_iri=write_action_iri,
            subject_iri=subject_iri,
        )
        direct_mcp_value = client.scalar(
            f"SELECT supplier_name FROM {source_fqn} WHERE supplier_id = 'SUP-001'"
        )
        assert direct_mcp_value == "MCP-direct-updated", direct_mcp_value
        print(
            "MCP direct-function write-back confirmed: supplier_name=MCP-direct-updated"
        )

        audit_counts = client.fetchall(
            f"""
            SELECT phase, status, action_kind, count(*) AS rows
            FROM {audit_fqn}
            GROUP BY phase, status, action_kind
            ORDER BY phase, status, action_kind
            """
        )
        print("Audit rows:")
        for row in audit_counts:
            print(f"  {row.phase}/{row.status}/{row.action_kind}: {row.rows}")

        effective_users = client.fetchall(
            f"SELECT DISTINCT effective_user FROM {audit_fqn} ORDER BY effective_user"
        )
        users = [row.effective_user for row in effective_users]
        expected_user = client.scalar("SELECT current_user()")
        assert users == [expected_user], users
        total_audit = client.scalar(f"SELECT count(*) FROM {audit_fqn}")
        assert total_audit == 12, total_audit
        completed = client.scalar(
            f"SELECT count(*) FROM {audit_fqn} WHERE phase = 'CONFIRM' AND status = 'COMPLETED'"
        )
        assert completed == 4, completed
        print(f"Audit attribution confirmed: {expected_user}, {total_audit} rows")

        print("LIVE_ACTION_E2E_PASS")
        if args.keep_resources:
            print(f"Kept source table: {source_fqn}")
            print(f"Kept audit table: {audit_fqn}")
            print(f"Kept external function: {fqn.schema_fqn}.`{external_function}`")
    finally:
        if not args.keep_resources:
            _cleanup_uc_objects(
                client,
                fqn,
                source_table=source_table,
                audit_table=audit_table,
                external_function=external_function,
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--host", required=True)
    parser.add_argument("--warehouse-id", required=True)
    parser.add_argument("--catalog", default="")
    parser.add_argument("--schema", default="vkg_actions_e2e")
    parser.add_argument("--run-id", default=f"r{int(time.time())}")
    parser.add_argument("--keep-resources", action="store_true")
    return parser.parse_args()


def _databricks_user_token(profile: str) -> str:
    raw = subprocess.check_output(
        ["databricks", "auth", "token", profile, "-o", "json"],
        text=True,
    )
    payload = json.loads(raw)
    token = payload.get("access_token") or payload.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"databricks auth token returned no token for {profile}")
    return token


class _SqlClient:
    def __init__(self, host: str, warehouse_id: str, token: str) -> None:
        self._server_hostname = (
            host.replace("https://", "").replace("http://", "").rstrip("/")
        )
        self._http_path = f"/sql/1.0/warehouses/{warehouse_id}"
        self._token = token

    def execute(self, statement: str, parameters: dict[str, Any] | None = None) -> None:
        self._run(statement, parameters)

    def scalar(self, statement: str, parameters: dict[str, Any] | None = None) -> Any:
        rows = self._run(statement, parameters)
        return rows[0][0] if rows and rows[0] else None

    def fetchall(
        self, statement: str, parameters: dict[str, Any] | None = None
    ) -> list[Any]:
        return self._run(statement, parameters)

    def _run(
        self, statement: str, parameters: dict[str, Any] | None = None
    ) -> list[Any]:
        with dbsql.connect(
            server_hostname=self._server_hostname,
            http_path=self._http_path,
            access_token=self._token,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(statement, parameters or None)
                try:
                    return list(cursor.fetchall())
                except Exception:
                    return []


class _Fqn:
    def __init__(self, catalog: str, schema: str) -> None:
        self.catalog = catalog
        self.schema = schema

    @property
    def schema_fqn(self) -> str:
        return f"`{self.catalog}`.`{self.schema}`"

    def table_name(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"


def _setup_uc_objects(
    client: _SqlClient,
    fqn: _Fqn,
    *,
    source_table: str,
    audit_table: str,
    external_function: str,
) -> None:
    client.execute(f"CREATE SCHEMA IF NOT EXISTS {fqn.schema_fqn}")
    client.execute(f"DROP FUNCTION IF EXISTS {fqn.schema_fqn}.`{external_function}`")
    client.execute(f"DROP TABLE IF EXISTS {fqn.schema_fqn}.`{audit_table}`")
    client.execute(f"DROP TABLE IF EXISTS {fqn.schema_fqn}.`{source_table}`")
    client.execute(
        f"""
        CREATE TABLE {fqn.schema_fqn}.`{source_table}` (
          supplier_id STRING NOT NULL,
          supplier_name STRING,
          supplier_status STRING
        )
        """
    )
    client.execute(
        f"""
        INSERT INTO {fqn.schema_fqn}.`{source_table}` VALUES
        ('SUP-001', 'Original supplier', 'ACTIVE')
        """
    )
    client.execute(
        f"""
        CREATE TABLE {fqn.schema_fqn}.`{audit_table}` (
          audit_id STRING,
          phase STRING,
          status STRING,
          action_iri STRING,
          action_kind STRING,
          subject_iri STRING,
          prepare_id STRING,
          params_hash STRING,
          preview_hash STRING,
          old_value_hash STRING,
          idempotency_key STRING,
          source_table STRING,
          source_key_column STRING,
          source_value_column STRING,
          old_value STRING,
          new_value STRING,
          params_json STRING,
          preview_json STRING,
          result_json STRING,
          error_message STRING,
          effective_user STRING,
          created_at TIMESTAMP
        )
        """
    )
    client.execute(
        f"""
        CREATE FUNCTION {fqn.schema_fqn}.`{external_function}`(
          object_uid STRING,
          params_json STRING,
          idempotency_key STRING
        )
        RETURNS STRUCT<
          status: STRING,
          external_request_id: STRING,
          result: STRING,
          message: STRING,
          executed_at: TIMESTAMP
        >
        RETURN named_struct(
          'status', 'COMPLETED',
          'external_request_id', idempotency_key,
          'result', concat(object_uid, '|', params_json),
          'message', 'accepted',
          'executed_at', current_timestamp()
        )
        """
    )


def _cleanup_uc_objects(
    client: _SqlClient,
    fqn: _Fqn,
    *,
    source_table: str,
    audit_table: str,
    external_function: str,
) -> None:
    client.execute(f"DROP FUNCTION IF EXISTS {fqn.schema_fqn}.`{external_function}`")
    client.execute(f"DROP TABLE IF EXISTS {fqn.schema_fqn}.`{audit_table}`")
    client.execute(f"DROP TABLE IF EXISTS {fqn.schema_fqn}.`{source_table}`")


def _action_service(
    *,
    catalog: str,
    schema: str,
    source_table: str,
    audit_table: str,
    external_function: str,
    warehouse_id: str,
) -> ActionService:
    with tempfile.TemporaryDirectory(prefix="vkg-action-e2e-") as raw_tmp:
        tmp = Path(raw_tmp)
        mapping_path = tmp / "mapping.ttl"
        ontology_path = tmp / "ontology.ttl"
        actions_path = tmp / "actions.ttl"
        mapping_path.write_text(
            f"""
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{ACTION_NS}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:SupplierMap a rr:TriplesMap ;
  rr:logicalTable [ rr:tableName "{catalog}.{schema}.{source_table}" ] ;
  rr:subjectMap [
    rr:class ex:Supplier ;
    rr:template "{SUBJECT_BASE}/{{supplier_id}}"
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:supplierName ;
    rr:objectMap [ rr:column "supplier_name" ; rr:datatype xsd:string ]
  ] .
""".strip()
            + "\n",
            encoding="utf-8",
        )
        ontology_path.write_text(
            f"""
@prefix ex: <{ACTION_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:Supplier a owl:Class .
ex:supplierName a owl:DatatypeProperty ;
  rdfs:domain ex:Supplier ;
  rdfs:range xsd:string .
""".strip()
            + "\n",
            encoding="utf-8",
        )
        actions_path.write_text(
            f"""
@prefix act: <https://databricks.com/ontology/vkg/actions#> .
@prefix ex: <{ACTION_NS}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:updateSupplierName a act:WriteBackAction ;
  act:logicalKey "updateSupplierName" ;
  act:boundClass ex:Supplier ;
  act:targetProperty ex:supplierName ;
  act:status "PUBLISHED" ;
  act:idempotencyStrategy "REQUEST_KEY" ;
  act:inputParameter [
    act:parameterName "newValue" ;
    act:datatype xsd:string
  ] .

ex:requestSupplierReview a act:ExternalAction ;
  act:logicalKey "requestSupplierReview" ;
  act:boundClass ex:Supplier ;
  act:invokesFunction "{catalog}.{schema}.{external_function}" ;
  act:status "PUBLISHED" ;
  act:idempotencyStrategy "REQUEST_KEY" ;
  act:inputParameter [
    act:parameterName "reason" ;
    act:datatype xsd:string
  ] .
""".strip()
            + "\n",
            encoding="utf-8",
        )
        action_catalog = ActionCatalog.load(actions_path, ontology_path, mapping_path)

    settings = Settings(
        warehouse_id=warehouse_id,
        mappings_volume_path="/tmp/vkg-actions-e2e",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog=catalog,
        default_schema=schema,
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=Path("/tmp/ontop-vkg-action-e2e"),
        fm_model_name="test-model",
        action_audit_table=f"{catalog}.{schema}.{audit_table}",
        action_confirm_signing_key="live-action-e2e-signing-key",
        action_prepare_ttl_seconds=600,
    )
    return ActionService(
        catalog=action_catalog,
        settings=settings,
        audit_logger=ActionAuditLogger(settings),
        token_signer=PrepareTokenSigner.from_settings(settings),
    )


def _run_rest_writeback(
    *,
    service: ActionService,
    token: str,
    action_iri: str,
    subject_iri: str,
) -> None:
    app = FastAPI()
    app.state.action_service = service
    app.include_router(create_action_router(), prefix="/api/actions")
    client = TestClient(app)
    listed = client.get(
        "/api/actions",
        params={"subject_iri": subject_iri, "kind": "WRITE_BACK"},
    )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["actions"]) == 1, listed.text

    prepared = client.post(
        "/api/actions/prepare",
        headers={"x-forwarded-access-token": f"Bearer {token}"},
        json={
            "action_iri": action_iri,
            "subject_iri": subject_iri,
            "params": {"newValue": "REST-updated"},
            "idempotency_key": "rest-writeback-1",
        },
    )
    assert prepared.status_code == 200, prepared.text
    preview = prepared.json()["preview"]
    assert preview["old_value"] == "Original supplier", preview
    assert preview["new_value"] == "REST-updated", preview

    confirmed = client.post(
        "/api/actions/confirm",
        headers={"x-forwarded-access-token": f"Bearer {token}"},
        json={"preparation_token": prepared.json()["preparation_token"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "COMPLETED", confirmed.text
    assert confirmed.json()["result"]["rows_affected"] == 1, confirmed.text


async def _run_mcp_http_writeback(
    *,
    service: ActionService,
    token: str,
    action_iri: str,
    subject_iri: str,
) -> None:
    with _mcp_http_url(service) as url:
        async with Client(
            StreamableHttpTransport(
                f"{url}/mcp",
                headers={"x-forwarded-access-token": f"Bearer {token}"},
            )
        ) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert {"list_actions", "prepare_action", "confirm_action"} <= tools, tools
            listed = (
                await client.call_tool(
                    "list_actions",
                    {"subject_iri": subject_iri, "kind": "WRITE_BACK"},
                )
            ).data
            assert listed["available"] is True, listed
            assert len(listed["actions"]) == 1, listed

            prepared = (
                await client.call_tool(
                    "prepare_action",
                    {
                        "action_iri": action_iri,
                        "subject_iri": subject_iri,
                        "params": {"newValue": "MCP-HTTP-updated"},
                        "idempotency_key": "mcp-http-writeback-1",
                    },
                )
            ).data
            assert prepared["preview"]["old_value"] == "REST-updated", prepared

            confirmed = (
                await client.call_tool(
                    "confirm_action",
                    {"preparation_token": prepared["preparation_token"]},
                )
            ).data
            assert confirmed["status"] == "COMPLETED", confirmed
            assert confirmed["result"]["rows_affected"] == 1, confirmed


async def _run_mcp_http_external(
    *,
    service: ActionService,
    token: str,
    action_iri: str,
    subject_iri: str,
) -> None:
    with _mcp_http_url(service) as url:
        async with Client(
            StreamableHttpTransport(
                f"{url}/mcp",
                headers={"x-forwarded-access-token": f"Bearer {token}"},
            )
        ) as client:
            prepared = (
                await client.call_tool(
                    "prepare_action",
                    {
                        "action_iri": action_iri,
                        "subject_iri": subject_iri,
                        "params": {"reason": "live E2E over MCP HTTP"},
                        "idempotency_key": "mcp-http-external-1",
                    },
                )
            ).data
            assert prepared["action_kind"] == "EXTERNAL", prepared

            confirmed = (
                await client.call_tool(
                    "confirm_action",
                    {"preparation_token": prepared["preparation_token"]},
                )
            ).data
            assert confirmed["status"] == "COMPLETED", confirmed
            assert confirmed["result"]["status"] == "COMPLETED", confirmed
            assert (
                confirmed["result"]["external_request_id"] == "mcp-http-external-1"
            ), confirmed


def _run_direct_mcp_writeback(
    *,
    service: ActionService,
    token: str,
    action_iri: str,
    subject_iri: str,
) -> None:
    _configure_mcp_service(service, token)
    from mcp_server import (
        confirm_action as mcp_confirm_action,
        list_actions as mcp_list_actions,
        prepare_action as mcp_prepare_action,
    )

    listed = mcp_list_actions(subject_iri=subject_iri, kind="WRITE_BACK")
    assert listed["available"] is True, listed
    assert len(listed["actions"]) == 1, listed

    prepared = asyncio.run(
        mcp_prepare_action(
            action_iri,
            subject_iri,
            {"newValue": "MCP-direct-updated"},
            idempotency_key="mcp-direct-writeback-1",
        )
    )
    assert isinstance(prepared, dict), prepared
    assert prepared["preview"]["old_value"] == "MCP-HTTP-updated", prepared

    confirmed = asyncio.run(mcp_confirm_action(prepared["preparation_token"]))
    assert isinstance(confirmed, dict), confirmed
    assert confirmed["status"] == "COMPLETED", confirmed
    assert confirmed["result"]["rows_affected"] == 1, confirmed


def _configure_mcp_service(service: ActionService, token: str) -> None:
    settings = Settings(
        warehouse_id="unused",
        mappings_volume_path="/tmp/vkg-actions-e2e",
        mapping_file="mapping.ttl",
        ontology_file="ontology.ttl",
        default_catalog="unused",
        default_schema="unused",
        ontop_internal_port=18080,
        app_port=8000,
        work_dir=Path("/tmp/ontop-vkg-action-e2e"),
        fm_model_name="test-model",
    )
    configure_mcp(
        McpRuntime(
            ontology_store=OntologyStore(),
            ontop_manager=object(),  # not used by action tools
            settings=settings,
            http_client=object(),  # not used by action tools
            action_service=service,
        )
    )
    import mcp_server

    mcp_server.get_mcp_user_token = lambda: token


class _mcp_http_url:
    def __init__(self, service: ActionService) -> None:
        self._service = service
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._port = _free_port()

    def __enter__(self) -> str:
        settings = Settings(
            warehouse_id="unused",
            mappings_volume_path="/tmp/vkg-actions-e2e",
            mapping_file="mapping.ttl",
            ontology_file="ontology.ttl",
            default_catalog="unused",
            default_schema="unused",
            ontop_internal_port=18080,
            app_port=8000,
            work_dir=Path("/tmp/ontop-vkg-action-e2e"),
            fm_model_name="test-model",
        )
        configure_mcp(
            McpRuntime(
                ontology_store=OntologyStore(),
                ontop_manager=object(),  # not used by action tools
                settings=settings,
                http_client=object(),  # not used by action tools
                action_service=self._service,
            )
        )
        app = mcp.http_app(path="/mcp", stateless_http=True)
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self._port,
                log_level="warning",
                lifespan="on",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("local MCP HTTP server did not start")
            time.sleep(0.05)
        return f"http://127.0.0.1:{self._port}"

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
