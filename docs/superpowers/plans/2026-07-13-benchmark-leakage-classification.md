# Benchmark Leakage Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically block memory derived from the exact benchmark example currently being evaluated by comparing complete `(eval_suite, input_hash)` identities.

**Architecture:** `MemoryContext` carries the current input hash; source-derived ephemeral `MemoryItem` values carry a complete source pair. The deterministic System Gate compares exact complete pairs, while the store enriches both preparation and finalization candidates and records current identity plus the automatic reason in existing usage evidence.

**Tech Stack:** Python 3.11+ frozen dataclasses, dependency-free policy/store code, JSON Schema 2020-12, pytest 8+, existing snapshot/PostgreSQL adapters.

## Global Constraints

- Benchmark example identity is exactly `(eval_suite, input_hash)`.
- A context input hash requires an eval suite; eval suite without input hash remains valid.
- Source identity fields are both present or both absent and are never persisted.
- Incomplete context or source identity never triggers a guessed leakage match.
- Exact pair equality blocks with `memory originates from current benchmark example` in every mode.
- Static `sensitive` and `eval_leaking` checks retain precedence and existing reasons.
- Different hashes in one suite and equal hashes in different suites remain allowed by this rule.
- Input hash is identity evidence, never a lesson/policy scope field and never prompt/snippet text.
- Legacy contexts and memory items without identity remain behaviorally unchanged.
- Snapshot version remains 2; PostgreSQL schema version remains 1; `schemas/postgres.sql` does not change.
- No new trace/lesson/failure/policy/usage-log persisted dataclass field or database column.
- Add no dependency and use `apply_patch` for manual edits.

---

### Task 1: Context and ephemeral source identity contracts

**Files:**
- Modify: `src/trace_backed_memory/models.py`
- Modify: `src/trace_backed_memory/policy.py`
- Modify: `schemas/memory_context.schema.json`
- Test: `tests/test_policy.py`
- Test: `tests/test_examples_and_schema.py`

**Interfaces:**
- Consumes: existing `eval_suite`, `Trace.input_hash`, context parser, and memory contract validation.
- Produces: `MemoryContext.input_hash`, `MemoryItem.source_eval_suite`, `MemoryItem.source_input_hash`, strict pair validation, and schema/parser support.

- [ ] **Step 1: Write failing context identity tests**

Test direct and parsed `MemoryContext` values with:

- valid `eval_suite="suite"`, `input_hash="sha256:example"`;
- eval suite without input hash remaining valid;
- input hash without eval suite rejected with
  `context input_hash requires eval_suite`;
- bool, bytes, number, list, dict, empty, and overlong input hashes rejected,
  while whitespace behavior remains exactly the existing context-string
  contract;
- appending the dataclass field preserving an existing fully positional
  construction's values.

Extend JSON Schema tests so a valid complete pair passes, input-hash-only
fails, and legacy contexts still pass.

- [ ] **Step 2: Write failing memory source identity tests**

Construct `MemoryItem` values and run `system_gate()` to prove:

- a valid complete source pair passes contract validation when context differs;
- source eval suite only and source input hash only are contract errors;
- non-string, empty, and overlong source values are rejected;
- the source fields cannot be supplied through memory scope;
- legacy items with both fields absent preserve current behavior.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
python -m pytest tests/test_policy.py tests/test_examples_and_schema.py -q -k "input_hash or benchmark_source_identity"
```

Expected: model constructor/schema/parser failures because the fields do not
exist.

- [ ] **Step 4: Add model fields without positional breakage**

Append `input_hash: str | None = None` as the final `MemoryContext` field.
Append these final `MemoryItem` fields:

```python
source_eval_suite: str | None = None
source_input_hash: str | None = None
```

- [ ] **Step 5: Implement context and memory contracts**

Add `input_hash` to context parsing/validation but explicitly keep it outside
scope fields and LLM prompt context lines. After normal string validation,
reject a present input hash without eval suite.

In `_memory_item_contract_error()`, validate both source values as exact
bounded non-empty strings or `None`, then require both-or-neither. Do not add
the automatic block yet.

Add the optional schema property plus JSON Schema `if`/`then` requirement.

- [ ] **Step 6: Run focused and full policy tests**

```powershell
python -m pytest tests/test_policy.py tests/test_examples_and_schema.py -q
```

Expected: all selected tests pass and legacy schema examples remain valid.

- [ ] **Step 7: Commit Task 1**

```powershell
git add src/trace_backed_memory/models.py src/trace_backed_memory/policy.py schemas/memory_context.schema.json tests/test_policy.py tests/test_examples_and_schema.py
git commit -m "feat: add benchmark example identity contracts"
```

### Task 2: Source propagation, automatic gate, and injection guardrail

**Files:**
- Modify: `src/trace_backed_memory/lifecycle.py`
- Modify: `src/trace_backed_memory/policy.py`
- Test: `tests/test_lifecycle.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: Task 1 model/contract fields.
- Produces: complete trace identity propagation, automatic System Gate reason, and context-aware direct injection.

