# Benchmark Leakage Classification Design

## Summary

Traces already persist `eval_suite` and `input_hash`, but runtime context does
not carry the current input hash and derived memory does not retain its source
example identity. The System Gate can therefore block only manually marked
`eval_leaking` memory. It cannot automatically recognize that a lesson or
failure case came from the exact benchmark example currently being evaluated.

This project defines benchmark example identity as the exact pair
`(eval_suite, input_hash)`. The runtime context optionally supplies the current
pair. Memory items derived from a trace carry the complete source pair as
ephemeral provenance. The System Gate automatically blocks a source-derived
memory when both pairs are complete and exactly equal.

Static `eval_leaking=True` remains an independent, globally authoritative
block. Incomplete identities never trigger a guessed match.

## Goals

- Use existing trace `eval_suite` and `input_hash` values as benchmark example
  identity without adding a duplicate persisted identifier.
- Allow `MemoryContext` to carry the current input hash.
- Require a context input hash to be accompanied by an eval suite.
- Propagate complete source identity into ephemeral lesson and failure-case
  `MemoryItem` values.
- Automatically block memory derived from the current benchmark example in
  every runtime mode.
- Preserve the block through prepare, finalize, LLM-prompt, direct injection,
  and audit boundaries.
- Persist the current input hash and automatic block reason in usage evidence.
- Validate that a finalized trace matches the benchmark identity bound at
  preparation time.
- Preserve static sensitivity and `eval_leaking` behavior and precedence.
- Keep snapshot version 2 and PostgreSQL schema version 1.
- Require no new dependency or PostgreSQL migration.

## Non-goals

- Inspecting raw benchmark inputs, expected outputs, evaluator reasoning, or
  lesson text for semantic overlap.
- Treating `output_hash` as an expected-answer identity.
- Guessing identity when either eval suite or input hash is absent.
- Automatically marking a lesson's persisted `eval_leaking` flag.
- Blocking memory from a different example in the same eval suite.
- Changing eval mode's existing policy-only memory restriction.
- Defining how callers canonicalize raw benchmark input before hashing.
- Enforcing one hash algorithm or hash encoding in stored legacy traces.
- Adding a new trace, lesson, failure-case, or PostgreSQL column.
- Persisting source benchmark identity on lessons or failure cases.

## Caller Contract

Callers that want automatic classification must:

1. assign a stable eval-suite name;
2. canonicalize one benchmark example input deterministically;
3. compute a collision-resistant, privacy-preserving hash;
4. store that hash as `Trace.input_hash` on source and current traces;
5. pass the same value as `MemoryContext.input_hash` with the matching
   `MemoryContext.eval_suite`.

The library compares opaque bounded strings exactly. It does not inspect raw
inputs or validate a particular digest format. Hash collisions, inconsistent
canonicalization, or suite-name drift are caller data-quality failures.

## Alternatives Considered

### 1. Dynamic provenance classification using the existing pair (selected)

Carry `(eval_suite, input_hash)` through context and ephemeral memory
provenance, then compare at the deterministic gate. This has exact per-example
semantics, does not over-block other examples, requires no persisted migration,
and records the decision reason in the existing audit path.

### 2. Persist a new benchmark-example ID everywhere

Add a dedicated ID to traces, cases, lessons, snapshots, JSON Schemas, and
PostgreSQL. This is explicit but duplicates existing trace identity, requires a
database/schema migration, and still depends on caller canonicalization.

### 3. Permanently mark all eval-derived lessons as leaking

Set `eval_leaking=True` whenever a lesson descends from a trace with an eval
suite. This is simple but globally blocks general procedural lessons that may
be safe for different examples, and it cannot distinguish current-example
leakage from suite-wide policy.

## Public Model Changes

Append one optional field to `MemoryContext` so existing positional
construction remains compatible:

```python
@dataclass(frozen=True)
class MemoryContext:
    # existing fields
    input_hash: str | None = None
```

