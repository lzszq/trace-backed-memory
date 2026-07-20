# Trace-backed Memory

A provenance-backed memory layer for LLM / agent harness engineering.

## One-liner

Trace-backed Memory turns provenance-bound agent traces, eval results, and git commits into verified, scoped, auditable memory that can be used selectively during debug, repair, regression analysis, planning, and production runtime.

[产品概览与当前能力](docs/product.md) | [Architecture](docs/architecture.md) | [Memory Usage Policy](docs/usage-policy.md) | [Roadmap](docs/mvp-roadmap.md)

## What this is

This project is not generic chatbot memory. It is a harness-oriented memory system:

```text
trace -> failure case -> verified lesson -> gated runtime memory
```

The system is designed around five rules:

1. Trace is the source of truth.
2. Memory is a curated projection derived from trace, eval, and git history.
3. Raw trace should not be injected into prompts by default.
4. Memory must pass both system gate and LLM applicability gate before use.
5. Every memory item must have source, scope, status, and usage logs.

## Core concepts

| Concept | Purpose | LLM-visible by default |
|---|---|---:|
| Trace | Immutable run provenance: prompts, tool calls, outputs, eval, commit | No |
| Failure Case | Structured postmortem of a failed run | Debug / repair only |
| Verified Lesson | Validated reusable rule derived from a case | Yes, if scoped and gated |
| Project Policy | Manually maintained prompt/tool/eval policy | Yes, if relevant |
| Memory Decision | Audit record of why memory was used or blocked | No |

## MVP architecture

```text
Git commit / PR / CI
        |
Harness run
        |
Trace store
        |
Eval result
        |
Failure detection
        |
Failure case draft
        |
Verification / regression
        |
Verified lesson
        |
Memory index
        |
System gate
        |
LLM applicability gate
        |
Runtime injection
        |
Memory usage log
```

## Install / Local Dev

The package uses a `src/` layout. From a checkout, install it in editable mode
before running the examples:

```powershell
python -m pip install -e .
```

Install a built wheel or source-distribution artifact with `pip`. The
distribution is marked as typed with `py.typed`. For one-off local commands from
a checkout, setting `PYTHONPATH=src` also works.

## Packaged Resources

Wheel, source-distribution, and editable installs contain byte-identical copies
of every file under `schemas/` and `examples/`, plus the canonical failure
taxonomy and active-lesson YAML example. Resource names come from a strict
allowlist of canonical POSIX paths; they never resolve arbitrary filesystem
input:

```text
tbm resource list
tbm resource read schemas/trace.schema.json
tbm resource export schemas/postgres.sql postgres.sql
tbm resource export schemas/postgres.sql postgres.sql --overwrite
```

All three commands emit one deterministic JSON value. Export refuses an
existing destination unless `--overwrite` is explicit and uses a
same-directory temporary file before replacement. Unknown names are input
errors; installed package-data failures are internal errors; export failures
use the existing write exit code 4.

Python callers use `packaged_resources()`, `read_packaged_resource()`, and
`export_packaged_resource()` to discover metadata, read exact bytes, or export
a resource without assuming that the package lives on a filesystem:

```python
from trace_backed_memory import (
    export_packaged_resource,
    packaged_resources,
    read_packaged_resource,
)

resources = packaged_resources()
postgres_sql = read_packaged_resource("schemas/postgres.sql")
export_packaged_resource("schemas/postgres.sql", "postgres.sql")
```

`PackagedResource` descriptions include kind, media type, byte size, and
SHA-256. `load_failure_taxonomy()` uses the packaged canonical taxonomy by
default; passing a path continues to load a caller-owned taxonomy file.

## Evidence Ingestion Integrity

Failure extraction treats `Trace.error`, then top-level `name`/`error` fields
from `tool_calls`, followed by explicit top-level `error` fields from
`tool_outputs`, as ordered structured evidence. An errored output's `name` may
label its symptom, but a successful output name, arbitrary output fields, and
nested result text are never searched for keywords. Ordinary tool content
therefore cannot create a false classification or tool-failure symptom.

The dependency-free failure-taxonomy and active-lessons YAML adapters reject
duplicate supported fields instead of silently applying last-key-wins
replacement. Duplicate lesson record or scope keys fail while the complete
document is still being parsed. Every resulting lesson is then constructed and
validated against staged state before one all-or-nothing Store commit, so a
duplicate ID or later semantic failure cannot partially import earlier records.
This hardening changes no valid YAML shape, snapshot version 2, JSON Schema, or
PostgreSQL schema version 1.

## Snapshot Operations CLI

Installing the package exposes the dependency-free `tbm` console script. The
same command surface is available through `python -m trace_backed_memory`:

```text
tbm snapshot validate SNAPSHOT
tbm snapshot stats SNAPSHOT
tbm audit SNAPSHOT
tbm metrics SNAPSHOT
tbm remediation SNAPSHOT
tbm recover-ready SNAPSHOT [--write]
tbm recover SNAPSHOT DECISION_ID [--memory-caused-failure true|false] [--write]
tbm recover-batch SNAPSHOT DECISION_ID... [--attribution DECISION_ID=true|false]... [--write]
```

Each command loads one local snapshot through the regular store validation
path. Read commands emit one deterministic JSON value plus a newline.
Recovery commands return the serialized completions, ordered decision IDs, and
a `written` flag. They are dry-run by default: the input bytes change only when
`--write` is explicit and the complete recovery succeeds. A write reuses the
store's same-directory temporary file and atomic replacement behavior.

Failures emit one structured JSON error to stderr without a traceback. Exit
codes are `0` for success or a no-op, `1` for an unexpected internal failure,
`2` for usage/path/encoding/JSON/snapshot input, `3` for a rejected recovery
state or attribution, and `4` for a snapshot write failure. Help remains normal
human-readable argparse output. Error text is capped at 2,048 characters, and
successful JSON is serialized before persistence. If a downstream pipe closes
stdout after `--write` commits, the already-persisted operation remains a
success rather than inviting an unsafe retry.

This interface accepts neither stdin nor remote URLs, PostgreSQL connections,
or alternate output paths. It adds no persisted CLI state: snapshot version 2,
active-lessons YAML, JSON Schemas, and PostgreSQL schema version 1 remain
unchanged.

## PostgreSQL Repository

PostgreSQL support is optional. Core installs do not import or require
`psycopg`; install the extra when using the synchronous repository:

```powershell
python -m pip install -e ".[postgres]"
pip install 'trace-backed-memory[postgres]'
```

The adapter requires PostgreSQL 12+ because `schemas/postgres.sql` uses
`jsonb_path_exists` in its hardened JSONB constraints.

