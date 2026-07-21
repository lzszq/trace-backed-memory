# Deferred Decision Outcome CLI Design

## Summary

`TraceBackedMemoryStore.record_decision_outcome()` already provides the
store-owned transition from an unevaluated usage decision to one measured
outcome. The CLI currently exposes measured completion only through
`complete`, which also completes the linked Trace and requires Trace evidence.
Callers that evaluate a decision independently cannot use the supported
decision-only lifecycle from an installed command.

Phase 38 adds a thin, privacy-bounded CLI adapter for that existing Store API.
It does not add another state machine, complete a Trace, or persist a command
record.

## Command

```text
tbm outcome SNAPSHOT DECISION_ID \
  --eval-result {pass,fail,error} \
  [--memory-caused-failure true|false] \
  [--write]
```

`python -m trace_backed_memory` exposes the identical command. The measured
result is required. Attribution defaults to `false`, matching
`record_decision_outcome()`; callers replaying a sealed `true` attribution must
state it explicitly.

The command loads the regular bounded snapshot and calls
`record_decision_outcome()` exactly once. It accepts no Trace ID, execution
evidence, alternate output path, PostgreSQL connection, batch, stdin, or remote
URL.

## Lifecycle Semantics

- A decision with `eval_result=None` or `unknown` may advance once to `pass`,
  `fail`, or `error` plus one exact attribution boolean.
- Exact replay of the measured pair succeeds with `changed=false`.
- A different measured result or attribution is a state error.
- `memory_caused_failure=true` requires `fail` or `error` and at least one used
  memory ID, as enforced by the Store.
- An unknown, empty, or oversized decision ID is a state error.
- The linked Trace is validated but never changed by this command.

The CLI reads the previous usage outcome only to produce the status summary.
It delegates all transition validation and mutation to the Store.

## Output

Success emits one deterministic JSON object plus one newline:

```json
{
  "changed": true,
  "decision_id": "decision_001",
  "eval_result": "pass",
  "memory_caused_failure": false,
  "previous_eval_result": null,
  "previous_memory_caused_failure": false,
  "written": false
}
```

`changed` compares only the previous and returned outcome pair. A dry-run may
therefore report `changed=true` while leaving the source bytes untouched.
`written` reports whether same-path snapshot publication was requested and
completed.

The output intentionally excludes the rest of `MemoryUsageLog`, including
run/Trace IDs, context, reason, risk, candidate/used/blocked memory IDs, status
snapshots, and System Gate reasons. It also excludes Trace execution evidence,
memory content, scope, and tool output.

## Failure and Publication Boundary

- argparse and snapshot document failures use the existing input error and
  exit 2;
- Store transition failures use state error and exit 3;
- output serialization or unexpected failures use internal error and exit 1;
- snapshot publication failures use write error and exit 4.

Dry-run is the default. The command serializes the complete output before
`--write` publishes the snapshot through the existing same-directory atomic
replacement. A serialization, transition, or publication failure preserves the
original snapshot. Closed stdout before publication is an internal failure;
closed stdout after a successful write returns success so retry cannot turn an
idempotent operation into a misleading failure.

## Compatibility and Verification

The command writes only the existing `MemoryUsageLog.eval_result` and
`memory_caused_failure` fields. Snapshot version 2, every JSON Schema,
active-lessons YAML, all 18 packaged resource bytes, `schemas/postgres.sql`,
PostgreSQL schema version 1, and the Store/PostgreSQL lifecycle remain
unchanged.

Tests cover dry-run, write, no-Trace mutation, privacy-bounded exact output,
failure attribution, exact replay, conflicts, invalid and unknown IDs,
serialization/write/Store failures, BrokenPipe behavior, module invocation,
and isolated wheel/sdist command smoke.

