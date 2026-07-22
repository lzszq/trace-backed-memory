# Bounded PR Change Sets Design

## Problem

An exact `PRChangeSet` supports six trace-backed fields and requires every
field name to be unique. A valid change set can therefore contain at most six
entries. The Store currently accepts an arbitrarily long exact tuple, scans
every entry, and then detects duplicates with `list.count()` for each field.
Direct callers can submit a large repeated tuple and force quadratic work even
though the input could never be valid.

The CLI applies a general JSON item budget, but that limit is much larger than
the domain's six-entry maximum and does not protect direct Store callers.

## Validation Order

Define the maximum from `len(PR_CHANGE_SET_FIELDS)` so the supported-field set
and cardinality cannot drift. `_validated_pr_change_set()` keeps context,
record-type, exact tuple, and non-empty checks first. It then rejects more than
six entries before inspecting entry shape, field names, endpoint values, or PR
case records.

Within the accepted cardinality, scan field names once. Maintain one set of
seen names, one set of duplicates, and one set of unsupported names. Preserve
the existing error priority:

1. a non-string field name fails immediately;
2. sorted unique unsupported fields are reported before duplicates;
3. sorted unique duplicate fields are reported next;
4. endpoint and context-binding validation follows;
5. validated entries retain canonical field-name sorting.

The early cardinality failure is intentionally more specific than any entry
error hidden inside an impossible seventh-or-later input.

## Public Behavior

Both `pr_report_commit_anchors()` and `pr_memory_report()` share the same
private validator, so the bound applies identically before either can scan
failure cases. The read-only `tbm pr-report` adapter continues to reuse those
Store interfaces. It maps an oversized change set to its existing structured
input-error path with exit code 2 and does not invoke Git ancestry capture.

One through six valid unique entries remain compatible. Legacy broad
`changed_fields` reporting is a separate interface and is unchanged.

## Compatibility

No public signature, dependency, model, snapshot field, JSON Schema,
active-lessons YAML field, packaged resource, PostgreSQL DDL, or persisted
version changes. `PRChangeSet` and its maximum remain ephemeral report inputs.
Snapshot version remains 2 and PostgreSQL schema version remains 1.

## Verification

Parameterized Store tests cover both public change-set interfaces with seven
malformed sentinel entries and prove cardinality rejection occurs before entry
or case scanning. A maximum-six test covers the exact valid boundary. A CLI
test supplies seven well-shaped entries, asserts input exit code 2, and makes
Git capture fail if invoked. Existing shape, unsupported, duplicate, endpoint,
binding, canonical-order, ancestry, and report tests protect prior semantics.