Before connecting, install the PostgreSQL resource into a fresh `public`
schema. From a checkout, use `schemas/postgres.sql` directly. From any package
installation, export the byte-identical resource first:

```powershell
tbm resource export schemas/postgres.sql postgres.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f postgres.sql
```

The adapter requires the schema metadata row at `schema_version` 1. The SQL
file is a fresh-install schema, not a migration for an existing database.

```python
from trace_backed_memory import PostgresMemoryRepository

with PostgresMemoryRepository.connect("postgresql://...") as repository:
    result = repository.sync(store)
    restored = repository.load()
```

`connect()` creates an owned connection, and the context manager closes it. Pass
an existing connection to `PostgresMemoryRepository(connection)` to borrow it;
closing that repository leaves the caller's connection open.

When the supplied connection already has an active caller transaction, each
repository operation uses a nested savepoint and does not commit or roll back
the outer transaction; the caller owns the final commit or rollback. Without an
outer transaction, the repository transaction commits normally.

`sync(store)` is additive and transactional: it inserts records that are absent
and never deletes database records. It preserves supported forward lifecycle
updates, including completing a pending Trace and sealing a previously
unevaluated decision outcome, compares records in canonical form, and rejects
immutable ID conflicts. Any conflict rolls back the whole synchronization.
`repository.load()` returns a normalized, validated
`TraceBackedMemoryStore`, not a snapshot object.

## Callback-based Memory Run Execution

Use `run_memory_execution()` for the common synchronous path after registering
the current Trace with `eval_result="unknown"`. Its `MemoryDecisionCallback`
receives a `MemoryGateRequest`; its `MemoryExecutionCallback` receives the
final `GatedMemoryResult` and returns a `MemoryRunMeasurement` without copying
a decision ID:

```python
from trace_backed_memory import MemoryRunMeasurement, run_memory_execution


def decide(request):
    return llm_call(request.prompt)


def execute(gated):
    outcome = harness_run(memory_snippet=gated.snippet)
    return MemoryRunMeasurement(
        eval_result=outcome.eval_result,
        memory_caused_failure=outcome.memory_caused_failure,
        output_hash=outcome.output_hash,
        tool_outputs=(
            tuple(outcome.tool_outputs)
            if outcome.tool_outputs is not None
            else None
        ),
        latency_ms=outcome.latency_ms,
        cost_usd=outcome.cost_usd,
        error=outcome.error,
        trace_uri=outcome.trace_uri,
    )


completion = run_memory_execution(
    store,
    context=context,
    trace_id=current_trace.trace_id,
    task="repair failed search_docs call",
    decide=decide,
    execute=execute,
)
```

The module fixes the order as prepare, decide, finalize, execute, and atomic
complete. It always uses the Store-produced `decision_id` and does not infer an
evaluator result, failure attribution, or execution evidence from an exception.
Preparation errors propagate unchanged because no request has been created.

After preparation, `MemoryRunExecutionError` retains the original callback or
Store exception as its cause and identifies the `decision`, `finalization`,
`execution`, or `completion` phase. Every such error exposes the pending
request; execution and completion failures also expose the finalized result
and decision ID so the caller can retry against the same state or complete
explicitly. Do not rerun the whole one-shot helper as a retry because each call
prepares a new request. `KeyboardInterrupt` and `SystemExit` still pass through.
Use the Store's existing `prepare_memory()`,
`finalize_memory()`, and `complete_memory_run()` directly when advanced callers
need to pause between stages or own custom retry and recovery policy.

Measurements and execution errors are ephemeral. Only the existing Trace and
usage log are persisted, so snapshot version 2, JSON Schemas, active-lessons
YAML, and PostgreSQL schema version 1 remain unchanged.

## Safe Store Workflow

Use the store's two-phase workflow for runtime memory. `prepare_memory()`
retrieves candidates, applies System Gate, and creates the bounded LLM gate
prompt. After the LLM returns a decision payload, `finalize_memory()` rechecks
state, renders the allowed snippet, and records one trace-linked audit event.
The linked current Trace must already exist with `eval_result="unknown"`.

```python
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    query="search_docs null query",
)
result = store.finalize_memory(
    request,
    {
        "use_memory": True,
        "allowed_memory_ids": ["lesson_001"],
        "blocked_memory_ids": [],
        "reason": "The lesson directly matches the current tool failure.",
        "risk": "low",
        "recommended_injection": "short_summary",
    },
    trace_id=trace.trace_id,
)
snippet = result.snippet
# Execute the task with snippet, then evaluate it.
completion = store.complete_memory_run(
    trace_id=trace.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
)
completed_trace = completion.trace
sealed_log = completion.usage_log
(audit,) = store.memory_run_audits()
assert audit.status == "complete"
run_metrics = store.memory_run_metrics()
assert run_metrics.complete_count == 1
assert run_metrics.recoverable_count == 0
```

Only this store workflow provides ownership, replay, stale-state, trace-link,
and atomic logging guarantees.

### Atomic memory-run completion

After execution, `complete_memory_run()` is the preferred way to record the
result. It requires the exact linked `trace_id` and `decision_id`, applies one
measured `eval_result` to both records, validates both candidates under one
store lock, and returns a frozen `MemoryRunCompletion` containing defensive
copies of the completed Trace and sealed usage log.

Both records may be pending, either record may already contain the same result
for partial recovery, or both may already match for exact replay. A conflicting
result, failure attribution, Trace field, or linkage rejects the operation
without changing either record. `complete_trace()` and
`record_decision_outcome()` remain public low-level operations for separately
owned lifecycles and explicit recovery, but normal memory runs should use the
atomic high-level method.

Snapshots persist the existing Trace and usage-log records rather than the
return wrapper. PostgreSQL synchronization updates both rows in one transaction
and rolls the Trace update back if the usage update conflicts. Snapshot version
2, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 1 remain
unchanged.

### Atomic batch memory-run completion

Use `complete_memory_runs()` when an evaluator finishes several new results
that must commit all-or-nothing:

```python
from trace_backed_memory import MemoryRunResult

completions = store.complete_memory_runs(
    (
        MemoryRunResult(
            decision_id="decision_000002",
            eval_result="pass",
            output_hash="sha256:output",
            tool_outputs=({"documents": 3},),
            latency_ms=125,
        ),
    )
)
```

`MeasuredEvalResult` contains only `pass`, `fail`, and `error`. Each frozen
`MemoryRunResult` carries one decision ID, its result and failure attribution,
plus optional Trace evidence. The API requires a non-empty tuple with unique
decision IDs, derives `trace_id` from each validated usage decision, and
preserves request order in defensive `MemoryRunCompletion` results.