Append two optional ephemeral provenance fields to `MemoryItem`:

```python
@dataclass(frozen=True)
class MemoryItem:
    # existing fields
    source_eval_suite: str | None = None
    source_input_hash: str | None = None
```

The two source fields form one identity and must be both present or both
absent. They are not included in any stored record or snapshot.

No new public identity dataclass is added. The pair is intentionally explicit
at API and audit boundaries and reuses already persisted trace data.

## Context Validation And Parsing

Add `input_hash` as an optional bounded non-empty string context field.
`validate_memory_context()` and `parse_memory_context()` enforce:

- absent input hash remains valid and preserves existing behavior;
- a present input hash requires a present eval suite;
- values follow the existing `METADATA_VALUE_MAX_CHARS` bound;
- booleans, bytes, numbers, containers, empty strings, and overlong strings are
  rejected by the existing context rules.

`schemas/memory_context.schema.json` gains the optional `input_hash` property
and an `if`/`then` requirement that `eval_suite` is present when `input_hash`
is present.

`input_hash` is identity evidence, not memory scope. Lesson and policy scope
fields remain unchanged, and memory scope validation continues to reject
`input_hash`.

## Source Identity Propagation

A trace contributes source benchmark identity only when both `eval_suite` and
`input_hash` are valid non-empty strings. If either is absent, derived memory
gets neither source field.

Extend `memory_item_from_lesson()` with an optional keyword-only source trace:

```python
def memory_item_from_lesson(
    lesson: Lesson,
    *,
    source_trace: Trace | None = None,
) -> MemoryItem:
```

The existing one-argument call remains unchanged. When a complete source trace
is supplied, the returned item carries both source fields.

`memory_item_from_failure_case()` already receives its source trace and carries
the complete pair automatically.

The store resolves a lesson through lesson -> source case -> source trace and
uses the enriched helper in both:

- metadata candidate construction during preparation;
- memory reconstruction during finalization and usage-log validation.

Project policy memory has no source benchmark identity.

## Memory Item Contract

The deterministic memory contract validates source identity before gating:

- each source field is `None` or a non-empty string bounded by
  `METADATA_VALUE_MAX_CHARS`;
- exact string types are required;
- one present field without the other is invalid;
- the pair is not accepted in memory scope and is never rendered as memory
  text.

Malformed source identity is a contract error, not a leakage classification.

## Automatic System Gate Rule

After existing context and memory contract checks, and after static sensitive
and `eval_leaking` checks, the System Gate compares identities:

```text
context.eval_suite == memory.source_eval_suite
and
context.input_hash == memory.source_input_hash
```

The automatic rule applies only when the context and memory source pairs are
complete. An exact match blocks with this stable reason:

```text
memory originates from current benchmark example
```

Different hashes in the same suite, identical hashes in different suites, or
any incomplete pair do not trigger the automatic rule.

Static `eval_leaking=True` is evaluated first and retains its existing reason,
because it declares global risk rather than current-example provenance.

The rule applies in every mode. Eval mode's policy-only restriction remains a
separate later rule.

## Retrieval, Preparation, And Finalization

Metadata retrieval does not filter by input hash. Source-derived memory from
the same example remains a candidate so the System Gate can produce explicit,
auditable block evidence. Retrieval by eval-suite scope remains unchanged.

`prepare_memory()` receives enriched candidates, blocks same-example memory,
and excludes it from the LLM Gate prompt. The pending request retains candidate
IDs and blocked evidence as it does today; it does not persist source identity.

`finalize_memory()` reconstructs enriched items and reruns the System Gate.
This preserves the same automatic block and prevents a prepared candidate from
losing its source identity during finalization.

Commit ancestry remains independent. Same-example candidates may still require
ancestry evidence before the System Gate because ancestry is an applicability
boundary and anchor discovery describes the complete metadata candidate set.

## LLM And Injection Boundaries

