# Snapshot Operations CLI Design

## Problem

The store already validates JSON snapshots and exposes stable audit, metrics,
remediation, and recovery APIs. Operators still need to write Python to inspect
or repair one snapshot. That makes CI checks, cron maintenance, and incident
response unnecessarily bespoke, and it encourages callers to duplicate state
classification outside the store.

Provide a dependency-free command-line interface that exposes existing
snapshot operations as deterministic JSON. It must preserve the store as the
only source of validation and recovery semantics, default every mutation to a
dry run, and reuse atomic snapshot replacement for explicit writes.

## Command Surface

Install the `tbm` console script and support the same interface through
`python -m trace_backed_memory`:

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

All commands load one local snapshot through
`TraceBackedMemoryStore.load_json()`. Stdin, PostgreSQL connections, remote
URLs, and arbitrary output paths are outside this phase.

## Read Output

Every successful non-help command writes exactly one UTF-8 JSON value plus a
newline to stdout with stable key ordering.

- `snapshot validate` returns `valid`, `snapshot_version`, and collection
  `counts` after full store reconstruction.
- `snapshot stats` returns `snapshot_version` and the same canonical counts.
- `audit` returns `asdict()` output for `memory_run_audits()` in decision order.
- `metrics` returns `memory`, `memory_runs`, and `memory_outcomes` from the
  three existing metrics APIs.
- `remediation` returns `asdict()` output for
  `memory_run_remediations()` in decision order.

Counts cover `traces`, `failure_cases`, `lessons`, `project_policies`, and
`usage_logs`. The CLI does not define a second schema for stored records; it
serializes the existing frozen public records.

## Recovery And Dry Runs

All three recovery commands load and mutate only an in-memory store first.
Without `--write`, the input bytes remain unchanged. With `--write`, the CLI
calls `store.save_json(SNAPSHOT)` only after the complete operation succeeds;
that method writes a same-directory temporary file and replaces the target.

Every successful recovery command returns one object:

```json
{
  "completions": [],
  "decision_ids": [],
  "written": false
}
```

Completion objects are defensive `MemoryRunCompletion` values serialized with
`asdict()`. `recover-ready` delegates to `recover_ready_memory_runs()` and may
return empty lists. `recover` delegates to `recover_memory_run()` and passes
`memory_caused_failure` only when the option is explicit. `recover-batch`
preserves command-line decision order, rejects duplicate IDs before recovery,
parses repeated attribution entries strictly, and delegates to
`recover_memory_runs()`.

## Error And Exit Contract

Argument parsing uses an `argparse` subclass so usage errors are JSON rather
than free-form parser text. Except for `--help`, failures write exactly one
object to stderr:

```json
{"error":{"kind":"input","message":"...","type":"ValueError"}}
```

Exit codes:

- `0`: successful read, dry run, write, or no-op ready recovery;
- `1`: unexpected internal error;
- `2`: usage, path, encoding, JSON, or snapshot validation error;
- `3`: recovery state or attribution rejection from the store;
- `4`: snapshot write failure.

Messages expose exception type and bounded runtime text but no traceback.
`KeyboardInterrupt` and `SystemExit` are not converted into internal errors.

## Implementation Boundaries

- Add `trace_backed_memory.cli` using only `argparse`, `dataclasses`, `json`,
  `pathlib`, and existing package APIs.
- Add `trace_backed_memory.__main__` as the module entry point.
- Add `[project.scripts] tbm = "trace_backed_memory.cli:main"`.
- Add an explicit setuptools build backend so console-script packaging is
  reproducible rather than relying on an implicit default.
- Do not export CLI internals from the package root.
- Do not duplicate snapshot envelope, audit classification, metrics, or
  recovery validation.

## Persistence And Compatibility

The CLI reads and writes the existing snapshot version 2 shape. It creates no
new persisted field and does not change active-lessons YAML or PostgreSQL
schema version 1. A successful write canonicalizes the snapshot through the
normal store serializer; a dry run or any failure leaves the original file
unchanged.

## Verification

Tests cover parser JSON errors, every read command, deterministic ordering,
malformed files and snapshots, recovery dry-run isolation, successful atomic
writes, missing attribution, pending/conflicting states, duplicate batch IDs,
batch all-or-nothing behavior, ready no-op idempotence, module invocation,
installed console-script smoke behavior, README commands, build metadata, and
unchanged persistence schemas.