For evidence fields, `None` means omitted and preserves an existing value.
`tool_outputs` uses an optional tuple at the request boundary and becomes a
list on the Trace; an explicit empty tuple requests an empty list. Results for
a shared Trace must agree. Evidence fields merge when disjoint or equal, while
an outcome, existing per-decision attribution, immutable-evidence, or same-field
evidence conflict rejects the whole batch before mutation.

`complete_memory_runs()` handles new pending, matching partial, and exact replay
states using the same candidate validators as `complete_memory_run()`. It also
shares its non-mutating staging engine with `recover_memory_runs()`, while the
recovery API remains limited to results already measured on one side.
`MemoryRunResult` is not persisted; snapshots and PostgreSQL store only existing
Trace and usage rows. Snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 1 remain unchanged.

### Memory-run audit view

`memory_run_audits()` returns an immutable tuple of frozen `MemoryRunAudit`
values, with one record for every usage decision sorted by `decision_id`. Each
record exposes its linked `trace_id`, `run_id`, raw Trace and decision results,
failure attribution, and one derived status:

| Trace result | Decision result | Status |
|---|---|---|
| unevaluated | unevaluated | `pending` |
| measured | unevaluated | `trace_only` |
| unevaluated | measured | `decision_only` |
| same measured result | same measured result | `complete` |
| different measured results | different measured results | `conflict` |

Use `trace_only` and `decision_only` to locate supported partial recovery for
`complete_memory_run()`. A `pending` run still needs an evaluator result. A
`conflict` exposes incompatible historical low-level writes for review; the
store will never auto-repair it or choose one side as authoritative. Traces
without a usage decision are not memory runs and are omitted, while multiple
decisions for one Trace remain separate records.

The view is derived and not persisted. Snapshot and PostgreSQL round trips
reproduce it from existing Trace and usage-log fields, so snapshot version 2,
JSON Schemas, active-lessons YAML, and PostgreSQL schema version 1 remain
unchanged.

### Memory-run remediation plan

`memory_run_remediations()` turns every audit row into a frozen
`MemoryRunRemediation` with a `MemoryRunRemediationAction`:

```python
remediations = store.memory_run_remediations()
automatic_ids = tuple(
    item.decision_id
    for item in remediations
    if item.action == "recover"
)
attribution_ids = tuple(
    item.decision_id
    for item in remediations
    if item.action == "recover_with_attribution"
)
```

The decision-sorted view maps `pending` to `measure`, a passing `trace_only`
or any `decision_only` record to `recover`, and a failed or errored
`trace_only` record to `recover_with_attribution`. It maps `conflict` to
`investigate` and `complete` to `none`. Shared Trace decisions remain separate
items.

Each item retains the raw audit values. `resolved_eval_result` and
`resolved_memory_caused_failure` contain only values established by current
records. Failed or errored Trace-only records therefore expose the Trace result
but leave resolved attribution as `None`; the default false value on the
unevaluated decision is not treated as causal evidence.

This is an advisory current-state plan. Compatible `recover` items can be sent
to `recover_memory_runs()`, and `measure` items can be evaluated before
`complete_memory_runs()`. Per-decision actions do not promise that different
resolved results for one shared Trace are batch-compatible; both write APIs
revalidate shared-Trace and stale state. Never automatically process
`investigate`, and supply an explicit boolean for every
`recover_with_attribution` item. The plan is derived and not persisted;
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 1 remain unchanged.

### Atomic ready memory-run recovery

Use `recover_ready_memory_runs()` for a race-free maintenance sweep:

```python
completions = store.recover_ready_memory_runs()
```

The method accepts no arguments. Under one reentrant lock in the store it
derives the current remediation plan, selects only `action == "recover"`, and
delegates the decision-ID-sorted tuple to `recover_memory_runs()` without releasing the lock.
It returns defensive `MemoryRunCompletion` values in that order. No ready work
returns an empty tuple, so a repeated successful scan is idempotent and does
not replay complete records.

The sweep automatically handles passing `trace_only` and all `decision_only`
records. It skips `measure`, `recover_with_attribution`, `investigate`, and
`none`: failed or errored Trace-only records still require explicit attribution
through the existing recovery APIs, while pending and conflicting records are
never guessed through.

Ready decisions sharing one Trace must resolve to the same outcome. A shared
result disagreement or any later candidate failure rejects the whole selected
set before mutation. Concurrent sweeps serialize; after one commits, another
re-plans against the completed state. Selection is not persisted, and only
existing Trace and usage rows change. Snapshot version 2, JSON Schemas,
active-lessons YAML, and PostgreSQL schema version 1 remain unchanged.

### Memory-run health metrics

`memory_run_metrics()` returns one frozen `MemoryRunMetrics` point-in-time
summary of the audit view. It counts one usage decision per row, including
separate decisions linked to the same Trace, and exposes `decision_count`,
`pending_count`, `trace_only_count`, `decision_only_count`, `complete_count`,
`conflict_count`, `recoverable_count`, `auto_recoverable_count`, and
`attribution_required_count`.

The five status counts are mutually exclusive and their sum always equals
`decision_count`. `recoverable_count` is the sum of `trace_only_count` and
`decision_only_count`. It also equals `auto_recoverable_count` plus
`attribution_required_count`: the former counts passing Trace-only and all
decision-only records, while the latter counts failed or errored Trace-only
records whose causal attribution is unresolved. Pending runs still need a
measured result and conflicts need manual review.

The summary is derived and not persisted. Empty stores return zero for every
field, and snapshot and PostgreSQL loads reconstruct the same value from the
underlying records. Snapshot version 2, JSON Schemas, active-lessons YAML, and
PostgreSQL schema version 1 remain unchanged.

### Safe memory-run recovery

Use `recover_memory_run()` with an audited `decision_id` when a low-level write
or interrupted process left one side complete:

```python
recoverable = next(
    audit
    for audit in store.memory_run_audits()
    if audit.status == "decision_only"
)
completion = store.recover_memory_run(recoverable.decision_id)
```

The method does not accept `trace_id` or `eval_result`; it derives both from the
validated linked records and returns `MemoryRunCompletion`. For `trace_only`,
the Trace result is authoritative only for the missing decision. For
`decision_only`, the sealed decision result and `memory_caused_failure` are
preserved while the Trace is completed. A `complete` record is an idempotent
exact replay.

