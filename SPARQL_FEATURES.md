# SPARQL features and full-native SQL

This app asks Ontop to reformulate each SPARQL query into **one complete, executable SQL query** for Databricks SQL. That is a stricter contract than ordinary Ontop query execution.

## Executive summary

There is no useful, fixed list of broadly “banned” SPARQL syntax. `OPTIONAL`, `UNION`, aggregates, subqueries, `VALUES`, and `BIND` can all work. What matters is whether Ontop can prove both of the following after optimization:

1. **Every projected SPARQL variable has one statically inferable RDF type.** Ontop must know whether each output is an IRI, blank node, language-tagged string, `xsd:string`, `xsd:integer`, or another single RDF type. A variable that may be a string in one branch and an integer in another is not uniquely typed.
2. **The entire result can be pushed into one SQL query.** No RDF-term construction, expression evaluation, result transformation, or other post-processing may remain above Ontop's native SQL node.

A query accepted by an ordinary Ontop endpoint can therefore fail in full-native mode. Ordinary execution may retain Ontop-side post-processing or RDF reconstruction; this app needs SQL that Databricks can execute independently.

When a query fails, first make its projected variables more strongly typed, then simplify anything that may require evaluation outside Databricks SQL.

## Version note

| Ontop version | How full-native reformulation is enabled |
|---|---|
| 5.0–5.5 | Set the global property `ontop.reformulateToFullNativeQuery=true`. It affects reformulation for the configured Ontop instance. |
| 5.6 change and later versions that include it | Pass `forNativeConsumption=true` on each `/ontop/reformulate` call. The setting is per request and participates in query caching. |

