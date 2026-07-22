# Linear Legacy PR Warnings Design

## Problem

The legacy `pr_memory_report(..., changed_fields=[...])` interface is
intentionally permissive. It accepts an empty list, duplicate names, unknown
names, and the broad `model_family` warning field. Only seven field names can
produce warnings, but the Store currently crosses every caller-supplied entry
with every related failure case before removing duplicate warning strings.
An arbitrarily long list of repeated or unknown names therefore causes
`O(C * W)` warning work for `C` cases and `W` input entries without changing
the report.

The shared `_unique()` helper also checks a growing result list for every
suggestion and warning. Its stable output is required, but list membership
makes unique output quadratic in the number of generated strings.

## Validation And Normalization

Define the seven legacy warning names once as an immutable set:

- `prompt_version`
- `prompt_family`
- `tool_schema_version`
- `tool`
- `model`
- `model_family`
- `eval_suite`

A private validator scans `changed_fields` exactly once before ancestry or
case scanning. It preserves the existing list and non-empty-string checks,
including validation of entries after all supported names have already been
seen. During that same pass it keeps only the first occurrence of each
supported warning name. Unknown and duplicate strings remain accepted; they
simply cannot increase downstream warning work.

First-occurrence order remains authoritative. This produces the same warning
order as the existing nested comprehension followed by stable `_unique()`.
An empty input remains valid and produces no warnings.

The exact `PRChangeSet` path is unchanged. It continues to use its own six
trace-backed fields, endpoint matching, canonical sorting, and validation.

## Stable Deduplication

Replace `_unique()` list membership with a `seen` set plus an ordered result
list. All current callers supply strings, so hashing preserves exact equality
semantics while reducing stable deduplication from quadratic to linear
expected time. The first occurrence remains in the output.

## Complexity

Let `U` be the number of supported legacy warning fields retained from the
input, where `U <= 7`.

- input validation and normalization is `O(W)` with at most seven retained
  names;
- warning construction is `O(C * U)`, therefore `O(C)` with a domain constant;
- stable suggestion and warning deduplication is expected `O(C)`.

The resulting report path is expected `O(W + C)` for legacy warning work and
does not allocate a caller-sized normalized field collection.

## Public Behavior And Compatibility

No public signature, accepted input, error text, warning text, ordering,
matching, ancestry behavior, provenance, dependency, model, snapshot field,
JSON Schema, active-lessons YAML field, packaged resource, PostgreSQL DDL, or
persisted version changes. Snapshot version remains 2 and PostgreSQL schema
version remains 1.

## Verification

A Store regression test supplies many repeated supported and unknown names to
multiple related cases. It asserts exact legacy warning order and instruments
warning construction so calls are bounded by cases times unique supported
names, not cases times input length. Existing invalid-input tests protect
fail-fast validation before scanning. Focused PR, ancestry, README, full-suite,
distribution, installed-wheel, and independent reviews protect compatibility.