Recovery rejects `pending` because no measured result exists and rejects
`conflict` because the two measured results disagree. It never guesses through
either state. A passed `trace_only` run safely implies no memory-caused failure;
a failed or errored `trace_only` run requires the caller to explicitly provide
`memory_caused_failure=True` or `False` because causal attribution is missing.

Optional output hash, tool outputs, latency, cost, error, and trace URI evidence
use the same immutable-slot rules as `complete_memory_run()`. Recovery delegates
to that atomic operation under the same store lock, so every rejection leaves
both records unchanged. It changes only existing persisted fields: snapshot
version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema version 1
remain unchanged.

### Atomic batch memory-run recovery

Use `recover_memory_runs()` when one worker must repair several audited runs as
one all-or-nothing operation:

```python
recoverable_ids = tuple(
    audit.decision_id
    for audit in store.memory_run_audits()
    if audit.status in {"trace_only", "decision_only"}
)
completions = store.recover_memory_runs(recoverable_ids)
```

The first argument must be a non-empty tuple of unique decision IDs. The method
preserves request order in its returned tuple of defensive
`MemoryRunCompletion` values. Every item is classified from state at method
entry: `trace_only`, `decision_only`, and `complete` are eligible, while one
`pending` or `conflict` item rejects the whole batch without mutation.

For each failed or errored `trace_only` item, pass an exact boolean in the
`memory_caused_failures` mapping. Passing `trace_only` defaults to false;
`decision_only` and `complete` preserve sealed attribution unless an equal
value is supplied. Decisions linked to a shared Trace must independently
derive the same result, and a pending decision cannot become eligible through
another item in the same batch.

Batch recovery does not accept `trace_id` or `eval_result`, and it also omits
Trace completion evidence parameters. Use `recover_memory_run()` for one item
when output hash, tool outputs, latency, cost, error, or Trace URI must be
attached. The batch result is not persisted; only existing Trace and usage rows
change. Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL
schema version 1 remain unchanged.

### Deferred Trace completion

Register the current Trace before memory finalization with all known identity,
input, provenance, retrieved-context, and tool-call evidence plus
an `eval_result` of `unknown`. After execution, `complete_trace()` requires
`pass`, `fail`, or `error` and can fill `output_hash`, `tool_outputs`,
`latency_ms`, `cost_usd`, `error`, and `trace_uri` once.

Omitted completion fields preserve their initial values. Existing non-empty
execution evidence may be repeated exactly but cannot be replaced. Every other
Trace field, including identity, repo/commit/tenant provenance, prompt/tool/model
metadata, input hash, retrieved context, tool calls, and `created_at`, remains
immutable. Exact replay is idempotent; a different completion fails without
mutation.

`complete_trace()` is the low-level Trace-only transition: it never updates a
usage log. Use it when the caller intentionally owns separate audit lifecycles
or needs partial recovery before calling `complete_memory_run()` with the same
measured result. PostgreSQL synchronization supports the same forward Trace
transition and rejects stale, reverse, or conflicting updates atomically.
Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 1 remain unchanged.

### Deferred decision outcome sealing

`finalize_memory()` may log an outcome immediately when one is already known.
`record_decision_outcome()` remains the low-level decision-only transition for
separately owned lifecycles and partial recovery; normal runtime callers use
`complete_memory_run()` after execution. The sealable measured results are
`pass`, `fail`, and `error`; initial `None` or `unknown` values are unevaluated.
A measured result and its `memory_caused_failure` value form one outcome pair.

An unevaluated decision can be sealed once. Exact replay of the same pair is
idempotent; a different result, a different failure attribution, a downgrade
to unevaluated, or an invalid wrong-memory claim is rejected without changing
the log. Metrics immediately move the decision out of the unevaluated bucket.

JSON snapshots already persist the outcome pair. PostgreSQL synchronization
allows only the same forward pair update while keeping every other usage-log
field, including `created_at`, immutable. A stale or conflicting sync fails and
rolls back atomically. Snapshot version 2, JSON Schemas, active-lessons YAML,
and PostgreSQL schema version 1 remain unchanged.

### Declared Trace provenance binding

At finalization and low-level logging, `repo`, `commit_sha`, and `tenant` always
match the linked Trace. `branch`, `prompt_version`, `prompt_family`,
`tool_schema_version`, `model`, and `eval_suite` bind only when the context
declares them. A declared tool must match an exact plain-string Trace tool call;
non-string tool names are ignored. Omitted optional provenance remains broad
and does not require Trace fields to be absent. `model_family`, `task_type`, and
`failure_type` remain unbound because Trace has no equivalent persisted fields.

Validation happens before pending request consumption or usage-log append, so
a mismatch cannot consume a reusable request or write partial evidence.
Imported version-2 and supplied legacy context evidence follows the same
declared-only rule. The feature reuses existing context and Trace fields;
snapshot version 2 and PostgreSQL schema version 1 remain unchanged.

### Benchmark example leakage classification

Benchmark example identity is the exact pair `(eval_suite, input_hash)`. To opt
in, the caller must choose a stable suite name, canonicalize one benchmark
example deterministically, compute a collision-resistant privacy-preserving
hash, and attach it to the trace for that example. Each trace carries the hash
of its own example, and the current `MemoryContext` must match the current
trace. Source and current traces use the same hash only when they represent the
same canonical example; different examples keep their own hashes. The library
compares strings exactly; hash algorithm, encoding, collision handling,
canonicalization stability, and suite-name stability remain caller
responsibilities.

The complete same-example workflow is executable:

