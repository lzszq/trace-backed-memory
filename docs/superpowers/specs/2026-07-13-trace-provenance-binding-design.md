# Declared Trace Provenance Binding Design

## Problem

Safe finalization and usage-log import currently bind a memory context to its
Trace only through `repo`, `commit_sha`, `tenant`, and an optional benchmark
identity pair. A context can declare branch, prompt, model, eval-suite, or tool
provenance that disagrees with the final Trace, yet the store records that
context as trace-linked audit evidence. Candidate retrieval and gating may then
run against metadata that the linked execution never had.

## Binding Rules

Keep the existing always-bound fields:

- `repo`;
- `commit_sha`;
- `tenant`, including exact `None` behavior.

Add declared-only scalar binding for:

- `branch`;
- `prompt_version`;
- `prompt_family`;
- `tool_schema_version`;
- `model`;
- `eval_suite`.

For these fields, a non-`None` context value must exactly equal the linked
Trace value. An omitted context value remains broad and does not require the
Trace value to be absent.

When `context.tool` is present, it must exactly equal at least one non-empty
plain-string tool-call name on the Trace. Tool names are not coerced.

The existing benchmark rule remains: a present context `input_hash` requires
`eval_suite`, and both must match the linked Trace.

`model_family`, `task_type`, and `failure_type` remain unbound because Trace
does not persist equivalent provenance. Adding that provenance or a database
migration is outside this phase.

## Runtime Boundaries

Apply the rules in both:

- `finalize_memory()` before candidate reconstruction, decision parsing,
  snippet rendering, request consumption, or log append;
- low-level `log_decision()` before creating or appending a usage log.

A mismatch must leave a pending gate request reusable with a matching Trace.

## Imported Audit Evidence

When a usage-log context contains a declared trace-backed field, snapshot and
PostgreSQL loading must require the same exact match. A missing optional field
remains compatible. A present tool must match an exact plain-string tool call.

Legacy usage logs migrated to the core context fields remain valid. Supplied
legacy decision-time optional evidence is validated rather than silently
trusted.

## Compatibility

- `MemoryContext`, `Trace`, requests, usage logs, and public signatures do not
  change.
- Callers that omit optional trace-backed context fields keep existing broad
  behavior.
- Retrieval, System Gate, LLM Gate, PR matching, and benchmark classification
  semantics do not change.
- Snapshot version 2, JSON Schemas, active-lessons YAML, and PostgreSQL schema
  version 1 remain unchanged.

## Verification

Tests cover every declared scalar field, exact tool matching and non-string
tool names, omitted-field compatibility, pending-request and append atomicity,
low-level logging, modern and legacy imported evidence, benchmark-pair
compatibility, documentation, and full persistence parity.