`build_llm_gate_prompt()` already reruns the System Gate. Same-example memory is
therefore rejected before prompt rendering, and neither current nor source
input hashes are exposed to the LLM prompt.

Extend `build_injection_snippet()` with an optional keyword-only context:

```python
def build_injection_snippet(
    memories: list[MemoryItem],
    *,
    recommended_injection: str | None = None,
    decision: MemoryDecision | None = None,
    context: MemoryContext | None = None,
) -> str:
```

If any memory carries source benchmark identity, injection requires a valid
context. The helper applies the automatic same-example rule before rendering.
The store passes its bound request context during finalization. Legacy callers
whose memory items have no source identity remain compatible without context.

Static sensitive and eval-leaking injection guardrails remain unchanged.

## Trace Binding And Audit

When `MemoryContext.input_hash` is present, finalization requires the trace to
match both `context.eval_suite` and `context.input_hash`, in addition to the
existing repo, commit, and tenant checks. Validation completes before a pending
request is consumed or a usage event is appended.

`_context_evidence()` automatically includes the current input hash. Usage-log
trace validation requires a logged input hash to be accompanied by eval suite
and requires both to match the linked trace. Legacy logs without input hash
retain current behavior.

The usage log records:

- current `eval_suite` and `input_hash` in context evidence;
- same-example candidates in candidate IDs/statuses;
- the automatic reason in `system_blocked_reasons`;
- no source benchmark identity fields.

This supplies an auditable automatic classification without persisting a new
memory-record field.

## Persistence Compatibility

Persisted trace fields already include `eval_suite` and `input_hash` in JSON
snapshots, JSON Schema, and PostgreSQL. No trace or database column changes are
needed.

The optional current input hash can appear in `MemoryUsageLog.context`, whose
JSON Schema and PostgreSQL JSONB contract already accept bounded string
properties. Snapshot version remains 2 and PostgreSQL schema version remains 1.

The only schema edit is the optional `input_hash` property and cross-field
requirement in `schemas/memory_context.schema.json`.

No changes are made to:

- trace, failure-case, lesson, policy, or usage-log persisted dataclass fields;
- trace/lesson/policy JSON record schemas;
- active-lessons YAML;
- `schemas/postgres.sql`;
- `PostgresMemoryRepository` SQL or synchronization rules.

## Error Handling

Stable validation failures cover:

- context input hash without eval suite;
- malformed or partial memory source identity;
- injection of source-identified memory without context;
- injection of memory from the current benchmark example;
- finalized trace eval-suite or input-hash mismatch;
- usage-log benchmark context missing its pair or mismatching the linked trace.

All failures occur before mutable store state or audit state changes.

## Testing

Implementation follows red-green-refactor. Focused tests cover:

- context construction, validation, parsing, JSON Schema, and optional-field
  backward compatibility;
- input hash requiring eval suite and exact string/length validation;
- source identity pair validation on `MemoryItem`;
- failure-case and lesson propagation with complete and incomplete traces;
- project policies remaining source-identity-free;
- store candidate and finalization reconstruction preserving source identity;
- exact same-example blocking in every mode;
- different hash, different suite, and incomplete identities remaining
  unclassified;
- static `eval_leaking` reason retaining precedence;
- same-example candidates excluded from LLM prompts;
- direct injection requiring context and blocking same-example memory;
- legacy injection without source identity remaining unchanged;
- prepare/finalize audit recording candidate IDs, context input hash, and the
  automatic block reason;
- finalized trace identity mismatch failing before request consumption;
- imported usage-log identity validation;
- snapshot, YAML, and PostgreSQL round trips requiring no version or schema
  migration;
- raw input hashes and source identity never appearing in prompts or snippets;
- README workflow, architecture, usage policy, and Phase 11 roadmap claims.

Completion requires focused tests, the full pytest suite, `compileall`,
`git diff --check`, a whole-branch review, merge-result tests on `main`, and a
verified push to `origin/main`.