```python
# BENCHMARK_SAFE_WORKFLOW_START
from trace_backed_memory import (
    MemoryContext,
    Trace,
    TraceBackedMemoryStore,
    draft_failure_case,
    lesson_from_failure_case,
    verify_failure_case,
)

BENCHMARK_BLOCK_REASON = "memory originates from current benchmark example"
source_input_hash = "sha256:79e820f10f2b4f322f84307a68a09f62f8342d5c824b86bd1b7f3f6fbebf01f9"
current_input_hash = source_input_hash

store = TraceBackedMemoryStore()
source_trace = store.record_trace(
    Trace(
        trace_id="trace_benchmark_source",
        run_id="run_benchmark_source",
        commit_sha="abc123",
        repo="agent-harness",
        tenant="tenant_a",
        eval_suite="tool_calling_regression",
        input_hash=source_input_hash,
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
    )
)
case = store.add_failure_case(
    verify_failure_case(
        draft_failure_case(
            source_trace,
            case_id="case_benchmark_source",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
        ),
        fix="require a non-empty query",
        fix_commit_sha="def456",
        regression_passed=True,
    )
)
lesson = store.add_lesson(
    lesson_from_failure_case(
        case,
        lesson_id="lesson_benchmark_source",
        lesson_text="Always pass a non-empty query to search_docs.",
        memory_type="procedural",
        scope={
            "repo": "agent-harness",
            "tenant": "tenant_a",
            "tool": "search_docs",
        },
    )
)
current_trace = store.record_trace(
    Trace(
        trace_id="trace_benchmark_current",
        run_id="run_benchmark_current",
        commit_sha="abc123",
        repo="agent-harness",
        tenant="tenant_a",
        eval_suite="tool_calling_regression",
        input_hash=current_input_hash,
        tool_calls=[{"name": "search_docs"}],
    )
)
context = MemoryContext(
    mode="production",
    repo="agent-harness",
    tenant="tenant_a",
    commit_sha="abc123",
    tool="search_docs",
    eval_suite="tool_calling_regression",
    input_hash=current_input_hash,
)

request = store.prepare_memory(context, task="answer current benchmark example")
assert request.candidate_memory_ids == (lesson.lesson_id,)
assert request.system_allowed_memory_ids == ()
assert dict(request.system_blocked) == {
    lesson.lesson_id: BENCHMARK_BLOCK_REASON
}
assert lesson.lesson_id not in request.prompt
assert source_input_hash not in request.prompt
assert current_input_hash not in request.prompt

result = store.finalize_memory(
    request,
    {
        "use_memory": False,
        "allowed_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "No eligible memory remains after the System Gate.",
        "risk": "none",
        "recommended_injection": "none",
    },
    trace_id=current_trace.trace_id,
)
usage = store.usage_logs[-1]
assert result.snippet == ""
assert usage.context["input_hash"] == current_input_hash
assert usage.system_blocked_reasons == {
    lesson.lesson_id: BENCHMARK_BLOCK_REASON
}
# BENCHMARK_SAFE_WORKFLOW_END
```

Derived lesson and failure-case candidates are enriched only at runtime with
ephemeral `source_eval_suite` and `source_input_hash`. They are checked against
the complete context pair before LLM narrowing. Candidate `source_eval_suite`
and `source_input_hash` fields are not serialized into prompts or snippets.
The builders do not render structured `input_hash` fields; `eval_suite` remains
ordinary prompt context and may also appear in memory scope. Exact equality
blocks in every mode with the automatic block reason shown above. Static
`sensitive` and `eval_leaking` checks retain precedence and their existing
reasons.

Incomplete identities never trigger a guessed match. `eval_suite` without
`input_hash` remains valid, context `input_hash` without `eval_suite` is
invalid, and an incomplete source trace contributes neither ephemeral field.
A directly constructed partial source pair is a memory contract error.
Different hashes in one suite and equal hashes in different suites do not match.

Finalization enforces context/trace binding for both identity values before it
consumes the request. Usage evidence keeps the current pair, candidate/status
evidence, and automatic block reason. `input_hash` is identity evidence, not
memory scope, so retrieval and lesson/policy scope fields remain unchanged.
Compatibility remains snapshot version 2 and PostgreSQL schema version 1 with
no new persisted memory fields; traces use the existing `input_hash` field,
usage evidence uses the existing context JSON/JSONB, and source identity is
reconstructed rather than stored.

### Semantic retrieval

```python
# Scores are computed by the caller's embedding index or reranker.
semantic_scores = {lesson.lesson_id: 0.93}

request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    semantic_scores=semantic_scores,
    max_candidates=10,
    minimum_score=0.70,
)
```

Metadata scope is applied before ranking. Scores may use any finite numeric
scale, but callers must normalize them so larger values mean greater relevance.
Keyword `query` and `semantic_scores` cannot be combined in one call.
`max_candidates` is required and must be an integer from 1 through 50 inclusive.
System Gate and LLM Gate remain
authoritative, and scores are not persisted in snapshots or PostgreSQL.

### Git ancestry applicability

```python
from trace_backed_memory import capture_commit_ancestry

anchors = store.candidate_commit_anchors(context)
commit_ancestry = capture_commit_ancestry(
    context.commit_sha,
    anchors,
    repo_path=".",
)
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    commit_ancestry=commit_ancestry,
)
```

Discover anchors while reading the store, then capture Git evidence outside the
store lock before calling `prepare_memory()`. The immutable evidence is bound
to the exact `context.commit_sha`: a lesson anchors to its source case's
`fix_commit_sha`, and a failure-case memory anchors to its source
`commit_sha`. Project policies have no commit anchor, so ancestry bypasses
only that filter for them; their normal metadata scope, System Gate, and LLM
Gate checks remain unchanged.

`capture_commit_ancestry()` runs `git merge-base --is-ancestor` for every
anchor. Exit 0 records `True`, exit 1 records `False` and excludes the
anchored history, and any other command error stops the workflow. When callers
provide evidence, it must contain a relation for every discovered anchor;
missing relations fail closed rather than leaving history unfiltered. Omitting
`commit_ancestry` preserves the pre-ancestry retrieval behavior.

Evidence is request-time input only: it is neither stored in snapshots nor
persisted to PostgreSQL, and it does not replace either gate. PR callers use
`pr_report_commit_anchors(context)`, capture against the same context commit,
and pass that same evidence object to `pr_memory_report()`.

## Endpoint-aware PR reports

Use an immutable `PRChangeSet` when a PR changes trace-backed metadata values.
Each tuple is `(field_name, old_value, new_value)` and supports only
`prompt_version`, `prompt_family`, `tool`, `tool_schema_version`, `model`, and
`eval_suite`. `new_value` must exactly equal the post-change `MemoryContext`
value, including `None`. Repo and tenant remain hard exact-match isolation
boundaries, and unchanged declared context metadata continues to match exactly.

The store matches every changed field against a complete old endpoint and a
complete new endpoint. It excludes mixed configurations. Report provenance
records `old`, `new`, or `both`; `both` is possible for a tool-only change when
one trace invoked both endpoint tool names. Reuse the same immutable change set
for `pr_report_commit_anchors()` and `pr_memory_report()` so ancestry evidence
is captured for exactly the cases the report can include.

The existing `changed_fields=[...]` input remains supported with its broad,
field-name-only behavior and legacy provenance of `None`, including its
existing `model_family` warning behavior. Exact value-aware `model_family`
matching is unsupported because traces do not store that provenance.
`PRChangeSet` values and endpoint provenance are ephemeral report-only values:
they are not persisted and do not change snapshot version 2, JSON schemas,
active-lessons YAML, or PostgreSQL schema version 1.

