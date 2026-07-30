# Ontop VKG on Databricks Apps

This repo uses the [Ontop Virtual Knowledge Graph (VKG)](https://ontop-vkg.org/) to provide a SPARQL endpoint over Databricks SQL, deployed as a Databricks App via a Declarative Automation Bundle (DAB).
All translated queries are executed using the user's identity, ensuring that the Unity Catalog permissions of the underlying table are enforced.

The app also provides an MCP endpoint for agents to iteratively validate SPARQL queries they generate against the ontology using the [Ontology-based Query Check (OBQC)](https://arxiv.org/abs/2405.11706) approach defined by Allemang and Sequeda.

For supported SPARQL features, common reformulation failures, and query rewrite patterns, see [SPARQL_FEATURES.md](SPARQL_FEATURES.md).

## Installation

Ensure that you have the latest Databricks CLI installed, then run:

```bash
databricks auth login --profile DEFAULT
make run
```

You will also need to edit `databricks.yml` to set the `catalog` and `schema` variables where the mapping files and other artefacts will be stored (see [Configuration](#configuration) below). The catalog and schema must already exist — the bundle will not create them.

## Mappings and ontology

The `mappings/` directory holds the VKG definition that gets uploaded to the UC Volume. It currently contains example TPC-H `mapping.ttl` and `ontology.ttl` files so the project works out of the box — edit or replace these with your own mapping and ontology when setting up your VKG.

**Ontology requirement:** MCP discovery tools (`search_ontology`, `describe_iri`) and `check_sparql` need `ontology.ttl` (or the configured ontology file) present on the volume. Without it, those tools report that the ontology is not loaded however `execute_sparql` can still run against the VKG if mappings are valid.

After changing files in `mappings/`, redeploy with:

```bash
make deploy-mappings
```

## Developing Mappings

The app also includes a visual editor that can help you define an R2RML mapping file, accessible at the `/mapper` endpoint. It speeds up the process by pulling data from Unity Catalog to prepopulate fields and can also import your own ontology to prepopulate fields like class and property selections.

It can also use an LLM to automatically generate a mapping for a selection of tables in Unity Catalog — click "Autogenerate", select the tables or schema you want to include, and it will gather context and add the result to your mapping.

Once it is ready, download it to your local machine and use `make run` to upload it and restart the app.

## Deployment stages

DAB supports only one `artifact_path` per target, and the UC volume must exist before artifacts can be uploaded. The bundle is therefore split into three targets:

| Target | Purpose |
|--------|---------|
| `volume` | Create the UC volume |
| `mappings` | Upload `mapping.ttl` and optional `ontology.ttl` to the volume |
| `app` | Deploy warehouse, app, and Ontop/JDBC artifacts |

The `make run` target runs all of these in order and then starts the app (`mcp-ontop-vkg`).

## Endpoints

| Path | Description |
|------|-------------|
| `/yasgui` | SPARQL query UI |
| `/sparql` | SPARQL 1.1 endpoint |
| `/mcp` | TBox Toolbox MCP |
| `/health` | Health check (Ontop + ontology loaded) |
| `/mapper` | Visual R2RML mapping editor |

## MCP tools

Public MCP URL: `https://<app-url>/mcp`

| Tool | Purpose |
|------|---------|
| `health` | Ontop running + ontology loaded |
| `search_ontology` | Fuzzy label/comment search returning results in Turtle format |
| `describe_iri` | Neighborhood of the given IRI returned in Turtle format |
| `check_sparql` | Ontology-Based Query Check (OBQC) |
| `execute_sparql` | Run SPARQL against the VKG → SPARQL JSON or error text |

### Agent usage pattern

1. `search_ontology` / `describe_iri` to discover relevant resources
2. Draft SPARQL
3. `check_sparql` and rewrite based on violation messages (if any)
4. `execute_sparql` and iterate based on the results

TBox tools use the in-memory ontology only. `execute_sparql` uses Ontop + Databricks SQL with the caller's Apps-forwarded access token.

## Configuration

Environment variables are set in `src/app/app.yaml`. Bundle variables in `databricks.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `catalog` | `ontop_vkg` | UC catalog for volume |
| `schema` | `default` | UC schema for volume |
| `warehouse_cluster_size` | `Small` | SQL warehouse size |
| `ontop_version` | `5.5.0` | Ontop release version |
| `jdbc_version` | `2.7.5` | Databricks JDBC driver version |
| `jre_version` | `17.0.19_10` | Temurin JRE version |

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
  placing the JRE tarball, Ontop zip, and `DatabricksJDBC42.jar` in `artifacts/` by
  hand makes the deploy fully offline. This is the simplest air-gapped path.

**Verifying downloads.** No checksums are enforced by default. To pin the exact bytes,
record them once and the script will verify on every subsequent run (failing the deploy
on a mismatch):

```bash
cd artifacts && shasum -a 256 * > SHA256SUMS
```

## Verify

```bash
databricks apps get mcp-ontop-vkg
databricks apps logs mcp-ontop-vkg
```

Open the app URL and navigate to `/yasgui` for a SPARQL editor, or point an MCP client at `/mcp`.

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
