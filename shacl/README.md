# SHACL rules over a Virtual Knowledge Graph

Libraries such as pySHACL and SHACL2SPARQL already exist, but they are built for purpose-built triplestores.
A virtual knowledge graph like Ontop has different performance and SPARQL-subset constraints. For example projected variables must be uniquely typed, queries must push down to a single SQL statement, and graph-wide
`COUNT` scans are not viable.

This package compiles SHACL Core into SPARQL `CONSTRUCT` or `SELECT` queries that report violations. Mapping-aware checks against R2RML are planned so predicates and classes can be confirmed to appear in the VKG before execution.

## Status

| Feature | Status |
| --- | --- |
| Parse Turtle shapes into an AST (`ShapesGraph`) | Done |
| Node shapes and property shapes (typed or via `sh:property`) | Done |
| Shape metadata (`sh:deactivated`, `sh:message`, `sh:severity`, `sh:name`, `sh:description`, `sh:order`) | Done |
| Targets: `sh:targetClass`, `sh:targetNode`, `sh:targetSubjectsOf`, `sh:targetObjectsOf` | Done |
| Property shapes inherit targets from parent node shapes | Done |
| SPARQL-based targets (`sh:target`) | Not started |
| SPARQL-based constraints (`sh:sparql`) | Not started |
| IRI `sh:path` on property shapes | Done (parsed and compiled) |
| Complex SHACL paths (sequence, inverse, alternative, zero-or-more, …) | Parsed into path AST; SPARQL not compiled |
| RDF lists (`sh:in`, `sh:and`, `sh:or`, `sh:xone`, `sh:ignoredProperties`, `sh:languageIn`) | Expanded |
| Node-shape constraints compiled to SPARQL | Not started (validator skips node shapes) |
| Property-shape constraints compiled to SPARQL | Partial (see below) |
| Execute SPARQL against a VKG / SPARQL endpoint | Not started |
| Assemble a `sh:ValidationReport` | Not started |
| R2RML mapping as evaluation context | Not started |
| Check that classes and predicates appear in the mapping | Not started |
| App / MCP integration | Not started |
| Parse tests (`python3 -m pytest shacl/tests -q`) | Done |
| Sample TPC-H shapes (`mappings/shapes.ttl`) | Done |

## Constraint components

Parser coverage is SHACL Core. SPARQL generation is only for the property-shape subset below.

| Component | Parse | SPARQL |
| --- | --- | --- |
| `sh:minCount` | Done | Done |
| `sh:maxCount` | Done | Done |
| `sh:datatype` | Done | Done |
| `sh:pattern` / `sh:flags` | Done | Done |
| `sh:class` | Done | Not started |
| `sh:nodeKind` | Done | Not started |
| `sh:hasValue` | Done | Not started |
| `sh:in` | Done | Not started |
| `sh:minExclusive` / `sh:minInclusive` / `sh:maxExclusive` / `sh:maxInclusive` | Done | Not started |
| `sh:minLength` / `sh:maxLength` | Done | Not started |
| `sh:uniqueLang` | Done | Not started |
| `sh:closed` / `sh:ignoredProperties` | Done | Not started |
| `sh:equals` / `sh:disjoint` / `sh:lessThan` / `sh:lessThanOrEquals` | Done | Not started |
| `sh:node` / `sh:not` | Done | Not started |
| `sh:and` / `sh:or` / `sh:xone` | Done | Not started |
| `sh:qualifiedMinCount` / `sh:qualifiedMaxCount` (`sh:qualifiedValueShape`, optional `sh:qualifiedValueShapesDisjoint`) | Done | Not started |
| `sh:languageIn` | Done | Not started |