```python
# PR_CHANGE_SET_WORKFLOW_START
from trace_backed_memory import (
    MemoryContext,
    PRChangeSet,
    Trace,
    TraceBackedMemoryStore,
    capture_commit_ancestry,
    draft_failure_case,
    verify_failure_case,
)

store = TraceBackedMemoryStore()

def add_case(case_id, commit_sha, prompt_version, tool_schema_version):
    trace = store.record_trace(
        Trace(
            trace_id=f"trace-{case_id}",
            run_id=f"run-{case_id}",
            commit_sha=commit_sha,
            repo="agent-harness",
            tenant="tenant_a",
            prompt_version=prompt_version,
            tool_schema_version=tool_schema_version,
            eval_result="fail",
            tool_calls=[{"name": "search_docs"}],
        )
    )
    store.add_failure_case(
        verify_failure_case(
            draft_failure_case(
                trace,
                case_id=case_id,
                failure_type="invalid_tool_argument",
                symptom="search_docs rejected an empty query",
            ),
            fix="require a non-empty query",
            fix_commit_sha=f"fix-{case_id}",
            regression_passed=True,
        )
    )


add_case("case-old", "commit-old", "planner-v1", "search-docs-v1")
add_case("case-new", "commit-new", "planner-v2", "search-docs-v2")
add_case("case-mixed", "commit-mixed", "planner-v1", "search-docs-v2")

context = MemoryContext(
    mode="regression",
    repo="agent-harness",
    tenant="tenant_a",
    commit_sha="pr-head",
    prompt_version="planner-v2",
    tool="search_docs",
    tool_schema_version="search-docs-v2",
    failure_type="invalid_tool_argument",
)
change_set = PRChangeSet(
    (
        ("prompt_version", "planner-v1", "planner-v2"),
        ("tool_schema_version", "search-docs-v1", "search-docs-v2"),
    )
)
anchors = store.pr_report_commit_anchors(context, change_set=change_set)
commit_ancestry = capture_commit_ancestry(
    context.commit_sha,
    anchors,
    repo_path=".",
    runner=lambda _args, _cwd=None: 0,
)
report = store.pr_memory_report(
    context,
    change_set=change_set,
    commit_ancestry=commit_ancestry,
)
# report: case-new/new and case-old/old; case-mixed is excluded.
# PR_CHANGE_SET_WORKFLOW_END
```

## Low-level System Gate Helper

```python
from trace_backed_memory import MemoryContext, MemoryItem, system_gate

context = MemoryContext(
    mode="repair",
    repo="agent-harness",
    tenant="tenant_a",
    branch="main",
    commit_sha="abc123",
    prompt_family="planner",
    tool="search_docs",
    tool_schema_version="search_docs_v2",
    eval_suite="tool_calling_regression",
    failure_type="invalid_tool_argument",
)

candidates = [
    MemoryItem(
        memory_id="lesson_001",
        status="active",
        memory_type="procedural",
        scope={"tenant": "tenant_a", "tool": "search_docs", "prompt_family": "planner"},
        text="When calling search_docs, always provide a non-empty natural-language query.",
        source_case_id="case_001",
    )
]

allowed, blocked = system_gate(context, candidates)
```

## Implemented MVP API

The package now implements the README pipeline as dependency-free Python objects and helpers:

```python
from dataclasses import replace

from trace_backed_memory import (
    MemoryContext,
    MemoryDecision,
    MemoryRunRemediation,
    MemoryRunResult,
    ProjectPolicy,
    Trace,
    TraceMetadataCaptureError,
    TraceBackedMemoryStore,
    apply_llm_gate_decision,
    build_injection_snippet,
    build_llm_gate_prompt,
    capture_trace_metadata,
    classify_failure_type,
    draft_failure_case,
    draft_failure_case_from_trace,
    export_packaged_resource,
    lesson_from_failure_case,
    load_failure_taxonomy,
    memory_item_from_failure_case,
    memory_item_from_lesson,
    memory_item_from_project_policy,
    obsolete_failure_case,
    obsolete_lesson,
    packaged_resources,
    parse_memory_context,
    parse_memory_decision,
    review_failure_case,
    system_gate,
    verify_failure_case,
)

store = TraceBackedMemoryStore()
try:
    metadata = capture_trace_metadata(repo_path=".")
except TraceMetadataCaptureError as exc:
    raise RuntimeError(f"cannot capture git metadata for memory trace: {exc}") from exc
taxonomy = load_failure_taxonomy()

trace = store.record_trace(
    Trace(
        trace_id="trace_001",
        run_id="run_001",
        commit_sha=metadata.commit_sha,
        repo=metadata.repo,
        tenant="tenant_a",
        branch=metadata.branch,
        dirty=metadata.dirty,
        prompt_family="planner",
        eval_suite="tool_calling_regression",
        eval_result="fail",
        tool_calls=[{"name": "search_docs", "arguments": {"query": None}}],
        error="Invalid argument: query is required",
    )
)

failure_type = classify_failure_type(trace, taxonomy=taxonomy)
case = draft_failure_case_from_trace(
    trace,
    case_id="case_001",
    taxonomy=taxonomy,
)
assert case.failure_type == failure_type
reviewed = review_failure_case(
    case,
    reviewed_by="jason",
    root_cause="planner prompt omitted the search_docs query contract",
    review_notes="Confirmed by inspecting failed tool call arguments.",
)
verified = verify_failure_case(
    reviewed,
    fix="added schema example",
    fix_commit_sha="def456",
    regression_passed=True,
)
store.add_failure_case(verified)

lesson = lesson_from_failure_case(
    verified,
    lesson_id="lesson_001",
    lesson_text="When calling search_docs, always provide a non-empty query.",
    memory_type="procedural",
    scope={"repo": metadata.repo, "tenant": "tenant_a", "tool": "search_docs"},
)
store.add_lesson(lesson)

current_trace = store.record_trace(
    replace(
        trace,
        trace_id="trace_002",
        run_id="run_002",
        eval_result="unknown",
        tool_calls=[
            {
                "name": "search_docs",
                "arguments": {"query": "trace-backed memory"},
            }
        ],
        error=None,
    )
)

context = parse_memory_context(
    {
        "mode": "repair",
        "repo": metadata.repo,
        "tenant": "tenant_a",
        "commit_sha": metadata.commit_sha,
        "tool": "search_docs",
        "failure_type": failure_type,
        "eval_suite": "tool_calling_regression",
    }
)
request = store.prepare_memory(
    context,
    task="repair failed search_docs call",
    query="search_docs null query",
)
result = store.finalize_memory(
    request,
    {
        "use_memory": True,
        "allowed_memory_ids": ["lesson_001"],
        "blocked_memory_ids": [],
        "reason": "The lesson directly matches the current tool failure.",
        "risk": "low",
        "recommended_injection": "short_summary",
    },
    trace_id=current_trace.trace_id,
)
snippet = result.snippet
completion = store.complete_memory_run(
    trace_id=current_trace.trace_id,
    decision_id=result.decision_id,
    eval_result="pass",
    output_hash="sha256:current-output",
    tool_outputs=[{"documents": 3}],
    latency_ms=125,
    cost_usd=0.002,
)
completed_trace = completion.trace
outcome_log = completion.usage_log
metrics = store.metrics()
assert metrics.evaluated_with_memory_count == 1
assert metrics.evaluated_without_memory_count == 0
assert metrics.unevaluated_decision_count == 0
memory_metrics = {
    item.memory_id: item for item in store.memory_outcome_metrics()
}
assert memory_metrics["lesson_001"].observed_pass_rate == 1.0
assert memory_metrics["case_001"].candidate_count == 0

snapshot = store.to_snapshot()
restored = TraceBackedMemoryStore.from_snapshot(snapshot)
store.save_json("memory-store.snapshot.json")
restored_from_disk = TraceBackedMemoryStore.load_json("memory-store.snapshot.json")
store.save_lessons_yaml("lessons.active.yaml")
lesson_only_store = TraceBackedMemoryStore()
lesson_only_store.record_trace(trace)
lesson_only_store.add_failure_case(verified)
lesson_only_store.load_lessons_yaml("lessons.active.yaml")

pr_report = store.pr_memory_report(context, changed_fields=["tool_schema_version", "eval_suite"])
```

