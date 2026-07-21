# PostgreSQL-Compatible Nonblank Strings Design

## Summary

The Store and portable JSON Schemas currently treat a non-empty string as any
string with at least one character. PostgreSQL schema version 1 is stricter for
persisted identities, provenance, selected failure-case fields, memory scopes,
and usage-audit object keys and values: its checks apply `btrim` and reject
empty or ordinary-space-only content. A Store can therefore accept a record or
snapshot containing `"   "` that later fails during
`PostgresMemoryRepository.sync()`.

Phase 49 removes that Store-to-PostgreSQL failure and defines one clearer
portable contract: a covered field must contain at least one non-whitespace
character. Python uses the project's existing `str.strip()` convention and
JSON Schema uses the existing `pattern: "\\S"` convention. This portable
contract is intentionally stricter than PostgreSQL's default `btrim(text)`,
which trims ordinary spaces but not every character Python and JSON Schema
classify as whitespace. Every value accepted through the supported Store and
repository path remains valid for PostgreSQL.

## Field Scope

The nonblank contract applies to:

- Trace `trace_id`, `run_id`, and `commit_sha`;
- Failure Case `case_id`, `source_trace_id`, `commit_sha`, `failure_type`,
  `symptom`, and non-null `fix` / `fix_commit_sha`;
- Lesson `lesson_id`, `source_case_id`, `lesson_text`, and every scope value;
- Project Policy `policy_id`, `policy_text`, and every scope value;
- Memory Context required and optional string values because accepted contexts
  become persisted usage-audit evidence;
- Usage Log `decision_id`, `run_id`, `trace_id`, context keys and values,
  `candidate_memory_statuses` keys, and `system_blocked_reasons` keys and
  values. `reason` is already nonblank.

The phase deliberately does not change optional Trace metadata, Failure Case
`root_cause`, `reviewed_by`, or `review_notes`, or the candidate/used/blocked
memory-ID arrays. PostgreSQL permits whitespace in those fields today, so
tightening them would be an unrelated compatibility change.

## Store Authority and Atomicity

The shared required-string validator becomes whitespace-aware. Its call sites
are identity, linkage, commit, and required domain-text values, so this also
prevents whitespace identifiers from reaching lookup, completion, recovery,
obsolescence, ancestry, or batch staging paths.

The optional-string validator gains an explicit nonblank option used only for
Failure Case `fix` and `fix_commit_sha`. Existing optional Trace metadata and
other narrative fields keep their current non-empty behavior. Lesson/Policy
scope validation, Memory Context validation, and Usage Log mapping/status-key
validation reject whitespace without trimming or normalizing accepted values.

All Store record writers and batch operations already validate candidates
before committing them. Snapshot reconstruction builds a fresh Store, and CLI
snapshot reads complete before publication. A rejection therefore returns no
partially mutated Store or file. Store-originated CLI semantic failures remain
state errors with exit code 3; malformed snapshot input remains input error 2.

## JSON Schema and PostgreSQL

Add `pattern: "\\S"` to the corresponding string properties in the canonical
Trace, Failure Case, Lesson, Project Policy, Memory Context, and Memory Usage
Log Schemas and their installed package copies. Scope and usage-audit mapping
values receive the same pattern; usage object property names receive a
non-whitespace pattern where PostgreSQL already checks their trimmed content.

The snapshot schema continues to reference the record Schemas and does not
change. Memory Decision and its memory-ID arrays do not change. PostgreSQL DDL
and schema version do not change: existing `btrim` checks already reject the
ordinary-space values that caused repository sync failures, while Store
prevalidation now enforces the broader portable contract. Direct SQL can still
write some tab- or Unicode-whitespace-only values that the Store rejects; such
rows are outside the repository write contract and will be rejected when
loaded into a Store. Six canonical/package Schema byte pairs change; the
packaged resource names and count remain 18.

## Compatibility

Previously accepted whitespace-only values in the listed Store, snapshot, or
context fields become invalid. Ordinary-space-only values were never portable
to the supported PostgreSQL backend; other whitespace-only values are a
deliberate tightening of the portable contract. Existing databases populated
through direct SQL should clean such rows before repository load. Accepted
strings are preserved byte-for-byte; the Store does not trim them. Public
signatures, dependencies, models, snapshot shape and version 2, active-lessons
YAML, PostgreSQL DDL, and PostgreSQL schema version 1 remain unchanged.

## Tests

- Direct Store tests reject whitespace across every covered record field,
  scope, context, and usage-audit mapping while leaving existing state intact.
- Snapshot tests exercise all five collections and nested usage fields without
  returning a partial Store.
- Policy/lifecycle tests reject whitespace-only contexts and scopes but retain
  internal spaces and surrounding whitespace around real content.
- Canonical and packaged Schema tests require the exact non-whitespace patterns
  while confirming unchanged Memory Decision arrays and snapshot version 2.
- PostgreSQL contract tests confirm the unchanged DDL rejects ordinary-space
  values on a real cluster and explicitly lock the narrower default-`btrim`
  behavior for tab characters.
- CLI snapshot tests preserve structured input errors and source bytes.
