# Ontop VKG on Databricks Apps

This repo uses the [Ontop Virtual Knowledge Graph (VKG)](https://ontop-vkg.org/) to provide a SPARQL endpoint over Databricks SQL, deployed as a Databricks App via a Declarative Automation Bundle (DAB).
All translated queries are executed using the user's identity, ensuring that the Unity Catalog permissions of the underlying tables are enforced.

The app also provides an MCP endpoint for agents to iteratively validate SPARQL queries they generate against the ontology using the [Ontology-based Query Check (OBQC)](https://arxiv.org/abs/2405.11706) approach defined by Allemang and Sequeda.

For supported SPARQL features, common reformulation failures, and query rewrite patterns, see [SPARQL_FEATURES.md](SPARQL_FEATURES.md).

## Installation

Ensure that you have the latest Databricks CLI installed, then run:

```bash
databricks auth login
export BUNDLE_VAR_catalog=main
export BUNDLE_VAR_schema=default
export BUNDLE_VAR_instance=alpha
make run
```

You must supply `catalog`, `schema`, and `instance` (see [Configuration](#configuration)). The catalog and schema must already exist — the bundle will not create them. `instance` is a short label that names this deployment’s volume, warehouse, and app so multiple independent copies can coexist in one workspace.

## Mappings and ontology

The `mappings/` directory holds the VKG definition that gets uploaded to the UC Volume. It currently contains example TPC-H `mapping.ttl` and `ontology.ttl` files so the project works out of the box — edit or replace these with your own mapping and ontology when setting up your VKG.

**Ontology requirement:** MCP discovery tools (`search_ontology`, `describe_iri`) and `check_sparql` need `ontology.ttl` (or the configured ontology file) present in the volume. Without it, those tools report that the ontology is not loaded however `execute_sparql` can still run against the VKG if mappings are valid.

## Developing Mappings

The app also includes a visual editor that can help you define an R2RML mapping file, accessible at the `/mapper` endpoint. It speeds up the process by pulling data from Unity Catalog to prepopulate fields and can also import your own ontology to prepopulate fields like class and property selections.

It can also use an LLM to automatically generate a mapping for a selection of tables in Unity Catalog.
Click "Autogenerate", select the tables or schema you want to include, and it will gather context and add the result to your mapping.

Once it is ready, download it to your local machine and use `make run` to upload it and restart the app.

## Deployment stages

DAB supports only one `artifact_path` per target, and the UC volume must exist before artifacts can be uploaded. The bundle is therefore split into three targets:

| Target | Purpose |
|--------|---------|
| `volume` | Create the UC volume `ontop_vkg_<instance>` in the specified catalog and schema |
| `mappings` | Upload `mapping.ttl` and optional `ontology.ttl` to that volume |
| `app` | Deploy warehouse, app, and Ontop/JDBC artifacts for that instance |

The `make run` target runs all of these in order and then starts the app.

Each `instance` gets its own volume (`ontop_vkg_<instance>`), SQL warehouse (`ontop-vkg-wh-<instance>`),
app (`mcp-ontop-vkg-<instance>`), and bundle state (via a per-instance `root_path`).
Destroying one instance’s targets tears down only that instance’s resources.

### Multiple instances

Deploy a second copy by choosing a different `BUNDLE_VAR_instance` (and optionally a different catalog/schema):

```bash
# First deployment
BUNDLE_VAR_catalog=main BUNDLE_VAR_schema=default BUNDLE_VAR_instance=alpha make run

# Second deployment in the same workspace
BUNDLE_VAR_catalog=main BUNDLE_VAR_schema=default BUNDLE_VAR_instance=beta make run
```

## Endpoints

| Path | Description |
|------|-------------|
| `/yasgui` | SPARQL query UI |
| `/sparql` | SPARQL 1.1 endpoint |
| `/mcp` | TBox Toolbox MCP |
| `/health` | Health check (Ontop + ontology loaded) |
| `/mapper` | Visual R2RML mapping editor |
| `/api/actions` | Governed action discovery, prepare, and confirm API |

## MCP tools

Public MCP URL: `https://<app-url>/mcp`

| Tool | Purpose |
|------|---------|
| `health` | Ontop running + ontology loaded |
| `search_ontology` | Fuzzy label/comment search returning results in Turtle format |
| `describe_iri` | Neighborhood of the given IRI returned in Turtle format |
| `check_sparql` | Ontology-Based Query Check (OBQC) |
| `execute_sparql` | Run SPARQL against the VKG → SPARQL JSON or error text |
| `list_actions` | List published actions and writable properties |
| `describe_action` | Describe one published action |
| `prepare_action` | Validate an action and return a signed preview |
| `confirm_action` | Confirm a prepared action and run its side effect |

### Agent usage pattern

1. `search_ontology` / `describe_iri` to discover relevant resources
2. Draft SPARQL
3. `check_sparql` and rewrite based on violation messages (if any)
4. `execute_sparql` and iterate based on the results

TBox tools use the in-memory ontology only. `execute_sparql` uses Ontop + Databricks SQL with the caller's Apps-forwarded access token.

Action execution also uses the caller's `x-forwarded-access-token`: Unity
Catalog permissions govern source reads, audit reads/writes, table updates,
and UC function invocation. Side effects use a two-phase flow. `prepare_action`
validates current state and writes a preview audit row; `confirm_action`
verifies the signed, unexpired preview, writes a confirming row, revalidates
state, and only then executes. Preparation tokens are bound to the actor
resolved by `session_user()` and cannot be confirmed by another user. The app
remains a read-only VKG when no action catalog is present.

## Governed actions

Actions are RDF declarations in an `actions.ttl` file alongside the deployed
mapping and ontology. See [mappings/samples/actions.ttl](mappings/samples/actions.ttl)
for the vocabulary shape. Copy a reviewed catalog to
`<MAPPINGS_VOLUME_PATH>/mappings/.internal/` and set `VKG_ACTIONS_FILE` to its
filename.

Two action kinds are supported:

- **Write-back:** updates one literal property on one existing row when the
  R2RML mapping is safely invertible. Joined, computed, aggregated, distinct,
  multi-table, multi-key, identity-column, object-property, and coupled-column
  mappings are reported as read-only.
- **External:** invokes a named three-part Unity Catalog function. External
  credentials and HTTP calls belong in that function, typically through a UC
  connection; the Ontop app does not store them. The subject must be a current
  VKG instance of the action's `boundClass` at both prepare and confirm.

### Runtime configuration

| Variable | Description |
|----------|-------------|
| `VKG_ACTIONS_FILE` | Catalog filename; defaults to `actions.ttl` |
| `VKG_ACTION_AUDIT_TABLE` | Three-part UC table for action audit rows |
| `VKG_ACTION_CONFIRM_SIGNING_KEY` | Secret used to sign preparation tokens |
| `VKG_ACTION_PREPARE_TTL_SECONDS` | Preparation token lifetime; defaults to `600` |

The bundle configures the non-secret values. Before publishing an action
catalog, bind the signing key from a Databricks secret resource; never put its
value in repository configuration:

```yaml
# Under resources.apps.ontop_vkg.config.env
- name: VKG_ACTION_CONFIRM_SIGNING_KEY
  value_from: action-confirm-signing-key

# Under resources.apps.ontop_vkg.resources
- name: action-confirm-signing-key
  secret:
    scope: my-secret-scope
    key: vkg-action-signing-key
    permission: READ
```

`VKG_ACTION_AUDIT_TABLE` and the signing key are required when a catalog is
present. They are intentionally not required for the default read-only
deployment.

### Required Unity Catalog privileges

Grant the acting user, not only the app service principal, the privileges used
by each action:

```sql
GRANT SELECT, MODIFY ON TABLE <catalog>.<schema>.<source_table> TO `<principal>`;
GRANT EXECUTE ON FUNCTION <catalog>.<schema>.<function_name> TO `<principal>`;
GRANT INSERT, SELECT ON TABLE <catalog>.<schema>.<audit_table> TO `<principal>`;
```

The corresponding `USE CATALOG` and `USE SCHEMA` grants are also required.
Create the audit table with this v1 shape:

```sql
CREATE TABLE <catalog>.<schema>.<audit_table> (
  audit_id STRING, phase STRING, status STRING, action_iri STRING,
  action_kind STRING, subject_iri STRING, prepare_id STRING,
  params_hash STRING, preview_hash STRING, old_value_hash STRING,
  idempotency_key STRING, source_table STRING, source_key_column STRING,
  source_value_column STRING, old_value VARIANT, new_value VARIANT,
  params_json STRING, preview_json STRING, result_json STRING,
  error_message STRING, effective_user STRING, created_at TIMESTAMP
);
```

`effective_user` is populated from `session_user()` by the app's audit insert,
and audit recovery queries restrict rows to that same session principal.
Primary-key and unique constraints on Delta tables are informational, so this
audit table is replay evidence rather than an external-effect uniqueness
mechanism.

### REST API

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/actions` | List actions; supports `class_iri`, `subject_iri`, `property_iri`, and `kind` filters |
| `GET /api/actions/describe?action_iri=...` | Describe one action |
| `POST /api/actions/prepare` | Prepare an action under the forwarded user token |
| `POST /api/actions/confirm` | Revalidate and execute a preparation token |

Prepare and confirm require `x-forwarded-access-token`. Confirmation refuses
expired, tampered, mismatched, or stale write-back previews.

Published external actions must use `REQUEST_KEY`. The runtime trims the opaque
client key and derives an actor/action-scoped `invocation_id` plus a canonical
`request_hash`. External functions receive four arguments:

```text
(object_uid STRING, params_json STRING, invocation_id STRING, request_hash STRING)
```

The target integration must atomically claim `invocation_id` with
`request_hash`, perform its external effect at most once, persist the terminal
result, and return that result for identical retries. Reusing an invocation ID
with a different request hash must return `CONFLICT` without another effect.
Every result envelope must echo `invocation_id` and `request_hash` and contain a
terminal `status` (`COMPLETED`, `SUCCESS`, `FAILED`, `ERROR`, or `CONFLICT`). The
runtime replays terminal audit outcomes but does not claim that the audit Delta
table alone provides exactly-once execution.

Each action's `act:timeoutSeconds` is applied with `SET STATEMENT_TIMEOUT` on
the same DBSQL session that performs its reads, writes, subject checks, audits,
or function invocation. Synchronous DBSQL work runs outside the FastAPI event
loop.

For a disposable live verification against a SQL warehouse, run:

```bash
python scripts/live-action-e2e.py \
  --profile DEFAULT \
  --host https://<workspace-host> \
  --warehouse-id <warehouse-id>
```

The harness creates isolated source, audit, and function objects, exercises
REST and MCP action paths, verifies user attribution, and removes the objects
unless `--keep-resources` is supplied.

## Configuration

The app's start command and environment variables are defined under the app's `config`
block in `databricks.yml` rather than having a separate `app.yaml`.

Bundle variables in `databricks.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `catalog` | *(required)* | UC catalog for volume and connection default |
| `schema` | *(required)* | UC schema for volume and connection default |
| `instance` | *(required)* | Used to differentiate multiple deployments in the same workspace |
| `warehouse_cluster_size` | `Small` | SQL warehouse size |
| `ontop_version` | `5.5.0` | Ontop release version |
| `jdbc_version` | `3.4.1` | Databricks JDBC driver version |
| `jre_version` | `17.0.19_10` | Temurin JRE version |
| `actions_file` | `actions.ttl` | Action catalog filename in the mappings volume |
| `action_audit_table` | empty | Three-part action audit table |
| `action_prepare_ttl_seconds` | `600` | Preparation token lifetime |

Set required variables using the Databricks bundle environment-variable convention:
`BUNDLE_VAR_catalog`, `BUNDLE_VAR_schema`, and `BUNDLE_VAR_instance`. 

## Required Unity Catalog grants

Two types of identities are involved and they need different grants:

| Identity | When | Needs |
|----------|------|-------|
| **App service principal** | Ontop startup — schema introspection | `USE CATALOG`, `USE SCHEMA`, **`SELECT`** on the mapped schema |
| **End user** | Query execution (forwarded token) | Their own `SELECT` on the tables they query |

Grant the app's service principal access to the schema your mapping references:

```sql
GRANT USE CATALOG ON CATALOG <catalog>            TO `<service-principal-id>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema>   TO `<service-principal-id>`;
GRANT SELECT      ON SCHEMA  <catalog>.<schema>   TO `<service-principal-id>`;
```

**`SELECT` is required, not just `USE SCHEMA`.** Ontop's metadata bootstrap runs as
the service principal and, besides the `SHOW`-family metadata calls, probes each mapped
table to read column types from `ResultSetMetaData`. Without `SELECT` the probe fails,
metadata enumeration silently falls back to the `samples` catalog and queries fail with:

```
InvalidMappingSourceQueriesException: Cannot find relation `<catalog>`.`<schema>`.`<table>`
  (available choices: [`samples`...])
```

Note this is **separate from data access**: reformulated SQL is executed under the
*user's* forwarded token, so per-user table/row/column permissions still apply to query
results. The service principal's `SELECT` is used only for startup introspection.
If that is not acceptable in your environment, Ontop's `endpoint --db-metadata=<file>`
can load column types and keys from a JSON file produced by `ontop extract-db-metadata`,
which skips the initial probe.

## Build-time downloads (external network access)

Deploying the `app` target runs `scripts/download-artifacts.sh` as a bundle artifact
`build:` step, which **downloads binaries from the public internet**:

| Artifact | Default source | Override |
|----------|----------------|----------|
| Ontop CLI zip | `github.com/ontop/ontop` (GitHub Releases) | `ONTOP_BASE_URL` |
| Temurin JRE 17 | `github.com/adoptium/temurin17-binaries` (GitHub Releases) | `JRE_BASE_URL` |
| Databricks JDBC driver | `repo1.maven.org` (Maven Central) | `MAVEN_REPO_URL` |
| ↳ fallback if the above fails | `maven.aliyun.com` | `MAVEN_MIRROR_URL` (set empty to disable) |

**If your organization requires internal mirrors, blocks egress, or intercepts TLS,
review and adjust these sources before deploying.** Options:

- **Repoint the sources.** Set the override variables above — e.g. to use an internal
  Maven repository and disable the third-party fallback:
  ```bash
  MAVEN_REPO_URL=https://artifacts.example.com/maven2 MAVEN_MIRROR_URL= \
    ./scripts/download-artifacts.sh
  ```
  To apply them to `make deploy-app`, add them to the `build:` block in
  `databricks.yml` next to the existing version variables.
- **Pre-populate `artifacts/`.** The script skips any file that already exists, so
  placing the JRE tarball, Ontop zip, and `databricks-jdbc-<version>.jar` in
  `artifacts/` by hand makes the deploy fully offline. This is the simplest
  air-gapped path.

**Verifying downloads.** No checksums are enforced by default. To pin the exact bytes,
record them once and the script will verify on every subsequent run (failing the deploy
on a mismatch):

```bash
cd artifacts && shasum -a 256 * > SHA256SUMS
```

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