- [ ] **Step 1: Write failing lifecycle propagation tests**

For `memory_item_from_failure_case(case, trace)`, assert complete source trace
identity propagates and any incomplete source pair produces both fields as
`None`.

Extend `memory_item_from_lesson()` tests for keyword-only
`source_trace=trace`, with the same complete/incomplete behavior. Confirm the
existing one-argument helper returns no source identity and project policy
memory remains identity-free.

- [ ] **Step 2: Write failing System Gate classification tests**

Parametrize every runtime mode. A complete equal pair must be blocked with:

```text
memory originates from current benchmark example
```

Add controls for different hash/same suite, same hash/different suite,
context without input hash, memory without source identity, and manually
`eval_leaking=True`. The static flag must retain `memory may leak eval data`.

Assert same-example memory passed to `build_llm_gate_prompt()` is rejected
before the prompt can contain either hash.

- [ ] **Step 3: Write failing direct-injection tests**

For source-identified memory and an allowing decision, assert:

- omitted context fails with `context is required for benchmark source identity`;
- same-example context fails with the automatic reason;
- different-example valid context allows injection without rendering current
  or source input hashes;
- legacy identity-free memory still injects without context.

- [ ] **Step 4: Run focused tests and verify RED**

```powershell
python -m pytest tests/test_lifecycle.py tests/test_policy.py -q -k "benchmark or source_identity or current_example"
```

Expected: propagation/classification/context-injection failures.

- [ ] **Step 5: Implement complete source propagation**

Add one private lifecycle helper that returns `(eval_suite, input_hash)` only
when both raw trace values are valid non-empty strings. Use it in failure-case
memory and in the optional keyword-only `source_trace` path for lesson memory.

- [ ] **Step 6: Implement the automatic gate rule**

Add a private exact-pair helper in `policy.py`. Invoke it after static
sensitive/eval-leaking checks and before scope/mode checks. Never classify an
incomplete pair.

- [ ] **Step 7: Harden injection**

Add `context: MemoryContext | None = None` to `build_injection_snippet()`. If
any memory has source identity, require and validate context, then apply the
automatic benchmark rule before rendering. Preserve legacy behavior when no
memory carries source identity.

- [ ] **Step 8: Run focused and broader lifecycle/policy suites**

```powershell
python -m pytest tests/test_lifecycle.py tests/test_policy.py -q
```

Expected: all tests pass and no hash appears in generated prompts/snippets.

- [ ] **Step 9: Commit Task 2**

```powershell
git add src/trace_backed_memory/lifecycle.py src/trace_backed_memory/policy.py tests/test_lifecycle.py tests/test_policy.py
git commit -m "feat: block current benchmark example memory"
```

### Task 3: Store preparation, finalization, and audit integration

