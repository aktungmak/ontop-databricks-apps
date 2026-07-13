# Ontop VKG on Databricks Apps

This repo uses the [Ontop Virtual Knowledge Graph (VKG)](https://ontop-vkg.org/) to provide a SPARQL endpoint over Databricks SQL, deployed as a Databricks App via a Declarative Automation Bundle (DAB).

## Prerequisites

- Databricks CLI with OAuth profile configured
- `curl`, `make`

```bash
databricks auth login --profile DEFAULT
```

You will also need to edit `databricks.yml` to set the `catalog` and `schema` variables where the mapping files and other artefacts will be stored (see [Configuration](#configuration) below). The catalog and schema must already exist — the bundle will not create them.

## Mappings

The `mappings/` directory holds the VKG definition that gets uploaded to the UC Volume. It currently contains example TPC-H `mapping.ttl` and `ontology.ttl` files so the project works out of the box — edit or replace these with your own mapping and ontology when setting up your VKG.

After changing files in `mappings/`, redeploy with:

```bash
make deploy-mappings
```

## Deployment stages

DAB supports only one `artifact_path` per target, and the UC volume must exist before artifacts can be uploaded. The bundle is therefore split into three targets:

| Target | Purpose |
|--------|---------|
| `volume` | Create the UC volume |
| `mappings` | Upload `mapping.ttl` and optional `ontology.ttl` to the volume |
| `app` | Deploy warehouse, app, and Ontop/JDBC artifacts |

### Full deploy and run

```bash
make run
```

This runs all three stages in order, then starts the app.

### Individual stages

```bash
# First-time setup (volume must exist before mappings or app)
make deploy-volume

# Upload or update mapping and ontology
make deploy-mappings

# Deploy warehouse, app, and Ontop runtime artifacts
make deploy-app
```

## Volume layout

```
/Volumes/{catalog}/{schema}/ontop_vkg/
├── artifacts/
│   └── .internal/         # ontop_runtime artifact (app target)
│       ├── ontop-cli-5.5.0.zip
│       ├── OpenJDK17U-jre_x64_linux_hotspot_*.tar.gz
│       └── DatabricksJDBC42.jar
└── mappings/
    └── .internal/         # mappings artifact (mappings target)
        ├── mapping.ttl
        └── ontology.ttl   # Optional ontology (see Ontop docs)
```

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Redirect to YASGUI |
| `/yasgui` | SPARQL query UI |
| `/sparql` | SPARQL 1.1 endpoint |
| `/health` | Health check |

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

## Verify

```bash
databricks apps get ontop-vkg 
databricks apps logs ontop-vkg
```

Open the app URL and navigate to `/yasgui` to run SPARQL queries.
