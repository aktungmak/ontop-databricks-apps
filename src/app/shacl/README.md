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
| One level of `sh:property` selected through a node shape | Done via `compile_shape` |
| Nested `sh:property` under property shapes | Done via `_nested_property_results` |
| SPARQL-based targets (`sh:target`) | Not started |
| SPARQL-based constraints (`sh:sparql`) | Not started |
| IRI `sh:path` on property shapes | Done (parsed and compiled) |
| Complex SHACL paths | Inverse, sequence, and alternative compiled; `*`, `+`, and `?` rejected at compile time |
| RDF lists (`sh:in`, `sh:and`, `sh:or`, `sh:xone`, `sh:ignoredProperties`, `sh:languageIn`) | Expanded |
| Node-shape constraints compiled to SPARQL | Done for implemented leaf validators; property-only parameters are ill-formed |
| Property-shape constraints compiled to SPARQL | Partial (see below) |
| Execute SPARQL against the VKG via MCP `validate_shacl` | Done |
| Assemble a `sh:ValidationReport` | Not started |
| R2RML mapping as evaluation context | Not started |
| Check that classes and predicates appear in the mapping | Not started |
| App / MCP integration | Done for `validate_shacl(shapes_turtle, shape_iri)` |
| Parse tests (`python3 -m pytest shacl/tests -q` from `src/app`) | Done |

## SPARQL validators (SHACL Core)

