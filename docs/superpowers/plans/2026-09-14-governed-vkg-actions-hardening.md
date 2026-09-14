# Governed VKG Actions Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Correct all five security and reliability findings in the governed VKG actions feature and submit them in one upstream pull request.

**Architecture:** Keep the existing prepare/confirm service boundary, but make the signed preparation actor-aware and deterministic for request-key actions. The runtime validates VKG subject state, runs synchronous DBSQL in worker threads with server-side timeouts, and requires external targets to atomically enforce a stable invocation identity.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, RDFLib, sqlglot, Databricks SQL Connector, httpx, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-14-governed-vkg-actions-hardening-design.md`

## Global Constraints

- All five findings ship in this pull request; do not split external actions into a follow-up.
- Preserve upstream OBQC and unbound-query behavior.
- Do not add Studio/UI code, customer configuration, or internal identifiers.
- Treat Databricks identifiers as case-insensitive for classifier safety decisions while preserving their original SQL spelling.
- Resolve and bind the effective actor with `session_user()`, never `current_user()`.
- Treat the audit Delta table as replay evidence, not an enforced uniqueness primitive.
- Require target-enforced atomic idempotency for published `REQUEST_KEY` external actions.
- Write each regression test first and observe the expected failure before editing production code.

---

### Task 1: Case-insensitive write-back safety classification

**Files:**
- Modify: `src/app/actions/r2rml_classifier.py`
- Test: `src/app/tests/test_r2rml_writeback_classifier.py`

**Interfaces:**
- Consumes: R2RML aliases and Databricks identifiers parsed by sqlglot.
- Produces: `_identifier_key(value: str) -> str` and conservative case-insensitive classifier decisions.

- [x] **Step 1: Add failing case-variant regression tests**

Add one mapping where the subject resolves through alias `ID` and the value through alias `id`; assert `IDENTITY_COLUMN_UPDATE`. Add two property maps whose aliases resolve to `Supplier_Name` and `supplier_name`; assert both receive `COUPLED_SOURCE_COLUMN`.

```python
assert classification.writable is False
assert "IDENTITY_COLUMN_UPDATE" in classification.reasons

assert all(item.writable is False for item in classifications)
assert all("COUPLED_SOURCE_COLUMN" in item.reasons for item in classifications)
```

- [x] **Step 2: Run the two tests and verify RED**

Run: `uv run --project src/app pytest src/app/tests/test_r2rml_writeback_classifier.py -q`

Expected: the new classifications are incorrectly writable because mixed-case identifiers compare unequal.

- [x] **Step 3: Normalize every identifier comparison**

Add:

```python
def _identifier_key(value: str) -> str:
    return value.casefold()
```

Key `alias_to_column` and `non_column_aliases` by normalized alias, normalize alias lookups, compare key/value and `WHERE ... IS NOT NULL` columns by normalized column value, and construct coupled-source keys from normalized table FQN parts and normalized columns. Keep original column names in `WriteBackTarget`.

- [x] **Step 4: Run classifier tests and verify GREEN**

Run: `uv run --project src/app pytest src/app/tests/test_r2rml_writeback_classifier.py -q`

Expected: all classifier tests pass.

- [x] **Step 5: Commit the classifier correction**

```bash
git add src/app/actions/r2rml_classifier.py src/app/tests/test_r2rml_writeback_classifier.py
git commit -m "fix: normalize write-back identifiers"
```

### Task 2: Server-side DBSQL statement timeouts

**Files:**
- Modify: `src/app/actions/dbsql.py`
- Modify: `src/app/sparql_execute.py`
- Test: `src/app/tests/test_action_primitives.py`
- Test: `src/app/tests/test_sparql_execute_errors.py`

**Interfaces:**
- Produces: `run_user_sql(..., *, timeout_seconds: int | None = None)`.
- Produces: `run_sql(..., statement_timeout_seconds: int | None = None)` and `execute_sparql_query(..., statement_timeout_seconds: int | None = None)`.

- [x] **Step 1: Add failing same-cursor timeout tests**

Patch `dbsql.connect` with recording connection/cursor doubles. Call `run_user_sql("SELECT 1", ..., timeout_seconds=17)` and assert:

```python
assert cursor.calls == [
    ("SET STATEMENT_TIMEOUT = 17", None),
    ("SELECT 1", None),
]
```

Add the equivalent `sparql_execute.run_sql` assertion. Add validation assertions that zero, negative, and boolean timeouts raise `ValueError` before connecting.

- [x] **Step 2: Run timeout tests and verify RED**

Run: `uv run --project src/app pytest src/app/tests/test_action_primitives.py src/app/tests/test_sparql_execute_errors.py -q`

Expected: the new keyword arguments are rejected or no `SET STATEMENT_TIMEOUT` call is recorded.

- [x] **Step 3: Implement the timeout primitive**

Validate with:

```python
if timeout_seconds is not None and (
    not isinstance(timeout_seconds, int)
    or isinstance(timeout_seconds, bool)
    or timeout_seconds <= 0
):
    raise ValueError("timeout_seconds must be a positive integer")