## Outcome-aware metrics

`pass`, `fail`, and `error` are evaluated outcomes; `error` is an evaluated
non-pass. `unknown` and `None` are unevaluated and are excluded from pass-rate
denominators. `evaluated_with_memory_count` and
`evaluated_without_memory_count` expose the two denominators, while
`unevaluated_decision_count` identifies decisions that still lack a usable
outcome. For values returned by `store.metrics()`, their sum equals
`decision_count`; legacy positional construction leaves the appended counts at
their compatible zero defaults.

When evaluation finishes after memory finalization, use
`complete_memory_run()` to complete the Trace and seal the decision before
reading metrics or persisting the completed audit. Use
`record_decision_outcome()` only when the caller deliberately owns the Trace
transition separately. Both transitions update these derived metrics without
persisting separate counters.

These are decision counts, not per-memory causal attribution. A decision is
classified as with-memory when its usage log has non-empty `used_memory_ids`.
Metrics remain derived and are not persisted; snapshot version 2, active-lessons
YAML, JSON Schemas, and PostgreSQL schema version 1 are unchanged.

### Per-memory observations

`memory_outcome_metrics()` returns a memory-ID-sorted tuple for every stored
failure case, lesson, and project policy, including zero-observation records.
`candidate_count`, `used_count`, and `blocked_count` expose retrieval and final
decision frequency; blocked count covers both deterministic and LLM-narrowing
blocks. For actual uses, `evaluated_use_count`, `passed_use_count`,
`failed_or_errored_use_count`, `unevaluated_use_count`, and
`observed_pass_rate` apply the same measured-outcome boundary as global
metrics.

These are observed associations, not causal effectiveness estimates. A run
that uses multiple memory IDs contributes its outcome to every used ID. The API
does not derive per-memory wrong-memory attribution from the decision-level
`memory_caused_failure` flag. Metrics remain derived and are not persisted;
snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
version 1 stay unchanged.

Low-level helpers remain public for callers that own equivalent orchestration,
but only the store workflow provides ownership, replay, stale-state,
trace-link, and atomic logging guarantees:

```python
manual_case = draft_failure_case(
    trace,
    case_id="case_manual",
    failure_type="invalid_tool_argument",
    symptom="planner called search_docs with null query",
)
lesson_memory = memory_item_from_lesson(lesson)
case_memory = memory_item_from_failure_case(verified, trace)
policy_memory = memory_item_from_project_policy(
    ProjectPolicy(
        policy_id="project_policy_001",
        policy_text="Planner responses must include a tool-call rationale.",
        scope={"prompt_family": "planner"},
    )
)
gate_prompt = build_llm_gate_prompt(context, [lesson_memory], task="repair failed tool call")
old_case = obsolete_failure_case(verified)
old_lesson = obsolete_lesson(lesson)
```

Implemented pieces:

- Core models: `Trace`, `FailureCase`, `Lesson`, `ProjectPolicy`,
  `MemoryUsageLog`, `MemoryRunResult`, `MemoryRunCompletion`, `MemoryRunAudit`,
  `MemoryRunRemediation`, `MemoryRunMetrics`, `MemoryMetrics`, and
  `MemoryOutcomeMetrics`.
- Git metadata capture for repo name, commit SHA, branch, and dirty state, with command failure errors wrapped for harness diagnostics.
- Git ancestry capture produces immutable, current-commit-bound relations for caller-discovered local commit anchors.
- Trace provenance fields for repo, prompt version, prompt family, tool schema version, model, and eval suite.
- Store-level checks that validate both the incoming and copied trace, preserve copy isolation, reject concurrent copy mutation, and reject empty identity fields, unsupported eval results, or malformed nested JSON trace collections, including non-string object keys, non-finite numbers, reference cycles, and excessive nesting.
- Atomic deferred Trace completion for measured output identity, tool outputs, latency, cost, error, and trace URI evidence, with immutable provenance and input fields, exact replay, copy isolation, and PostgreSQL forward synchronization.
- Atomic memory-run completion by linked `trace_id` and `decision_id`, with one measured result, exact replay, partial recovery, defensive return values, and transactional PostgreSQL synchronization.
- Atomic `complete_memory_runs()` evaluation batches with derived Trace linkage,
  per-run evidence, shared-Trace merging, and all-or-nothing assignment.
- Derived `memory_run_audits()` visibility for pending, one-sided, complete, and conflicting Trace/decision outcomes without new persistence state.
- Derived `memory_run_remediations()` actions with safe resolved recovery
  values, explicit attribution work, stale-state revalidation, and no persisted
  plan.
- Atomic `recover_ready_memory_runs()` sweeps that select and commit current
  automatic recoveries under one lock while skipping unresolved work.
- Dependency-free `run_memory_execution()` orchestration with typed decision
  and execution callbacks, Store-produced linkage, explicit measurement, and
  recoverable post-preparation error context.
- Derived `memory_run_metrics()` health counts with five-state conservation,
  automatic-versus-attributed recovery work, and no redundant persisted
  aggregate.