**Files:**
- Modify: `src/trace_backed_memory/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: Task 2 lifecycle and gate APIs.
- Produces: enriched store candidates in prepare/finalize, trace identity binding, usage audit evidence, and context-aware snippet generation.

- [ ] **Step 1: Write failing candidate enrichment tests**

Create one verified case and one active lesson from a trace with a complete
benchmark identity. In debug/repair contexts, assert `candidate_memories()`
returns both items with the exact source pair. Confirm a source trace with an
incomplete pair yields neither field and project policy candidates remain
identity-free.

- [ ] **Step 2: Write failing prepare/finalize audit tests**

Prepare memory for the same example and assert:

- source-derived candidates remain in candidate IDs;
- System Gate blocks both with the automatic reason;
- neither appears in system-allowed IDs or LLM prompt;
- project policy remains governed by existing rules.

Finalize with a matching trace and decision, then assert usage context includes
`eval_suite` and `input_hash`, candidate IDs/statuses remain present, and
`system_blocked_reasons` records the automatic reason.

Add a different-example control that allows a procedural lesson through the
normal LLM decision and builds a snippet without either hash.

- [ ] **Step 3: Write failing reconstruction and trace-binding tests**

Assert finalization reconstructs lesson identity and reruns the same block.
Then provide traces with mismatched eval suite and mismatched input hash;
each must fail before consuming the pending request, allowing a subsequent
matching finalize to succeed.

- [ ] **Step 4: Write failing imported usage-log tests**

Usage logs containing `context.input_hash` must also contain `eval_suite`, and
both values must match the linked trace. Add missing-pair and each mismatch
case, plus a legacy log without input hash.

- [ ] **Step 5: Run store tests and verify RED**

```powershell
python -m pytest tests/test_store.py -q -k "benchmark_example or input_hash_audit or source_identity"
```

Expected: missing enrichment/audit/trace-binding behavior.

- [ ] **Step 6: Enrich lesson reconstruction everywhere**

Add one store helper resolving lesson -> case -> trace and calling
`memory_item_from_lesson(..., source_trace=trace)`. Use it in metadata candidate
construction and `_memory_items()` finalization/log reconstruction.

Failure-case items use Task 2 propagation directly. Project policies stay
unchanged.

- [ ] **Step 7: Bind context, trace, injection, and logs**

When `context.input_hash` exists, `_validate_trace_context()` must compare
trace eval suite and input hash. Pass request context into
`build_injection_snippet()` during finalization.

Usage-log validation must require a logged input hash to have eval suite and
require both to equal the linked trace. Preserve legacy logs with no input
hash. Ensure all validation precedes pending-request consumption and usage-log
append.

- [ ] **Step 8: Run focused and full store tests**

```powershell
python -m pytest tests/test_store.py -q
```

Expected: all store tests pass, including stale/failure atomicity cases.

- [ ] **Step 9: Commit Task 3**

```powershell
git add src/trace_backed_memory/store.py tests/test_store.py
git commit -m "feat: audit automatic benchmark leakage blocks"
```

### Task 4: Persistence compatibility and public workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/usage-policy.md`
- Modify: `docs/mvp-roadmap.md`
- Modify: `examples/trace.example.json`
- Modify: `tests/test_readme_api.py`
- Modify: `tests/test_examples_and_schema.py`
- Modify: `tests/test_postgres_repository.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Consumes: Tasks 1-3 complete behavior.
- Produces: executable benchmark-safe runtime workflow, Phase 11 docs, and verified snapshot/PostgreSQL compatibility.

- [ ] **Step 1: Add an executable README test**

Extend the runtime workflow with source and current traces sharing
`eval_suite/input_hash`, a derived lesson, and a current `MemoryContext` pair.
Assert preparation blocks the lesson automatically, the LLM prompt excludes
it and both hashes, and final usage evidence contains current input hash plus
the automatic reason.

- [ ] **Step 2: Run README test and verify RED**

```powershell
python -m pytest tests/test_readme_api.py -q
```

Expected: failure until README executes the new workflow.

- [ ] **Step 3: Add snapshot and PostgreSQL compatibility tests**

Round-trip a store with source trace input hash and usage-log context input
hash through snapshot v2. Assert ephemeral `MemoryItem` source fields are not
serialized.

In PostgreSQL repository tests, sync and load the same store and assert trace
input hash plus usage context/block reason round-trip through existing columns
and JSONB without SQL/schema changes.

Keep `schemas/postgres.sql` byte-for-byte unchanged.

- [ ] **Step 4: Update example and documentation**

Give `examples/trace.example.json` a non-empty opaque input hash matching its
eval suite. Document:

- exact pair identity and caller hash responsibilities;
- incomplete identities never guessed;
- static flag precedence and every-mode automatic rule;
- source identity enrichment and no prompt/snippet disclosure;
- context/trace binding and audit evidence;
- input hash is not memory scope;
- snapshot 2/PostgreSQL 1 compatibility and no new persisted memory fields.

Add `Phase 11: Benchmark example leakage classification (implemented)` to the
roadmap.

- [ ] **Step 5: Add documentation contract tests**

Assert README, architecture, policy, and roadmap publish all boundaries,
memory-context schema contains the optional pair rule, and PostgreSQL schema
contains no new benchmark column.

- [ ] **Step 6: Run compatibility suites**

```powershell
python -m pytest tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_store.py tests/test_postgres_repository.py -q
```

Expected: all pass with PostgreSQL schema version 1 and snapshot version 2.

- [ ] **Step 7: Commit Task 4**

```powershell
git add README.md docs/architecture.md docs/usage-policy.md docs/mvp-roadmap.md examples/trace.example.json tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_postgres_repository.py tests/test_store.py
git commit -m "docs: publish benchmark leakage workflow"
```

### Task 5: Full verification and whole-branch review

**Files:**
- Modify only if verification exposes a defect: files from Tasks 1-4

**Interfaces:**
- Consumes: complete feature branch.
- Produces: reviewed merge-ready behavior and measured compatibility evidence.

- [ ] **Step 1: Run focused tests**

```powershell
python -m pytest tests/test_policy.py tests/test_lifecycle.py tests/test_store.py tests/test_readme_api.py tests/test_examples_and_schema.py tests/test_postgres_repository.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full verification**

Run separately:

```powershell
python -m pytest -q --durations=20
python -m compileall -q src tests
git diff --check main...HEAD
git status --short
```

Expected: full suite passes with no warnings; compile/diff checks are clean;
tracked worktree is clean; no PostgreSQL process or cluster leaks remain.

- [ ] **Step 3: Review the complete branch**

Verify exact complete-pair classification, static precedence, incomplete-pair
behavior, source propagation in prepare/finalize, injection fail-closed
context, trace binding, usage audit evidence, prompt/hash secrecy, legacy
compatibility, snapshot/PostgreSQL version stability, schema scope, and docs.
Resolve every Critical, Important, and Minor finding and rerun covering tests.

- [ ] **Step 4: Merge and push**

Fetch `origin/main`. Fast-forward if it remains an ancestor; otherwise rebase
and resolve conflicts without dropping either upstream behavior or this
design. Run the full suite on merged `main`, push, verify remote SHA exactly,
then remove the owned worktree and branch.