```

On the same cursor, execute `SET STATEMENT_TIMEOUT = <integer>` before the requested statement. Thread the optional value through `execute_sparql_query` to its `asyncio.to_thread(run_sql, ...)` call without changing existing callers.

- [x] **Step 4: Run timeout tests and verify GREEN**

Run: `uv run --project src/app pytest src/app/tests/test_action_primitives.py src/app/tests/test_sparql_execute_errors.py -q`

Expected: all targeted tests pass.

- [x] **Step 5: Commit the timeout primitive**

```bash
git add src/app/actions/dbsql.py src/app/sparql_execute.py src/app/tests/test_action_primitives.py src/app/tests/test_sparql_execute_errors.py
git commit -m "fix: enforce action SQL timeouts"
```

### Task 3: Actor-bound tokens, audit reads, and non-blocking handlers

**Files:**
- Modify: `src/app/actions/models.py`
- Modify: `src/app/actions/tokens.py`
- Modify: `src/app/actions/audit.py`
- Modify: `src/app/actions/service.py`
- Test: `src/app/tests/test_action_primitives.py`
- Test: `src/app/tests/test_action_service.py`

**Interfaces:**
- Produces: `resolve_effective_user(token: str, settings: Settings, timeout_seconds: int | None = None) -> str`.
- Produces: `PrepareTokenPayload.effective_user: str`, payload version 2, and `ActionAuthorizationError` with HTTP 403.
- Produces: `ActionAuditRecord.effective_user: str` and actor-filtered audit lookups.

- [x] **Step 1: Add failing actor and event-loop tests**

Inject an actor resolver returning `user-a@example.com` for token A and `user-b@example.com` for token B. Prepare as A, confirm as B, and assert `ActionAuthorizationError` plus zero update/function calls. Add audit SQL assertions for `session_user()` and `effective_user = session_user()`.

For the event loop, use a SQL runner that sleeps for 0.2 seconds, schedule a 0.02-second heartbeat beside `service.prepare`, and assert the heartbeat fires before 0.1 seconds.

- [x] **Step 2: Run actor and heartbeat tests and verify RED**

Run: `uv run --project src/app pytest src/app/tests/test_action_primitives.py src/app/tests/test_action_service.py -q`

Expected: user B confirms successfully, audit SQL uses `current_user()`, and the heartbeat is delayed by the synchronous runner.

- [x] **Step 3: Implement actor resolution and token binding**

Implement the default resolver with `run_user_sql("SELECT session_user() AS effective_user", ...)`; require exactly one non-empty string. Add `effective_user` to token payloads and bump `_SUPPORTED_VERSION` to 2. Add:

```python
class ActionAuthorizationError(ActionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="ACTION_FORBIDDEN", status_code=403)
```

Resolve actors in public `prepare` and `confirm`, compare the confirmer with the signed actor before loading state or executing an action, and validate persisted audit actor values.

- [x] **Step 4: Move synchronous service work to worker threads**

Keep the public methods async and move their synchronous bodies into `_prepare_sync` and `_confirm_sync`:

```python
actor = await asyncio.to_thread(self._actor_resolver, token, self._settings, timeout)
return await asyncio.to_thread(self._prepare_sync, request, token, actor, action)
```

Use the same pattern for confirmation. Pass `action.timeout_seconds` to SQL, audit, actor, read, update, and function operations. Do not hold an asyncio event-loop thread while acquiring a `threading.Lock`.

- [x] **Step 5: Change audit identity handling**

Replace `current_user()` with `session_user()` in inserts. Select `effective_user` in audit reads, add `AND effective_user = session_user()` to prepare/history predicates, and populate `ActionAuditRecord.effective_user`. Reject audit rows whose effective user differs from the signed actor.

- [x] **Step 6: Run actor and heartbeat tests and verify GREEN**

Run: `uv run --project src/app pytest src/app/tests/test_action_primitives.py src/app/tests/test_action_service.py -q`

Expected: all targeted tests pass and the heartbeat remains responsive.

- [x] **Step 7: Commit actor and async hardening**

```bash
git add src/app/actions/models.py src/app/actions/tokens.py src/app/actions/audit.py src/app/actions/service.py src/app/tests/test_action_primitives.py src/app/tests/test_action_service.py
git commit -m "fix: bind actions to the preparing user"
```

### Task 4: External subject/class preconditions

**Files:**
- Create: `src/app/actions/subjects.py`
- Modify: `src/app/actions/service.py`
- Modify: `src/app/main.py`
- Modify: `src/app/sparql_execute.py`
- Test: `src/app/tests/test_action_service.py`
- Test: `src/app/tests/test_action_main_integration.py`

**Interfaces:**
- Produces: async `check_subject_class(subject_iri: str, class_iri: str, token: str, timeout_seconds: int) -> bool` callable accepted by `ActionService`.
- Consumes: `execute_sparql_query(..., statement_timeout_seconds=timeout_seconds)`.

- [x] **Step 1: Add failing precondition tests**

Create an external action with `bound_class_iri`. Inject a checker that returns false at prepare and assert `ActionValidationError`. Inject results `[True, False]`, prepare then confirm, and assert `ActionConflictError` with zero governed-function invocations. Assert a published external action without `boundClass` fails closed.

- [x] **Step 2: Run precondition tests and verify RED**

Run: `uv run --project src/app pytest src/app/tests/test_action_service.py src/app/tests/test_action_main_integration.py -q`

Expected: external preparation ignores the checker or the constructor does not accept it.

- [x] **Step 3: Implement the VKG subject checker**

Build a bound query with conservatively validated absolute IRIs:

```sparql
SELECT ?subject WHERE {
  VALUES ?subject { <subject-iri> }
  ?subject a <bound-class-iri> .
}
LIMIT 1
```

Run it through `execute_sparql_query` under the forwarded token and action timeout. Return true only when the result contains a binding; convert execution errors to `ActionUnavailableError` without exposing native SQL.

- [x] **Step 4: Enforce the checker twice**

Require `bound_class_iri` and the checker for external actions. Call it after actor resolution during prepare and after actor comparison during confirm. Use validation failure at prepare and conflict at confirm.

- [x] **Step 5: Wire the checker into app lifespan**

Pass a closure using `app.state.http_client`, `settings`, and `ontop_manager` into the available `ActionService`; keep the unavailable service inert. Exercise the lifespan test with a checker dependency that does not contact a workspace.

- [x] **Step 6: Run precondition and integration tests and verify GREEN**

Run: `uv run --project src/app pytest src/app/tests/test_action_service.py src/app/tests/test_action_main_integration.py -q`

Expected: all targeted tests pass.

- [x] **Step 7: Commit semantic preconditions**

```bash
git add src/app/actions/subjects.py src/app/actions/service.py src/app/main.py src/app/sparql_execute.py src/app/tests/test_action_service.py src/app/tests/test_action_main_integration.py
git commit -m "fix: validate external action subjects"
```

### Task 5: Durable request-key invocation contract

**Files:**
- Modify: `src/app/actions/models.py`
- Modify: `src/app/actions/service.py`
- Modify: `src/app/actions/catalog.py`
- Modify: `src/app/tests/test_action_catalog_loading.py`
- Modify: `src/app/tests/test_action_service.py`
- Modify: `src/app/tests/test_action_routes.py`
- Modify: `src/app/tests/test_mcp_action_tools.py`

**Interfaces:**
- Produces: signed `PrepareTokenPayload.invocation_id` and `.request_hash`.
- Produces: deterministic `_invocation_id(effective_user, action_iri, client_key)`, `_request_hash(request)`, and `_prepare_id(invocation_id)` helpers.
- Changes governed-function signature to `(object_uid, params_json, invocation_id, request_hash)`.

- [x] **Step 1: Add failing deterministic-idempotency tests**

Prepare the same external request twice and assert equal prepare IDs and invocation IDs. Confirm both and assert one logical target effect. Reuse the key with changed params and assert HTTP 409. Recreate two services over shared audit state and a target double that atomically stores by invocation ID; confirm concurrently and assert one stored effect and equal results.

Add malformed-envelope tests requiring exact `invocation_id` and `request_hash` echoes.

- [x] **Step 2: Run idempotency tests and verify RED**

Run: `uv run --project src/app pytest src/app/tests/test_action_catalog_loading.py src/app/tests/test_action_service.py src/app/tests/test_action_routes.py src/app/tests/test_mcp_action_tools.py -q`

Expected: repeated prepares have random IDs, conflicting key reuse is accepted, and the function receives the old three-argument contract.

- [x] **Step 3: Implement deterministic invocation identity**

Strip outer key whitespace, preserve its case, and compute the exact hashes from the design using UTF-8 and NUL separators. Add both hashes to the version-2 signed token. Use the deterministic prepare ID for `REQUEST_KEY`; retain a random prepare ID only for action kinds that do not use this strategy.

- [x] **Step 4: Replay or reject an existing preparation**

Before writing a new PREPARED audit row, load the actor-filtered deterministic preparation from memory or audit. Reconstruct and return an identical request with a fresh signed token. Raise `ActionConflictError` if action, actor, subject, params hash, request hash, or client key differs. Have audit lookup deterministically choose the earliest PREPARED row so a cross-replica duplicate insert cannot make recovery ambiguous.

- [x] **Step 5: Enforce the governed-target contract**

Accept only `REQUEST_KEY` for published external actions. Invoke the target with four named parameters and require every terminal envelope to contain:

```python
assert result["invocation_id"] == prepared.invocation_id
assert result["request_hash"] == prepared.request_hash
```

Treat missing/mismatched echoes or an unknown status as unavailable, audit the failure, and never claim exactly-once behavior from the runtime audit table alone.

- [x] **Step 6: Run idempotency and adapter tests and verify GREEN**

Run: `uv run --project src/app pytest src/app/tests/test_action_catalog_loading.py src/app/tests/test_action_service.py src/app/tests/test_action_routes.py src/app/tests/test_mcp_action_tools.py -q`

Expected: all targeted tests pass.

- [x] **Step 7: Commit the durable contract**

```bash
git add src/app/actions/models.py src/app/actions/service.py src/app/actions/catalog.py src/app/tests/test_action_catalog_loading.py src/app/tests/test_action_service.py src/app/tests/test_action_routes.py src/app/tests/test_mcp_action_tools.py
git commit -m "fix: enforce durable action idempotency"
```

### Task 6: Documentation, live fixture, and full verification

**Files:**
- Modify: `README.md`
- Modify: `mappings/samples/actions.ttl`
- Modify: `scripts/live-action-e2e.py`
- Modify: `src/app/tests/test_action_main_integration.py`
- Modify: `docs/superpowers/plans/2026-09-14-governed-vkg-actions-hardening.md`

**Interfaces:**
- Documents the four-argument target contract, actor binding, subject checks, and timeout behavior.
- Provides a side-effect-free live function contract and a two-replica test target
  that atomically replays by invocation ID and request hash.

- [x] **Step 1: Update the generic sample and E2E fixture**

Make the sample external action declare `boundClass`, `REQUEST_KEY`, and a positive timeout. Update the live side-effect-free governed function to accept and echo `invocation_id` and `request_hash`. Use the two-replica service test's locked target ledger to persist one result per invocation and reject hash conflicts.

- [x] **Step 2: Update README guarantees and deployment schema**

Replace any `current_user()` examples with `session_user()`. State that the audit table requires `effective_user`, that audit constraints are informational, and that the target—not the runtime Delta audit—must atomically deduplicate external effects.

- [x] **Step 3: Run the complete automated suite**

Run:

```bash
(cd src/app && uv run python -m pytest tests -q)
(cd src/app && uv run ruff check .)
git diff -z --name-only origin/main...HEAD -- '*.py' | xargs -0 uv run --project src/app ruff format --check
uv run --project src/app python -m compileall -q src/app scripts/live-action-e2e.py
uv run --project src/app python scripts/live-action-e2e.py --help
```

Expected: all tests and checks pass with no new warnings or errors.

- [x] **Step 4: Run bundle and repository checks**

Run:

```bash
databricks bundle validate -t volume
git diff --check origin/main...HEAD
git status --short
```

Expected: the volume target validates, the diff has no whitespace errors, and only the intentional plan update is uncommitted. If app validation reaches the known missing test UC volume, record that environmental limitation without weakening tests.

- [x] **Step 5: Mark this plan complete and commit docs/fixture changes**

Change each completed checkbox to `[x]`, then run:

```bash
git add README.md mappings/samples/actions.ttl scripts/live-action-e2e.py src/app/tests/test_action_main_integration.py docs/superpowers/plans/2026-09-14-governed-vkg-actions-hardening.md
git commit -m "docs: finalize governed action contract"
```

- [x] **Step 6: Address final security and reliability review**

Authenticate persisted audit evidence with an actor-bound, domain-separated
HMAC; require a Unity Catalog row filter for audit confidentiality; strictly
validate recovered preparation data against the current request and target;
replay terminal external outcomes before mutable subject checks; bound Ontop
HTTP reformulation with the action timeout; and align the live audit DDL with
the documented `VARIANT` columns.

- [ ] **Step 7: Review the final diff and open the one PR**

Review `git diff --stat origin/main...HEAD` and `git diff origin/main...HEAD` for customer identifiers, secrets, UI code, and unrelated changes. Push `feat/governed-vkg-actions` to the user's fork and open one pull request against `aktungmak/ontop-databricks-apps:main` describing all five hardening fixes and the verification evidence.
