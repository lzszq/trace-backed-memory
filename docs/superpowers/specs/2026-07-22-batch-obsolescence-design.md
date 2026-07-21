# Atomic Batch Obsolescence Design

## Problem

The snapshot CLI can make one failure case, lesson, or project policy obsolete.
It deliberately has no batch loop because a later invalid item would otherwise
leave earlier records changed in memory. Operators still need a safe way to
deactivate a reviewed set of memories with one all-or-nothing decision.

Add a Store-level batch transition first, then expose one bounded manifest
adapter. Preserve the existing forward-only lifecycle, failure-case cascade,
dry-run default, and non-sensitive output.

## Public Store Contract

Add the frozen public input record:

```python
MemoryKind = Literal["failure_case", "lesson", "project_policy"]

@dataclass(frozen=True)
class MemoryObsolescenceRequest:
    memory_kind: MemoryKind
    memory_id: str
```

Expose:

```python
TraceBackedMemoryStore.obsolete_memories(
    requests: tuple[MemoryObsolescenceRequest, ...],
) -> tuple[FailureCase | Lesson | ProjectPolicy, ...]
```

The method requires an exact, non-empty tuple of exact request records. Kinds
must use the three canonical underscore values, IDs remain subject to the
existing non-empty and 128-character boundary, and requested memory IDs must
be unique. Each kind is looked up only in its matching Store collection; the
method never guesses a kind from the shared ID namespace.

The returned records correspond only to explicit requests and preserve request
order. They are deep copies of the post-transition records. Already-obsolete
records remain successful idempotent results.

## Atomic Transition

The Store resolves every request against the entry state before mutating any
collection. It stages obsolete candidates for all requested records. Every
requested non-obsolete failure case also stages every active derived lesson,
including a lesson that is separately present in the same request tuple.

The Store validates all staged cases, lessons, and policies, including lesson
contracts, before publishing any candidate. Only then does it update the three
collections. An empty or malformed tuple, duplicate ID, wrong kind, unknown
record, transition failure, or later candidate validation failure leaves the
entire Store unchanged.

An explicitly requested lesson may overlap a requested case's cascade. This is
valid and order-independent: the same obsolete lesson candidate is staged
once, while the explicit result remains in request order. No record is counted
or written twice.

## Command And Manifest

Expose:

```text
tbm obsolete-batch SNAPSHOT REQUESTS_JSON [--write]
```

`REQUESTS_JSON` is strict UTF-8 JSON under the existing 8 MiB, 10,000-item,
100,000-node, and depth-100 CLI limits. Its root is a non-empty array. Every
item is an exact object with only:

```json
{"memory_kind": "failure_case", "memory_id": "case-1"}
```

Both fields are required strings. `memory_kind` accepts only `failure_case`,
`lesson`, or `project_policy`. Duplicate object keys, unknown or missing
fields, wrong types, non-finite values, and unsupported kinds fail during
document parsing. The adapter constructs one request tuple and calls
`obsolete_memories()` exactly once; it never loops over the three single-item
Store methods.

## Output

Success emits one deterministic non-sensitive object:

```json
{
  "affected_count": 4,
  "cascaded_count": 2,
  "cascaded_lesson_ids": ["lesson-a", "lesson-b"],
  "changed_count": 3,
  "requested_count": 3,
  "results": [
    {
      "changed": true,
      "memory_id": "case-1",
      "memory_kind": "failure_case",
      "previous_status": "verified",
      "status": "obsolete"
    }
  ],
  "written": false
}
```

`results` preserves manifest order. `changed_count` counts explicit requests
whose status changed. `cascaded_lesson_ids` is the sorted set of entry-active
lessons transitioned by requested failure cases, including any lesson also
listed explicitly. `affected_count` is the size of the union of changed
explicit IDs and cascaded lesson IDs, so overlapping explicit/cascade entries
are counted once. The envelope never contains memory text, scope, Trace data,
tool evidence, manifest paths, actor, or reason fields.

## Dry Run, Errors, And Retry Safety

The fully validated Store changes only in memory by default. `--write` remains
the sole publication gate and atomically replaces the same snapshot after the
complete batch succeeds. Output serialization happens before persistence. A
failed parse, transition, serialization, or write cannot publish a partial
batch.

CLI shape and manifest format errors use exit 2. Store identity, duplicate,
kind/ID, and transition rejection use exit 3. Snapshot write failure uses exit
4, and an unexpected failure uses exit 1. A closed stdout after a committed
write remains success; a dry-run stdout failure remains internal failure.

## Compatibility And Non-Goals

This phase adds one public input record, one Store method, and one CLI command.
It does not alter existing single-item methods or command output. The manifest
is ephemeral and no batch, actor, or reason record is persisted. There is no
reactivation, partial-success mode, skip-unknown mode, stdin, remote URL, or
direct PostgreSQL CLI access.

The resulting ordinary status updates continue to synchronize through the
existing PostgreSQL transaction. Snapshot version 2, JSON Schemas,
active-lessons YAML, all 18 packaged resources and their bytes,
`schemas/postgres.sql`, and PostgreSQL schema version 1 remain unchanged.

## Verification

Store tests cover mixed kinds, manifest order, cascades, overlap, idempotence,
duplicates, wrong kinds, unknown later items, exact type boundaries, candidate
validation failure, deep-copy results, and byte-equivalent atomic failure.
CLI tests cover strict bounded manifests, one Store call, exact counts and
ordering, dry-run isolation, explicit writes, state and write errors, output
serialization, stdout closure, module entry points, and non-sensitive output.
Wheel and source-distribution smoke tests exercise installed batch commands.
