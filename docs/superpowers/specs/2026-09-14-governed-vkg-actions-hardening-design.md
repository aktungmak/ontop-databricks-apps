# Governed VKG Actions Hardening Design

## Context

The governed VKG actions implementation already provides an action catalog,
R2RML write-back classification, prepare/confirm APIs, user-token DBSQL, audit
events, REST and MCP adapters, and runtime configuration. Review found five
security and reliability gaps that must be corrected before the feature is
submitted upstream:

1. Databricks identifiers were compared case-sensitively in the write-back
   classifier.
2. `REQUEST_KEY` did not define a durable cross-process idempotency boundary.
3. External actions did not prove that their subject was an instance of the
   declared `boundClass`, or repeat that proof at confirmation.
4. A preparation token was not bound to the Databricks user who prepared it.
5. Synchronous DBSQL blocked async handlers and catalog timeouts were unused.

All five corrections belong to the same pull request. The UI and unrelated
monorepo changes remain out of scope.

## Security and Reliability Boundary

The app authenticates each prepare and confirm operation with the forwarded
Databricks user token. It resolves the effective principal by running
`SELECT session_user()` through DBSQL with that token. The resolved principal,
action, subject, request hashes, catalog fingerprint, expiry, and deterministic
invocation identity form the signed preparation boundary.

The app can prevent duplicate orchestration and replay recorded outcomes, but
it cannot by itself guarantee exactly-once behavior for an external side effect:
a process can fail after the external target commits and before the app records
completion. Therefore, published external actions using `REQUEST_KEY` must call
a target that atomically enforces the invocation identity. The runtime and the
target jointly provide durable idempotency; the audit Delta table is evidence
and replay state, not a uniqueness primitive.

## Component Changes

### R2RML classifier

All comparisons involving unquoted Databricks catalog, schema, table, alias,
and column identifiers use a `casefold()` comparison key. Original spellings
are retained in `WriteBackTarget` for quoted SQL generation. Duplicate aliases,
identity-column checks, coupled-source checks, computed-column checks, and
`IS NOT NULL` guard comparisons all use the normalized key. Consequently,
`ID` and `id` represent the same source identifier and cannot bypass a refusal.

### Actor-bound preparation

An actor resolver runs `SELECT session_user()` with the forwarded token at both
prepare and confirm. Preparation token payload version 2 includes the resolved
`effective_user` and the deterministic `invocation_id`. Confirmation resolves
the actor again and refuses with HTTP 403 before any action execution when it
does not match the signed actor.

Audit reads select and validate `effective_user` and include
`effective_user = session_user()` in their predicates. This prevents another
user from loading preparation or confirmation history even if they learn a
prepare ID. Audit inserts continue deriving `effective_user` from
`session_user()` rather than trusting a request value.

### External subject precondition

Every published external action must declare `boundClass`. An injected async
subject checker asks the active VKG whether the supplied subject is currently
an instance of that class. It uses a bound-subject SPARQL query, the caller's
forwarded token, and the action timeout. It fails closed when Ontop or DBSQL is
unavailable.

The check runs at prepare and again at confirm. Absence at prepare is a request
validation failure. Absence at confirm is a conflict because the semantic state
changed after the preview. Write-back actions retain their existing subject
template, source-row, old-value, and guarded-update checks.

### Durable external-action idempotency

For `REQUEST_KEY`, the client key is a case-sensitive opaque string after outer
whitespace is removed. The runtime computes:

```
invocation_id = "v1:" + sha256(effective_user + NUL + action_iri + NUL + client_key)
request_hash  = sha256(canonical_json({"subject_iri": subject_iri, "params": params}))
prepare_id    = "prepare_" + sha256(invocation_id)
```

The deterministic prepare ID makes sequential retries converge on the same
persisted preparation. If the same actor, action, and client key are reused with
another subject or parameter set, prepare returns HTTP 409. Identical retries
reconstruct the existing preview and issue a fresh, actor-bound token without
creating another logical invocation.

The governed function contract becomes four arguments:

```
(object_uid STRING, params_json STRING, invocation_id STRING, request_hash STRING)
```

The target integration must atomically claim `invocation_id` together with
`request_hash`, perform the side effect at most once, persist the result, and
return the persisted result for an identical retry. Reuse of an invocation ID
with a different request hash must return a conflict without performing an
effect. A successful or failed result envelope must echo both `invocation_id`
and `request_hash`; the runtime rejects mismatched or missing echoes and records
a failed confirmation.

The runtime checks durable audit history before invoking the function and
replays a terminal result when one exists. An in-process lock prevents duplicate
calls within one replica. Separate replicas may both enter the governed function
during a race, which is why atomic target enforcement is mandatory. The same
invocation ID makes those calls one logical target operation.

Only the documented `REQUEST_KEY` strategy is accepted for published external
actions. Unknown or absent durable strategies fail closed. Documentation and
the live E2E fixture will show the target-side ledger contract explicitly and
will not claim that Delta informational constraints provide uniqueness.

### Async execution and timeouts

No synchronous DBSQL operation runs on the event-loop thread. Public action
methods keep their async API, while actor resolution, audit access, source reads,
updates, and governed-function execution run in worker threads. The async VKG
subject check uses the existing non-blocking reformulation path and moves its
DBSQL execution to a worker thread.

The DBSQL primitive accepts a positive timeout. On the same connection and
cursor used for the action statement it first executes:

```
SET STATEMENT_TIMEOUT = <timeout_seconds>
```

The parsed per-action `timeoutSeconds` is passed to subject checks, source
reads, guarded updates, audit operations associated with the action, and
external-function calls. Timeout failures are sanitized, audited as failures
when possible, and surfaced as action-unavailable responses.

## Confirmation Flow

1. Verify token signature, version, and expiry.
2. Resolve `session_user()` under the confirming token.
3. Compare it with the signed preparing actor; refuse mismatches.
4. Load the current action and require it to remain published.
5. For external actions, recheck subject membership in `boundClass`.
6. Load actor-filtered preparation and confirmation audit state.
7. Verify signed hashes, invocation identity, and catalog fingerprint.
8. Replay a terminal audit result when available.
9. Revalidate write-back state or invoke the target with the stable invocation
   ID and request hash.
10. Validate affected rows or the external result envelope, then record the
    terminal audit event.

## Error Semantics

- Actor mismatch: HTTP 403, no action execution.
- Reused client key with a different request: HTTP 409.
- Subject not in `boundClass` at prepare: HTTP 400.
- Subject no longer in `boundClass` at confirm: HTTP 409.
- Unsupported idempotency strategy or missing target contract: fail closed.
- SQL or semantic-check timeout: HTTP 503 with a sanitized message.
- Existing terminal invocation: return the recorded response without another
  logical side effect.

## Testing and Verification

Each correction is introduced with a regression test that is observed failing
before production code changes:

- case-variant identity and coupled-column classifier refusals;
- identical repeated prepare calls converging on one invocation, conflicting
  key reuse returning 409, restart replay, and two-replica target deduplication;
- external subject rejection at prepare and state-change rejection at confirm;
- user B being unable to confirm user A's preparation or read their audit rows;
- event-loop heartbeat continuing while a slow SQL runner executes;
- `SET STATEMENT_TIMEOUT` occurring on the same cursor before the statement;
- external result-envelope invocation and request hash validation.

After targeted tests, the complete Python test suite, Ruff lint and formatting,
Python compilation, E2E CLI smoke test, bundle validation, secret scan, and an
independent code review must pass before the branch is pushed and the single PR
is opened against `aktungmak/ontop-databricks-apps`.