The per-call API is present in the referenced development snapshot, but that snapshot is **not itself a released 5.6 artifact**. Its nearest released version is [`ontop-5.5.0`](https://github.com/ontop/ontop/releases/tag/ontop-5.5.0). Check the actual Ontop version deployed with this app before assuming the per-call parameter is available.

## Support matrix

“Conditional” means the operator is translated by Ontop, but the final query must still meet the unique-type and complete-pushdown contract.

| SPARQL feature | Full-native expectation | Scope of limitation |
|---|---|---|
| `SELECT` | **Supported**, subject to the full-native contract | Full-native |
| `ASK` | **Supported**; interpret SQL row existence as the boolean result | Result/API caveat |
| `CONSTRUCT` | **Supported for reformulation**, but SQL returns bindings rather than a serialized RDF graph | Result/API caveat |
| `DESCRIBE` | **Not available as one-step native reformulation**; rewrite as `CONSTRUCT` or `SELECT` | Ontop's `DESCRIBE` implementation is multi-step |
| Aggregates (`COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `GROUP_CONCAT`, `SAMPLE`) | **Conditional**; output types and aggregate expressions must be inferable and pushable | Full-native/dialect dependent |
| `DISTINCT` / `REDUCED` | **Conditional**; normally lowered to SQL duplicate handling | Full-native/optimizer dependent |
| `ORDER BY`, `LIMIT`, `OFFSET` | **Conditional**; normally pushable, including with `DISTINCT`, but expressions must be SQL-translatable | Full-native/dialect dependent |
| `GROUP BY` / `HAVING` | **Conditional**; grouping, aggregates, and filters must all push down | Full-native/dialect dependent |
| `OPTIONAL` | **Conditional**; commonly lowers to an outer join, with SQL `NULL` for unbound values | Full-native typing/pushdown |
| `UNION` | **Conditional**; a projected variable must have one RDF type across every branch | Full-native typing |
| `MINUS` | **Conditional**; translated by Ontop, then subject to pushdown | Full-native/optimizer dependent |
| `FILTER` | **Conditional**; useful for proving a datatype, but its expression must also push down | Full-native/dialect dependent |
| `VALUES` | **Conditional**; rows must not make a projected variable heterogeneously typed | Full-native typing |
| `BIND` | **Conditional**; the expression must have one inferable output type and push down | Full-native typing/pushdown |
| Subqueries | **Conditional**; the optimized whole query must collapse to one native SQL query | Full-native/optimizer dependent |
| `EXISTS` / `NOT EXISTS` | **Conditional**; supported shapes are translated, but correlated or complex forms may fail translation or pushdown | General translation plus full-native |
| Fixed property paths (`/`, `|`, inverse, fixed length) | **Conditional** when RDF4J/Ontop lowers them to joins or unions | General translation plus full-native |
| Arbitrary-length paths (`*`, `+`) | **Generally unsupported**. The explicit exception is arbitrary-length `rdfs:subClassOf*` support | General Ontop limitation |
| `SERVICE` | **Unsupported** because Ontop cannot delegate a federated SPARQL call to the relational database | General Ontop limitation |
| SPARQL Update | **Unsupported by this read/query reformulation path** | General Ontop/application limitation |
| RDF-star / SPARQL-star | **Unsupported** | General Ontop limitation |

This matrix is guidance, not a parser grammar. Mapping design, ontology rewriting, Ontop version, optimizer decisions, and the Databricks SQL dialect can change whether a particular conditional query succeeds.

## Writing queries that reformulate successfully

### Mixed types across `UNION`

This query is risky because `?value` is a string in one branch and an integer in the other:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?book ?value
WHERE {
  { ?book ex:title ?value }
  UNION
  { ?book ex:pageCount ?value }
}
```

Normalize both branches to one datatype when a text representation is acceptable:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?book ?value
WHERE {
  {
    ?book ex:title ?rawTitle
    BIND(STR(?rawTitle) AS ?value)
  }
  UNION
  {
    ?book ex:pageCount ?rawCount
    BIND(STR(?rawCount) AS ?value)
  }
}
```

Other reliable options are:

- project separate variables, such as `?title` and `?pageCount`, instead of overloading `?value`;
- constrain a dynamic value with `FILTER(datatype(?value) = xsd:string)`;
- run separate queries when preserving distinct RDF datatypes is essential.

Casting changes RDF value semantics. Do not cast merely to silence an error if consumers need the original datatype.

### Unconstrained `?s ?p ?o` output

An unrestricted triple scan can expose many RDF types through `?o`, and potentially heterogeneous term kinds through other variables:

```sparql
SELECT ?s ?p ?o
WHERE {
  ?s ?p ?o
}
LIMIT 100
```

Prefer a known predicate or class shape:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?person ?name
WHERE {
  ?person a ex:Person ;
          ex:name ?name .
}
LIMIT 100
```

If dynamic predicate access is required, restrict it to predicates with compatible output types:

```sparql
PREFIX ex:  <https://example.com/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

SELECT ?s ?p ?o
WHERE {
  ?s ?p ?o .
  VALUES ?p { ex:name ex:displayName }
  FILTER(isIRI(?s))
  FILTER(datatype(?o) = xsd:string)
}
LIMIT 100
```

Useful constraints include a fixed predicate, `VALUES` over compatible predicates, a fixed `rdf:type`, `isIRI`/`isLiteral`, and `datatype`. A filter helps only when Ontop can use it to establish one type and can translate the filter itself.

### `OPTIONAL`, `VALUES`, and `BIND`

`OPTIONAL` is not inherently prohibited. The unbound side normally becomes SQL `NULL`. Problems arise when the same projected variable can receive incompatible RDF types:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?item ?value
WHERE {
  ?item a ex:Item .
  OPTIONAL { ?item ex:label ?value }
  OPTIONAL { ?item ex:quantity ?value }
}
```

Use distinct variables:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?item ?label ?quantity
WHERE {
  ?item a ex:Item .
  OPTIONAL { ?item ex:label ?label }
  OPTIONAL { ?item ex:quantity ?quantity }
}
```

Avoid heterogeneous inline data:

```sparql
# Risky: ?value is both xsd:string and xsd:integer
SELECT ?value
WHERE {
  VALUES ?value { "unknown" 42 }
}
```

Use one datatype:

```sparql
SELECT ?value
WHERE {
  VALUES ?value { "unknown" "42" }
}
```

The same rule applies to conditional expressions. This is risky:

```sparql
BIND(IF(?isKnown, ?numericValue, "unknown") AS ?displayValue)
```

Normalize it:

```sparql
BIND(IF(?isKnown, STR(?numericValue), "unknown") AS ?displayValue)
```

Even a consistently typed `BIND` can fail if its function cannot be translated into Databricks SQL.

### Expressions and functions that do not push down

A SPARQL function may be understood by Ontop but ordinarily evaluated after SQL execution. Full-native mode rejects the query if that post-processing cannot be eliminated.

For example, a complex or implementation-specific function inside a projection is risky:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?item (ex:customNormalize(?raw) AS ?normalized)
WHERE {
  ?item ex:rawValue ?raw
}
```

Try these remedies, in order:

1. simplify the expression and project the source value;
2. replace it with a standard SPARQL function that Ontop maps to a supported Databricks SQL expression;
3. precompute the value in a Databricks view and expose it through the Ontop mapping;
4. encode the transformation directly in the mapping's SQL source when appropriate;
5. use ordinary Ontop execution, if the deployment offers it and Ontop-side post-processing is acceptable.

Treat database-specific date, regular-expression, geospatial, JSON, and string functions as dialect-sensitive. Verify them with a small query before composing them with grouping, ordering, or subqueries.

### Rewrite `DESCRIBE`

Ontop implements `DESCRIBE` as a selection step followed by one or more construction steps. It therefore cannot translate `DESCRIBE` into one native SQL query:

```sparql
PREFIX ex: <https://example.com/>

DESCRIBE ex:person-123
```

Request an explicit, typed graph shape:

```sparql
PREFIX ex: <https://example.com/>

CONSTRUCT {
  ?person ex:name ?name ;
          ex:age ?age .
}
WHERE {
  VALUES ?person { ex:person-123 }
  ?person ex:name ?name .
  OPTIONAL { ?person ex:age ?age }
}
```

Or request bindings directly:

```sparql
PREFIX ex: <https://example.com/>

SELECT ?person ?name ?age
WHERE {
  VALUES ?person { ex:person-123 }
  ?person ex:name ?name .
  OPTIONAL { ?person ex:age ?age }
}
```

Unlike `DESCRIBE`, these forms define exactly which predicates and output types are expected. For an open-ended resource description, use multiple typed queries or perform graph assembly in the client.

### Features without a direct full-native rewrite

#### Arbitrary-length property paths

Most recursive paths such as `ex:parent+` or `ex:partOf*` have no equivalent finite SPARQL rewrite:

```sparql
SELECT ?ancestor
WHERE {
  ex:alice ex:parent+ ?ancestor
}
```

Practical alternatives:

- use a bounded union of fixed paths when a known maximum depth is correct;
- create a recursive Databricks SQL view or materialized transitive-closure table and map it as a normal predicate;
- traverse iteratively in the client.

Only arbitrary-length `rdfs:subClassOf*` has explicit special support in the referenced Ontop translator. Fixed paths that lower to a finite set of joins or unions may work.

#### `SERVICE`

There is no query-only rewrite that makes Ontop or Databricks SQL call an arbitrary SPARQL endpoint. Run the remote query separately and join results in the client, ingest the remote data into Databricks, or expose it through a mapped relational source.

#### RDF-star

Replace quoted triples with a conventional model, such as RDF reification or a domain-specific statement resource, and map that model explicitly. This changes the RDF shape and requires agreement between producers and consumers.

#### SPARQL Update

Use an authorized Databricks SQL/DML path, ingestion job, or application write API. Updating source tables is not safely expressible as a read-only Ontop reformulation, and virtual RDF triples may combine data from several tables.

## Result and API caveats

- **The reformulation endpoint returns SQL, not a SPARQL result serialization.** It does not include enough RDF reconstruction metadata for a generic external SQL consumer to recover every RDF term exactly.
- **`ASK` uses row-existence semantics.** Interpret at least one returned row as `true` and no rows as `false`; do not expect the raw SQL necessarily to return a SPARQL JSON boolean field.
- **`CONSTRUCT` native reformulation is supported.** The SQL returns bindings for the construct template. An external consumer must apply that template, create RDF terms, handle blank-node scope, suppress invalid triples, and serialize the graph. Raw SQL does none of this.
- **Unbound variables appear as SQL `NULL`.** A SPARQL serializer should omit that variable from the binding, not turn it into an empty string or an RDF literal.
- **Address SQL columns by name.** With Ontop 5.5, do not rely on positional column order matching the SPARQL projection. The 5.6 development change preserves projection order, but name-based access remains safer.
- **A provably empty optimized query may have no native SQL to return.** This is an API/optimizer-output edge case, not evidence that its SPARQL syntax is unsupported. The caller should represent an empty result directly.

For this app's execution path, keep the distinction clear: Ontop produces SQL, Databricks executes it, and the application or another consumer turns rows into an API response. Native reformulation deliberately removes Ontop's normal result post-processing.

## Diagnostic checklist

| Error or symptom | Likely cause | What to try |
|---|---|---|
| `the variable ... is not guaranteed to be uniquely typed` | A projected variable can have different RDF types, often through `UNION`, dynamic predicates, `VALUES`, `OPTIONAL`, `IF`, or `COALESCE` | Cast/normalize all branches, split the variable, constrain predicates, or add a `datatype` filter |
| `could not infer the unique type of the variable ...` | Ontop cannot trace the projected variable to a typed RDF-term definition | Project a mapped typed property, remove unnecessary aliases, use an explicit cast, or simplify the projection |
| `the post-processing step could not be eliminated` | A function, term construction, ordering/grouping expression, or optimizer shape remains above native SQL | Simplify expressions; move computation into a Databricks view/mapping; test smaller query fragments; use normal Ontop execution if available |
| `was expected to have an extended projection at the top` | The optimized query lacks the top-level projection shape required by native consumption | Add an explicit `SELECT` projection, remove unusual wrappers, and reduce nested projection/slice combinations |
| `the variables ... are missing an independent definition` | One or more top-level variables depend on a combined or unresolved RDF-term definition | Give each projected variable its own typed binding; avoid reusing one variable for incompatible branches |
| `DESCRIBE queries cannot be translated in one step` | `DESCRIBE` requires Ontop's multi-query selection/construction process | Rewrite as explicit `CONSTRUCT` or `SELECT` |
| `Unsupported arbitrary length path` | The path is recursive and is not the special supported `rdfs:subClassOf*` case | Bound the depth, map a recursive SQL view/closure table, or traverse outside Ontop |
| `Unsupported SPARQL operator` | The query uses a general Ontop limitation such as `SERVICE`, or an unsupported algebra shape | Split federation client-side, remodel the query, or use a different execution engine |
| Successful reformulation but unexpected empty output with no SQL | Optimization proved the query empty | Treat it as an empty result; inspect mappings and constraints if emptiness was unexpected |

A practical isolation sequence:

1. replace `SELECT *` with an explicit projection;
2. reduce the query to one basic graph pattern;
3. add branches, expressions, grouping, and ordering back one at a time;
4. check that every newly projected variable has one RDF type in every branch;
5. test any nontrivial function separately against the deployed Ontop and Databricks versions;
6. inspect mappings when SPARQL alone cannot make the output type unambiguous.

## Sources

The code links below are pinned to Ontop commit [`5ec07573b18513f33dfcd59ac45fe26a81f9cdbd`](https://github.com/ontop/ontop/tree/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd), a development snapshot whose nearest release is `ontop-5.5.0`. They document the investigated behavior but should not be described as a released 5.6 binary.

1. [SQLGeneratorImpl: full-native generation, RDF type extraction, and final `NativeNode` check](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/engine/reformulation/sql/src/main/java/it/unibz/inf/ontop/answering/reformulation/generation/impl/SQLGeneratorImpl.java)
2. [RDF4JDescribeQueryImpl: `DESCRIBE` selection and construction are separate steps](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/core/kg-query/src/main/java/it/unibz/inf/ontop/query/impl/RDF4JDescribeQueryImpl.java)
3. [RDF4JTupleExprTranslator: translated operators, `EXISTS` handling, and the `rdfs:subClassOf*` exception](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/core/kg-query/src/main/java/it/unibz/inf/ontop/query/translation/impl/RDF4JTupleExprTranslator.java)
4. [Full-native RDF4J tests: `OPTIONAL`/`BIND`, unconstrained SPO failure, and datatype-filter success](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/binding/rdf4j/src/test/java/it/unibz/inf/ontop/rdf4j/repository/DestinationFullSQLReformulationTest.java)
5. [Reformulation endpoint tests: `SELECT`, `ASK`, `CONSTRUCT`, mixed types, and projected-column order](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/client/cli/src/test/java/it/unibz/inf/ontop/cli/OntopEndpointReformulateTest.java)
6. [ReformulateController: per-call `forNativeConsumption` parameter](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/client/endpoint/src/main/java/it/unibz/inf/ontop/endpoint/controllers/ReformulateController.java)
7. [Property description for `ontop.reformulateToFullNativeQuery`](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/core/model/src/main/resources/property_description.json)
8. [Full-native property used by the integration tests](https://github.com/ontop/ontop/blob/5ec07573b18513f33dfcd59ac45fe26a81f9cdbd/binding/rdf4j/src/test/resources/destination/dest-full-sql.properties)
9. [PR #933: runtime selection of native consumption and removal of the global property](https://github.com/ontop/ontop/pull/933)
10. [Issue #904: generating SQL without executing it](https://github.com/ontop/ontop/issues/904)