[SHACL Core](https://www.w3.org/TR/shacl/#core-components) requires one validator per constraint component ([Appendix D](https://www.w3.org/TR/shacl/#SHACL-Core-Validators)). Each validator will live in `sparql_validator.py`, registered with `@sparql_validator(...)`, and emit a violation `SELECT` (or a composition of such queries) rather than the spec’s illustrative `ASK`.

SHACL-SPARQL (`sh:SPARQLConstraintComponent` / `sh:sparql`, SPARQL-based targets, custom constraint components) is out of Core and is tracked only in Status above.

`sh:property` is a Core constraint component (`sh:PropertyConstraintComponent`) but is stored on `Shape.property`, not as a `ConstraintComponent` dataclass. The product API, `SparqlValidator.compile_shape`, compiles the selected shape and one level of direct property children with the selected shape's effective targets. Nested `sh:property` under those children is compiled through `_nested_property_results`, rebinding focus to the parent's value nodes. Deactivated shapes and shapes with ``has_targets=false`` (no effective SHACL targets; this is a boolean, not a count) produce no queries. Malformed shape syntax raises `IllFormedShapeError`.

Unless noted, a component applies to both node shapes and property shapes. Parameters marked *property shapes only* make a node shape ill-formed.

### 4.1 Value type

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `ClassValidator` | `sh:ClassConstraintComponent` | `sh:class` (repeatable) | Done | Done (property and node shapes; `rdf:type/rdfs:subClassOf*`) |
| `DatatypeValidator` | `sh:DatatypeConstraintComponent` | `sh:datatype` | Done | Done (property and node shapes) |
| `NodeKindValidator` | `sh:NodeKindConstraintComponent` | `sh:nodeKind` | Done | Done (property and node shapes) |

### 4.2 Cardinality (property shapes only)

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `MinCountValidator` | `sh:MinCountConstraintComponent` | `sh:minCount` | Done | Done (property shapes only) |
| `MaxCountValidator` | `sh:MaxCountConstraintComponent` | `sh:maxCount` | Done | Done (property shapes only) |

### 4.3 Value range

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `MinExclusiveValidator` | `sh:MinExclusiveConstraintComponent` | `sh:minExclusive` | Done | Done (property and node shapes) |
| `MinInclusiveValidator` | `sh:MinInclusiveConstraintComponent` | `sh:minInclusive` | Done | Done (property and node shapes) |
| `MaxExclusiveValidator` | `sh:MaxExclusiveConstraintComponent` | `sh:maxExclusive` | Done | Done (property and node shapes) |
| `MaxInclusiveValidator` | `sh:MaxInclusiveConstraintComponent` | `sh:maxInclusive` | Done | Done (property and node shapes) |

### 4.4 String-based

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `MinLengthValidator` | `sh:MinLengthConstraintComponent` | `sh:minLength` | Done | Not started |
| `MaxLengthValidator` | `sh:MaxLengthConstraintComponent` | `sh:maxLength` | Done | Not started |
| `PatternValidator` | `sh:PatternConstraintComponent` | `sh:pattern`, optional `sh:flags` | Done | Done (property and node shapes) |
| `LanguageInValidator` | `sh:LanguageInConstraintComponent` | `sh:languageIn` | Done | Not started |
| `UniqueLangValidator` | `sh:UniqueLangConstraintComponent` | `sh:uniqueLang` (property shapes only) | Done | Not started |

### 4.5 Property pair

`sh:lessThan` and `sh:lessThanOrEquals` are property shapes only.

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `EqualsValidator` | `sh:EqualsConstraintComponent` | `sh:equals` (repeatable) | Done | Not started |
| `DisjointValidator` | `sh:DisjointConstraintComponent` | `sh:disjoint` (repeatable) | Done | Not started |
| `LessThanValidator` | `sh:LessThanConstraintComponent` | `sh:lessThan` (repeatable) | Done | Not started |
| `LessThanOrEqualsValidator` | `sh:LessThanOrEqualsConstraintComponent` | `sh:lessThanOrEquals` (repeatable) | Done | Not started |

### 4.6 Logical (compose nested shapes)

These do not emit a single filter; they combine conformance of nested shapes (conjunction, disjunction, negation, exactly-one).

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `NotValidator` | `sh:NotConstraintComponent` | `sh:not` (repeatable) | Done | Not started |
| `AndValidator` | `sh:AndConstraintComponent` | `sh:and` (repeatable; RDF list of shapes) | Done | Not started |
| `OrValidator` | `sh:OrConstraintComponent` | `sh:or` (repeatable; RDF list of shapes) | Done | Not started |
| `XoneValidator` | `sh:XoneConstraintComponent` | `sh:xone` (repeatable; RDF list of shapes) | Done | Not started |

### 4.7 Shape-based

Qualified cardinality is property shapes only. Optional `sh:qualifiedValueShapesDisjoint` belongs to both qualified components.

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `NodeValidator` | `sh:NodeConstraintComponent` | `sh:node` (repeatable) | Done | Not started |
| `PropertyValidator` | `sh:PropertyConstraintComponent` | `sh:property` (direct property shapes) | Done (`Shape.property`) | Done (`compile_shape` plus `_nested_property_results`) |
| `QualifiedMinCountValidator` | `sh:QualifiedMinCountConstraintComponent` | `sh:qualifiedValueShape`, `sh:qualifiedMinCount`, optional `sh:qualifiedValueShapesDisjoint` | Done | Not started |
| `QualifiedMaxCountValidator` | `sh:QualifiedMaxCountConstraintComponent` | `sh:qualifiedValueShape`, `sh:qualifiedMaxCount`, optional `sh:qualifiedValueShapesDisjoint` | Done | Not started |

### 4.8 Other

| Validator | Component | Parameters | Parse | SPARQL |
| --- | --- | --- | --- | --- |
| `ClosedValidator` | `sh:ClosedConstraintComponent` | `sh:closed`, optional `sh:ignoredProperties` | Done | Not started |
| `HasValueValidator` | `sh:HasValueConstraintComponent` | `sh:hasValue` (repeatable) | Done | Not started |
| `InValidator` | `sh:InConstraintComponent` | `sh:in` | Done | Not started |

**29 Core validators.** SPARQL is done for 10 (`MinCount`, `MaxCount`, `Class`, `Datatype`, `NodeKind`, `Pattern`, `MinExclusive`, `MinInclusive`, `MaxExclusive`, `MaxInclusive`). Implemented leaf validators support node shapes and property shapes with IRI, inverse, sequence, or alternative paths; cardinality remains property-shape only. User paths containing `*`, `+`, or `?` are rejected at compile time, while `ClassValidator` retains its hardcoded `rdf:type/rdfs:subClassOf*` check.

`ClosedValidator` is the main VKG hazard: the spec enumerates *any* unexpected predicate on the value node. Prefer mapping-aware allowed-predicate lists over graph-wide property scans. Logical, `sh:node`, and qualified-count validators will reuse the leaf validators above rather than duplicating SPARQL.