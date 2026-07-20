# Measured Completion CLI Design

## Problem

The store already exposes `complete_memory_run()` as the atomic boundary for a
fresh measured result. Snapshot operators and CI jobs cannot use that boundary
without writing Python, while the existing recovery commands only reconcile a
result that was already written to one side of the trace/decision relationship.

Add one command that submits an explicit measured result to a pending memory
run. The CLI must delegate linkage, replay, attribution, evidence, and atomicity
rules to the store rather than introducing a second completion model.

## Alternatives

### A. Explicit flags plus a tool-output file

Use positional snapshot and linked IDs, require the measured outcome, expose
small scalar evidence as flags, and read structured tool outputs from one JSON
file. This keeps the normal invocation readable, preserves omitted evidence,
and avoids shell-dependent JSON quoting. This is the selected design.

### B. One measurement-object JSON file

A single JSON object could mirror `MemoryRunMeasurement`, but it would hide the
three identifiers that make a completion auditable and require another public
input schema and parser. That duplication is not justified for a single-run
command.

### C. Reuse a recovery command

Recovery cannot safely stand in for completion. It derives an already-recorded
outcome from partial state; it must never invent the fresh measured outcome
that this command requires.

Inline JSON for tool outputs is also excluded because quoting and command-line
size behavior vary across shells.

## Command Surface

Support the same command through `tbm` and
`python -m trace_backed_memory`:

```text
tbm complete SNAPSHOT TRACE_ID DECISION_ID \
  --eval-result {pass,fail,error} \
  [--memory-caused-failure true|false] \
  [--output-hash VALUE] \
  [--tool-outputs-file PATH] \
  [--latency-ms INTEGER] \
  [--cost-usd NUMBER] \
  [--error VALUE] \
  [--trace-uri VALUE] \
  [--write]
```

`--eval-result` is required and cannot be `unknown`.
`--memory-caused-failure` defaults to `false`, matching the public store API;
the store rejects `true` for a passing result. Cost must be finite. Other
scalar evidence is passed through to the store for canonical validation.

The command reads one local snapshot through `TraceBackedMemoryStore.load_json`
and calls `complete_memory_run()` exactly once. It never infers the outcome,
trace ID, decision ID, failure attribution, or evidence.

## Structured Evidence

`--tool-outputs-file` reads a UTF-8 JSON document that must be an array of
objects. Path, decoding, JSON syntax, non-finite JSON constants, top-level
shape, and item-shape failures are input errors.

Omission is significant. An absent evidence flag is not forwarded, preserving
the store's internal `UNSET` behavior and any compatible evidence already on a
partially completed trace. A provided file containing `[]` is forwarded as an
explicit empty tool-output list. This command has no stdin input or inline JSON
mode.

## Output And Persistence

Success returns the same deterministic completion envelope as recovery:

```json
{
  "completions": [],
  "decision_ids": [],
  "written": false
}
```

The single completion is serialized with `asdict()` and its decision ID is the
only entry in `decision_ids`. Stable key ordering and one trailing newline use
the existing CLI serializer.

The command is a dry run by default: it mutates only the loaded in-memory
store. With `--write`, the completed trace and sealed usage log are serialized
first and then committed through the existing atomic same-path snapshot
replacement. Any input, completion, serialization, or write failure leaves the
input snapshot unchanged. A stdout failure after a successful write does not
reclassify the persisted operation as failed.

## Error And Exit Contract

The existing JSON error envelope and exit codes remain authoritative:

- `0`: successful dry run or write;
- `1`: unexpected internal failure;
- `2`: usage, snapshot input, or tool-output file failure;
- `3`: linkage, state, replay, attribution, or evidence rejection by the store;
- `4`: snapshot write failure.

The CLI only classifies file parsing and argument conversion. Store `ValueError`
messages remain state errors so domain behavior is not duplicated in argparse.

## Compatibility And Non-Goals

This phase adds no persisted fields and does not change snapshot version 2,
active-lessons YAML, PostgreSQL schema version 1, or the Python completion API.
Batch measured completion, remote stores, database connections, alternate
output paths, stdin, outcome inference, and a new measurement JSON schema are
outside scope.

## Verification

Tests cover required arguments, deterministic dry-run output, explicit full
evidence, omitted-versus-empty tool outputs, default and explicit attribution,
successful atomic writes, replay, mismatched IDs, invalid attribution, invalid
scalar evidence, malformed structured evidence, unchanged files on every
failure class, write failures, stdout failures after commit, module and console
entry points, README coverage, and unchanged persistence schema versions.
