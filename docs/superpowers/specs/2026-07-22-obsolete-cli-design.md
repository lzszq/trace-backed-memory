# Memory Obsolescence CLI Design

## Problem

The Store already provides forward-only, idempotent obsolescence for failure
cases, lessons, and project policies. Obsoleting a failure case also validates
and atomically obsoletes every active lesson derived from it. Operators cannot
perform these safety transitions through the CLI, so deactivating stale or
incorrect memory still requires Python glue.

Add one narrow snapshot command that delegates every transition to the Store,
previews irreversible effects by default, and exposes no record text or raw
execution evidence.

## Command

Expose:

```text
tbm obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID [--write]
```

`SNAPSHOT` uses the existing bounded, fully validating snapshot loader. The
kind is an argparse choice and maps only to `obsolete_failure_case()`,
`obsolete_lesson()`, or `obsolete_project_policy()`. `MEMORY_ID` is forwarded
unchanged so the Store remains authoritative for non-empty and bounded IDs,
unknown records, current status, validation, and idempotence.

The command accepts one local snapshot only. It does not connect to
PostgreSQL, accept stdin or remote URLs, infer a kind from a shared ID, edit a
record, reactivate memory, attach a reason or actor, or provide a batch mode.
Batch obsolescence would need its own Store-level all-or-nothing API rather
than a CLI loop.

## Transition And Cascade Semantics

For a failure case, the adapter captures the current case status and the IDs of
its currently active dependent lessons, then calls `obsolete_failure_case()`
exactly once. The Store constructs and validates the obsolete case and every
dependent lesson before updating any record. On success, the captured active
lesson IDs are the actual cascade and are sorted for deterministic output.
Unrelated and already-obsolete lessons are not reported.

For a lesson or project policy, the adapter captures the current status and
calls the corresponding Store method exactly once. The CLI does not duplicate
status-transition or contract validation. A record already obsolete is a
successful idempotent no-op with `changed: false`; no transition can restore an
obsolete record.

The reconstructed Store changes only in memory by default. `--write` is the
sole persistence gate and calls `save_json()` on the same snapshot after the
complete transition succeeds. A dry-run therefore previews the exact cascade
while leaving source bytes unchanged. As with other snapshot mutation
commands, concurrent external modification of the same file is caller-owned;
the CLI adds no cross-process transaction or lock.

## Output

Success emits one canonical JSON value:

```json
{
  "cascaded_count": 2,
  "cascaded_lesson_ids": ["lesson-a", "lesson-b"],
  "changed": true,
  "memory_id": "case-1",
  "memory_kind": "failure_case",
  "previous_status": "verified",
  "status": "obsolete",
  "written": false
}
```

`memory_kind` uses the internal stable values `failure_case`, `lesson`, and
`project_policy`. Cascade fields are always present and empty for non-case
operations or idempotent no-ops. The envelope intentionally omits symptom,
fix, lesson text, policy text, scope, sensitive flags, Trace data, and tool
evidence.

## Errors And Durability

CLI shape and unsupported-kind failures are input errors with exit code 2.
Snapshot path, encoding, JSON, and validation failures retain input exit 2.
Unknown IDs or rejected Store transition state use exit code 3. Snapshot
publication failures use exit code 4, and unexpected failures use exit code 1.

The result is serialized before `save_json()` runs. A failed transition or
serialization never reaches persistence; a failed atomic snapshot write keeps
the prior destination. If stdout closes after a requested write commits, the
command remains successful to avoid retrying an irreversible operation. A
dry-run stdout failure remains an internal error because nothing committed.

## Compatibility And Non-Goals

This phase adds only a CLI adapter over existing Store methods. It changes no
lifecycle helper, record field, status value, cascade rule, public Python
export, or PostgreSQL synchronization behavior. It persists no command,
preview, cascade manifest, reason, or actor. Snapshot version 2, all JSON
Schemas, active-lessons YAML, all 18 packaged resources and their bytes,
`schemas/postgres.sql`, and PostgreSQL schema version 1 remain unchanged.

## Verification

Tests cover all three kinds, failure-case cascades, unrelated and previously
obsolete lessons, dry-run byte isolation, explicit writes, idempotent replay,
draft and verified cases, unknown/empty IDs, invalid kinds, Store call count,
transition and write failures, deterministic non-sensitive output, post-write
stdout closure, module and installed entry points, documentation, the full
suite, and isolated wheel/source-distribution smoke tests.
