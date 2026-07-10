# Compatible Secure Store Boundary Design

## Goal

Make the safe trace-backed-memory workflow the default, auditable path without
removing the existing low-level policy helpers. Close the current integrity
gaps around gate ordering, source scope, persisted lifecycle transitions,
historical metrics, runtime type validation, and snapshot loading.

## Scope

This design delivers one cohesive store-boundary increment:

- a two-phase store-level memory gating workflow;
- validated store-backed failure-case and lesson lifecycle transitions;
- source-scope narrowing for lessons derived from traces;
- decision-time context, status, block-reason, trace, and timestamp evidence;
- historically stable metrics;
- strict, deterministic, versioned snapshots with legacy migration;
- fail-closed runtime validation aligned with JSON Schema and PostgreSQL.

The following are intentionally separate follow-up projects:

- Git ancestry or valid-from commit applicability;
- old/new PR change-set matching;
- a PostgreSQL runtime adapter;
- vector retrieval or learned ranking;
- benchmark example identity and automated leakage classification.

## Compatibility Strategy

Existing dataclasses and low-level helpers remain importable. Valid existing
README calls continue to work. The README will recommend the new store-level
workflow because it binds retrieval, both gates, rendering, and logging into
one validated transaction.

Low-level prompt construction will fail closed when passed a candidate that
does not pass System Gate for the supplied context. Low-level rendering keeps
its current guardrails, while the store workflow is the only API documented as
providing gate-order and audit guarantees.

Legacy unversioned snapshots remain loadable through an explicit migration
path. New snapshots use version 2 and reject missing or unknown envelope keys.

## Architecture

### Immutable Boundary Models

Add `MemoryGateRequest` and `GatedMemoryResult` in `models.py`.

`MemoryGateRequest` contains only immutable values needed between the external
LLM call and finalization:

- request ID;
- scalar `MemoryContext`;
- candidate IDs;
- System Gate allowed IDs;
- sorted `(memory_id, reason)` blocked pairs;
- the bounded LLM gate prompt;
- a non-public store token.

The store also retains the pending request internally. A request from another
store, a fabricated request, or a request finalized twice is rejected.

`GatedMemoryResult` contains the final decision as immutable scalar/tuple
fields, the rendered snippet, the decision ID, and the trace ID. It exposes
everything a harness needs without exposing mutable store records.

### Two-Phase Safe Workflow

`TraceBackedMemoryStore.prepare_memory(context, *, task, query=None,
context_summary="")` performs:

1. runtime context validation;
2. metadata-first candidate retrieval;
3. deterministic System Gate filtering;
4. LLM prompt construction from System Gate allowed memory only;
5. registration of a one-use pending request.

`TraceBackedMemoryStore.finalize_memory(request, decision_payload, *,
trace_id, eval_result=None, memory_caused_failure=False)` performs:

1. request ownership and one-use validation;
2. trace existence plus repo, commit, and tenant consistency checks;
3. fresh candidate lookup and System Gate evaluation so memory obsoleted after
   preparation cannot be injected;
4. strict LLM decision parsing and intersection with System Gate output;
5. bounded snippet rendering;
6. creation of one audit log containing decision-time evidence;
7. request consumption and return of `GatedMemoryResult`.

Validation failure does not consume the request and does not append a partial
usage log. Successful finalization consumes the request exactly once.

### Store-Backed Lifecycle

Add store methods that apply the existing pure lifecycle functions and replace
records only after all invariants pass:

- `review_failure_case(case_id, ...)`;
- `verify_failure_case(case_id, ...)`;
- `obsolete_failure_case(case_id)`;
- `obsolete_lesson(lesson_id)`;
- `obsolete_project_policy(policy_id)`.

Only documented forward transitions are allowed. Obsoleting a failure case
atomically obsoletes every active lesson derived from it, preventing an active
lesson from retaining an obsolete source. Repeated obsoletion is idempotent;
reviewing or verifying a non-draft case remains an error.

### Provenance Scope Narrowing

When adding a lesson, the store resolves its source case and trace. If the
source trace declares `repo` or `tenant`, the lesson scope must declare the
same value. A derived lesson may narrow its source scope with additional
fields, but it may not omit or change those provenance boundaries.

Project policies remain the mechanism for intentionally broader manually
maintained rules.

### Historical Audit Events

Extend `MemoryUsageLog` with:

- `trace_id`;
- serialized non-null context fields;
- decision-time candidate status by memory ID;
- System Gate blocked reasons;
- an automatically generated UTC timestamp.

The safe workflow derives candidate IDs and evidence itself. The low-level
`log_decision()` method remains available but must resolve a unique stored
trace for the run, validate context against that trace, derive statuses from
the store, and record a timestamp.

`obsolete_memory_usage_attempts` is computed only from the status snapshots in
each usage event. Later lifecycle transitions therefore cannot rewrite prior
metrics.

### Store Isolation

Records are deep-copied on insertion. Public collection access returns copies
or read-only views, so mutating caller-owned nested lists or dictionaries does
not alter validated store evidence. Internal methods use private collections.

Snapshots, candidate lists, reports, and status maps use deterministic ID
ordering. Equivalent stores therefore produce byte-stable JSON snapshots.

### Snapshot Version 2

New snapshots contain exactly:

- `snapshot_version: 2`;
- `traces`;
- `failure_cases`;
- `lessons`;
- `project_policies`;
- `usage_logs`.

The loader accepts either this exact envelope or the exact five-key legacy
envelope. Legacy usage logs are migrated by deriving missing context/status
evidence where possible and receive no fabricated historical timestamp.
Malformed, truncated, or unknown envelopes fail instead of becoming an empty
store.

JSON writes use a temporary sibling file and `os.replace()` so readers never
observe a partially written snapshot.

## Runtime Validation

Store validators require exact booleans for `dirty`, `regression_passed`,
`sensitive`, `eval_leaking`, and `memory_caused_failure`. System Gate also
rejects non-boolean safety flags so directly constructed `MemoryItem` objects
fail closed.

Snapshot records must match dataclass field types relevant to security and
identity. Required string fields must be non-empty, scope keys and values must
be valid strings, and timestamp values must be null or RFC 3339 date-time
strings.

The JSON usage-log schema requires `eval_result` when
`memory_caused_failure` is true. PostgreSQL uses an explicit non-null failed or
errored result check, enforces case/trace commit equality with a composite
foreign key, and rejects empty required identity/text values and nullable
confidence.

## Error Handling

All contract failures raise `ValueError` with the record type, field, and
reason. Unknown IDs use stable messages naming the missing ID. Atomic lifecycle
operations validate the complete proposed state before replacing any record.

File read and JSON parse errors retain their native exception as the cause.
Atomic save cleanup removes only the temporary sibling created by that save.

## Testing

Every behavior change follows red-green-refactor.

Focused tests cover:

- sensitive, cross-scope, or malformed candidates never entering the LLM
  prompt;
- request ownership, replay rejection, and stale-memory rechecks;
- trace/context mismatches and all-or-nothing finalization;
- persisted draft review, verification, obsoletion, and lesson cascade;
- source repo/tenant scope narrowing;
- decision-time metrics remaining stable after later obsoletion;
- exact boolean rejection through direct insertion and snapshot loading;
- strict v2 envelopes, explicit v1 migration, deterministic ordering, deep
  copy isolation, and atomic save replacement;
- JSON Schema and SQL parity for the new audit fields and constraints;
- README end-to-end usage of the safe workflow.

Completion requires the focused tests, the full pytest suite, and
`git diff --check` to pass from the final worktree.
