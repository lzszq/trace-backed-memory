# Batch Measured Completion CLI Design

## Problem

The Store already exposes `complete_memory_runs()` as the all-or-nothing
boundary for several fresh measured results. The CLI exposes only
`complete_memory_run()` one run at a time, so an evaluator that loops over
`tbm complete --write` can persist an early result before a later conflict is
discovered.

Add one file-backed command that submits an ordered batch to the existing Store
API. The CLI must not reproduce linkage, shared-Trace merge, replay,
attribution, or commit rules.

## Command Surface

The console script and module entry point both support:

```text
tbm complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]
```

`SNAPSHOT` and `MEASUREMENTS_JSON` are local paths. The command accepts no
stdin, inline JSON, remote URL, database connection, alternate output path, or
outcome inference.

The measurement file is strict UTF-8 JSON. Its top level is a non-empty array
whose order is preserved in the returned completions:

```json
[
  {
    "decision_id": "decision_000001",
    "eval_result": "pass"
  },
  {
    "decision_id": "decision_000002",
    "eval_result": "error",
    "memory_caused_failure": true,
    "tool_outputs": [],
    "error": "executor timeout"
  }
]
```

Each object requires `decision_id` and `eval_result`. It may contain only the
remaining `MemoryRunResult` fields: `memory_caused_failure`, `output_hash`,
`tool_outputs`, `latency_ms`, `cost_usd`, `error`, and `trace_uri`.
`trace_id` is deliberately absent because the Store derives it from the
validated decision.

## Manifest Boundary

The parser rejects unreadable files, invalid UTF-8 or JSON, non-finite numbers,
duplicate object keys, an empty or non-array top level, non-object entries,
missing required fields, unknown fields, and wrong JSON field types as input
errors. `tool_outputs` is either null or an array of objects and becomes a tuple
at the immutable `MemoryRunResult` boundary. Optional string fields and numeric
fields may be null. `latency_ms` is an exact integer, `cost_usd` is a finite
number, and `memory_caused_failure` is an exact boolean.

An omitted optional field and explicit null both map to `None`, preserving the
Store's omission behavior. An explicit empty `tool_outputs` array maps to an
empty tuple and requests an empty persisted list. Omitted
`memory_caused_failure` defaults to false.

The Store remains authoritative for non-empty identity values, measured outcome
values, duplicate decision IDs, unknown decisions, attribution rules, immutable
evidence, partial state, exact replay, and shared-Trace result/evidence
compatibility. Those failures remain state errors.

## Execution And Atomicity

After loading the snapshot, the command parses the complete manifest into one
tuple and calls `complete_memory_runs()` exactly once. The Store derives every
Trace link, stages every Trace and usage-log candidate, and commits none unless
the whole batch is valid. Completion results retain manifest order.

The command is a dry run by default. `--write` serializes the success envelope
first and then uses `save_json()` for synchronized same-path atomic replacement.
Input, state, serialization, or write failure leaves the snapshot unchanged. A
stdout failure after a successful write remains success to avoid an unsafe
retry.

Success reuses the existing deterministic completion envelope with
`completions`, ordered `decision_ids`, and `written`. Existing JSON errors and
exit codes remain authoritative: 0 success, 1 internal, 2 input, 3 state, and 4
write.

## Compatibility And Non-Goals

`MemoryRunResult` remains ephemeral. The feature changes only existing Trace
completion fields and usage outcome pairs. It adds no manifest JSON Schema or
persisted command state and leaves snapshot version 2, JSON Schemas,
active-lessons YAML, packaged resources, `schemas/postgres.sql`, and PostgreSQL
schema version 1 unchanged.

## Verification

Tests cover dry-run isolation, ordered mixed results, complete evidence,
omission versus explicit empty tool outputs, exact replay, atomic writes,
strict manifest failures, duplicate and unknown decisions, later-item rollback,
shared-Trace conflicts, write failures, post-commit stdout failures, module and
installed-console entry points, documentation, and unchanged persistence
versions and resources.