- Safe `recover_memory_run()` orchestration that derives correlated IDs/results, requires explicit failed-run attribution, and reuses atomic completion.
- Atomic `recover_memory_runs()` orchestration that validates and stages a unique decision tuple before committing any shared-Trace recovery.
- Lifecycle helpers: failed trace -> validated draft failure case -> verified case -> validated active lesson -> `MemoryItem`.
- Failure extraction helpers that load the failure taxonomy, classify failed
  traces from ordered Trace, tool-call, and top-level `tool_outputs` error
  evidence, and draft failure cases without scanning arbitrary output content.
- Manual review helper that records reviewer, root cause, notes, and review timestamp on draft failure cases.
- Verification loop hardening: only draft cases can be verified, and verified cases require a fix commit and passing regression evidence.
- Obsolete transitions for failure cases and lessons.
- Store-level checks that reject failure cases with empty identity fields, invalid status, missing verified evidence, missing source trace, or source commit mismatch.
- Project policy helper that turns manually maintained prompt/tool/eval policy into sourced `MemoryItem` policy memory.
- Deterministic System Gate with strict source, tenant-aware scope, status, memory-type, confidence, sensitivity, eval-leak, and mode checks.
- Gate boundary helpers that validate runtime context JSON and direct-call container/record types before use, require non-empty string tasks and string-or-`None` queries, JSON-quote and cap dynamic gate prompt fields, validate LLM decision JSON with non-empty unique IDs and consistent `use_memory` / `recommended_injection` fields, reject contradictory System Gate allowed/blocked inputs, require the final `MemoryDecision` before rendering non-empty runtime snippets, honor `none`/`pointer_only`/`short_summary` injection modes, and prevent the LLM decision from overriding System Gate.
- In-memory MVP store for trace/case/lesson/project-policy records, metadata-first candidate retrieval that requires all declared scope fields to match, optional opt-in Git ancestry filtering before keyword or semantic ranking, debug/repair visibility for verified regression-backed failure cases, optional keyword filtering including short domain tokens, optional bounded caller-provided semantic scores ranked score-descending with memory-ID-ascending ties, and usage decision logs; retrieval cannot bypass System Gate or LLM Gate.
- Usage-log validation and persisted contract that require trace ID, serialized context, candidate status snapshots, and System Gate block reasons; reject empty identities, duplicate imported decision IDs, invalid mode/risk/injection fields, duplicate, empty-string, or non-string memory ID lists, unsupported eval results, unknown runtime memory IDs, and used or blocked memory IDs outside the candidate set.
- Dependency-free strict JSON snapshot save/load for trace, failure case, lesson, project policy, and usage-log records; non-object snapshots, non-finite floats, over-limit integers, and non-standard JSON numeric constants are rejected while JSON-serializable integer costs remain valid.
- Dependency-free active lesson YAML save/load for the repository's simple
  `memory/lessons.example.yaml` shape, preserving numeric-looking scope strings
  and rejecting duplicate or semantically invalid documents through
  all-or-nothing Store mutation.
- Zip-safe packaged resource discovery, exact-byte reads, SHA-256 metadata, and
  explicit atomic export for all 18 canonical Schemas, examples, and memory
  support files in wheel, source-distribution, and editable installs.
- Store-level checks that reject lessons with empty identity fields, invalid memory type/status, unknown non-empty scope fields, unbounded confidence, or a missing, unverified, non-regression-backed source case.
- Store-level checks that reject project policies with empty identity/text fields, invalid status, invalid scope, unbounded confidence, or IDs that collide with failure case, lesson, or project policy memory IDs.
- JSON schemas for stored records and full memory-store snapshots.
- Postgres schema parity checks for model defaults, an atomic fresh-install transaction pinned to `public`, invariant functions pinned to `pg_catalog`, a trigger-owned shared runtime memory ID registry that rejects direct DML, `TRUNCATE`, helper-shadow bypasses, and ghost usage, non-empty required text, composite case/trace commit provenance, forward-only status updates, `FOR SHARE` parent/lesson lifecycle serialization and cascades, JSONB object/array and element-type checks, required usage-decision audit evidence, and context example parsing.
- Lesson safety flags for sensitive or eval-leaking memory are preserved through retrieval and blocked by System Gate.
- PR reports can reuse current-commit-bound ancestry evidence to exclude unrelated historical failure cases before generating report content.
- PR/CI helper that reports related verified, regression-backed historical failures from repo-matched traces, includes source/fix provenance, suggests regressions, warns on risky prompt/tool/model/eval-suite changes, and supports immutable complete-endpoint `PRChangeSet` matching with old/new/both provenance.
- Deferred, idempotent decision-outcome sealing plus outcome-aware metrics for decisions, candidates, used/blocked memory, measured pass rates with explicit denominators, unevaluated decisions, wrong-memory failures, obsolete attempts, and lesson confidence.

## Repository layout

```text
.
|-- docs/
|   |-- product.md
|   |-- architecture.md
|   |-- usage-policy.md
|   `-- mvp-roadmap.md
|-- examples/
|   |-- trace.example.json
|   |-- failure_case.example.json
|   |-- lesson.example.json
|   |-- memory_context.example.json
|   |-- project_policy.example.json
|   |-- memory_usage_log.example.json
|   `-- memory_decision.example.json
|-- memory/
|   |-- lessons.example.yaml
|   `-- failure_taxonomy.yaml
|-- schemas/
|   |-- postgres.sql
|   |-- trace.schema.json
|   |-- failure_case.schema.json
|   |-- lesson.schema.json
|   |-- project_policy.schema.json
|   |-- memory_usage_log.schema.json
|   |-- memory_store_snapshot.schema.json
|   |-- memory_context.schema.json
|   `-- memory_decision.schema.json
|-- src/trace_backed_memory/
|   |-- _resources/
|   |-- __main__.py
|   |-- __init__.py
|   |-- capture.py
|   |-- cli.py
|   |-- execution.py
|   |-- extraction.py
|   |-- lifecycle.py
|   |-- models.py
|   |-- policy.py
|   |-- postgres.py
|   |-- py.typed
|   |-- resources.py
|   `-- store.py
`-- tests/
    |-- test_capture.py
    |-- test_cli.py
    |-- test_execution.py
    |-- test_examples_and_schema.py
    |-- test_extraction.py
    |-- test_lifecycle.py
    |-- test_packaging.py
    |-- test_postgres_integration.py
    |-- test_postgres_repository.py
    |-- test_policy.py
    |-- test_readme_api.py
    |-- test_resources.py
    |-- verify_distribution.py
    `-- test_store.py
```
