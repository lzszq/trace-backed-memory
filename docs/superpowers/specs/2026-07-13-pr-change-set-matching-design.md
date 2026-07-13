# PR Change-Set Matching Design

## Summary

The PR report currently accepts only `changed_fields`. Those names control
warnings, while historical cases are still matched against the post-change
`MemoryContext`. A PR that changes `tool_schema_version` from `v1` to `v2`
therefore cannot report a verified failure recorded at the old `v1` endpoint,
and callers cannot distinguish whether a related case matched the old or new
configuration.

This project adds an optional immutable old/new change set. A historical trace
is related only when its changed metadata matches the complete old endpoint or
the complete new endpoint. Unchanged metadata keeps the existing strict
context match. The same endpoint matcher drives ancestry-anchor discovery and
report generation, and report provenance records which endpoint matched.

The existing `changed_fields=[...]` path retains its exact behavior.

## Goals

- Represent PR metadata changes as immutable `(field, old, new)` entries.
- Bind every new endpoint value to the exact post-change `MemoryContext`.
- Match historical traces against the complete old endpoint or complete new
  endpoint, never an arbitrary old/new hybrid.
- Preserve repo, tenant, failure-type, and unchanged-metadata isolation.
- Support added and removed optional metadata through `None` endpoint values.
- Support tool-name changes using captured trace tool calls.
- Report whether each case matched the old endpoint, new endpoint, or both.
- Use identical matching for PR ancestry-anchor discovery and PR reporting.
- Preserve deterministic ordering and fail-closed ancestry completeness.
- Keep snapshots, active-lessons YAML, JSON Schemas, PostgreSQL schema version,
  and repository synchronization unchanged.
- Preserve all existing `changed_fields` callers and outputs.

## Non-goals

- Parsing Git diffs, workflow files, prompt files, or tool schemas.
- Inferring old/new values from repository contents.
- Comparing arbitrary code paths or free-form text.
- Matching partial or mixed configurations when several fields changed.
- Adding `model_family` provenance to `Trace`; exact model-family change-set
  matching remains unavailable because traces do not store it.
- Treating repo or tenant changes as reportable metadata; they remain hard
  isolation boundaries.
- Replacing Git ancestry filtering or either runtime memory gate.
- Persisting change sets or endpoint-match results.
- Changing legacy field-name-only report behavior.

## Alternatives Considered

### 1. Immutable complete-endpoint matching (selected)

Use one frozen change-set value in both anchor discovery and reporting. Match a
trace only if all changed fields equal their old values or all equal their new
values. This models the two real PR configurations, prevents mixed-state false
positives, is deterministic, and gives ancestry discovery the exact same
candidate boundary as report generation.

### 2. Per-field old/new union

Accept a mapping and require each historical field to be either its old or new
value. This is compact, but two changed fields create four accepted
combinations even though only two are real endpoints. Those hybrid matches
produce noisy PR warnings and misleading regression suggestions.

### 3. Keep field-name-only matching

Continue using post-change context matching and changed names only for warning
text. This is backward compatible but cannot surface old-endpoint failures and
does not implement the roadmap follow-up.

## Public Models

Add this ephemeral model to `models.py` and re-export it from the package root:

```python
@dataclass(frozen=True)
class PRChangeSet:
    field_changes: tuple[tuple[str, str | None, str | None], ...]
```

Each entry is `(field_name, old_value, new_value)`. The outer and inner tuples
make the caller-owned value immutable and reusable across anchor discovery and
report generation. Direct dataclass construction is not trusted; every store
boundary validates the complete value before scanning records.

Extend ephemeral report provenance:

```python
PRChangeEndpoint = Literal["old", "new", "both"]


@dataclass(frozen=True)
class PRCaseProvenance:
    ...
    matched_change_endpoint: PRChangeEndpoint | None = None
```

Legacy reports leave `matched_change_endpoint` as `None`. A trace can match
`both` when a tool-name-only change names two tools and the historical trace
invoked both tools. Report and provenance models are not persisted.
`PRChangeEndpoint` is re-exported with `PRChangeSet` for type-aware callers.

## Supported Fields

Value-aware change sets support only fields with exact historical trace
provenance:

- `prompt_version`
- `prompt_family`
- `tool`
- `tool_schema_version`
- `model`
- `eval_suite`

`tool` is matched against the set of non-empty `Trace.tool_calls[*].name`
values. The other fields are matched against their direct optional trace
attributes.

Legacy `changed_fields` remains permissive and keeps its current warning
behavior, including `model_family`. A `PRChangeSet` containing
`model_family`, `repo`, `tenant`, `branch`, `commit_sha`, or any unknown field
is rejected rather than claiming exact provenance that does not exist or
weakening isolation.

## Validation And Normalization

The store validates a change set before scanning records:

- the value must be an exact `PRChangeSet` instance;
- `field_changes` must be an exact tuple and must not be empty;
- every entry must be an exact three-item tuple;
- every field name must be one of the supported names;
- duplicate field names are rejected;
- each endpoint value must be `None` or a non-empty, non-whitespace string no
  longer than `METADATA_VALUE_MAX_CHARS`;
- old and new values must differ exactly;
- each new value must exactly equal the corresponding value in the supplied
  post-change `MemoryContext`, including `None`.

Entry order is not semantically significant. After complete validation, the
store normalizes entries by field name for deterministic matching, warning
order, and error messages. Validation never mutates the public value.

Context binding prevents a stale or unrelated change set from being reused
with another post-change context. Standard `MemoryContext` validation still
runs first.

## Endpoint Matching

The common PR matcher first applies the existing hard and unchanged-context
conditions:

1. the case is verified and regression-backed;
2. the source trace exists;
3. trace repo exactly equals context repo;
4. trace tenant exactly equals context tenant;
5. an explicit context failure type matches the case;
6. every declared, unchanged trace-backed context field matches exactly;
7. an unchanged explicit context tool appears in trace tool calls.

It then evaluates changed fields twice:

- **old match:** every changed field matches its old endpoint value;
- **new match:** every changed field matches its new endpoint value.

The record is excluded if neither endpoint matches. It is tagged `old`, `new`,
or `both` otherwise. Matching is endpoint-wide, so a trace with the old prompt
version and new model does not match a change set that changed both fields.

For ordinary optional trace fields, expected `None` means the stored attribute
must be `None`. For `tool`, expected `None` means the trace has no named tool
calls; a string means that exact tool name is present.

The existing no-change-set matcher remains untouched in observable behavior.

## Store APIs

Extend anchor discovery:

```python
def pr_report_commit_anchors(
    self,
    context: MemoryContext,
    *,
    change_set: PRChangeSet | None = None,
) -> tuple[str, ...]: ...
```

Omitting `change_set` preserves current context matching. Supplying one uses
complete endpoint matching before returning sorted unique source commits.

Extend report generation:

```python
def pr_memory_report(
    self,
    context: MemoryContext,
    *,
    changed_fields: list[str] | None = None,
    change_set: PRChangeSet | None = None,
    commit_ancestry: CommitAncestryEvidence | None = None,
) -> PRMemoryReport: ...
```

Exactly one report-change input must be supplied:

- `changed_fields` uses the existing validation, matching, warning order, and
  provenance with a `None` endpoint;
- `change_set` uses validated endpoint matching and derives sorted warning
  field names from its entries.

An empty legacy `changed_fields` list remains valid because it is valid today.
An empty `PRChangeSet` is invalid because it defines no endpoints.

Both methods validate context and change-set inputs before scanning records.

## Ancestry Integration

Callers using value-aware matching discover anchors with the same immutable
change set they will pass to `pr_memory_report()`. The report validates and
matches endpoints first, then requires ancestry relations for every matched
case source commit, then excludes false ancestry relations.

This preserves fail-closed race behavior:

- a newly added endpoint-matched case after discovery introduces a missing
  anchor and rejects the report;
- a removed case leaves harmless extra evidence;
- a different or stale change set cannot bypass context binding or ancestry
  completeness.

False ancestry removes the case from IDs, suggestions, warnings, and
provenance exactly as it does today.

## Report Construction

After optional ancestry filtering, records remain sorted by case ID. The
report keeps current deterministic suggestion and warning generation.

For the legacy path, caller `changed_fields` order remains authoritative and
`_unique()` preserves existing output. For the change-set path, normalized
field-name order makes warning output independent of entry order.

Every related provenance record includes its endpoint tag. No old/new metadata
value is copied into warning text or persisted output; the caller already owns
the change set and provenance only explains which endpoint caused inclusion.

## Persistence Compatibility

No persisted contract changes:

- snapshot version remains 2;
- trace, failure-case, lesson, policy, and usage-log dataclasses are unchanged;
- JSON Schemas remain unchanged;
- PostgreSQL schema version remains 1;
- `PostgresMemoryRepository` load and sync remain unchanged;
- active-lessons YAML remains unchanged.

`PRChangeSet`, `PRMemoryReport`, and `PRCaseProvenance` are ephemeral report
boundary values. The new optional provenance field is never serialized by the
store.

## Error Handling

Malformed change-set shapes, unsupported fields, duplicate fields, invalid
endpoint values, identical endpoints, context-binding mismatches, and invalid
report-input combinations raise stable `ValueError` messages before record
scanning or ancestry completeness checks.

Ancestry evidence errors retain their current precedence after valid endpoint
matching. No validation failure mutates store state.

## Testing

Implementation follows red-green-refactor. Focused tests cover:

- immutable public model and package export;
- exact tuple shapes, supported fields, duplicates, value types, whitespace,
  length bounds, identical endpoints, and empty change sets;
- new-endpoint binding to context, including `None`;
- complete old and new endpoint matches;
- mixed old/new configurations being excluded;
- optional metadata addition and removal;
- tool old/new matching, unnamed tools, and `both` endpoint provenance;
- unchanged context metadata, repo, tenant, failure type, and tool isolation;
- input-order-independent results and case-ID ordering;
- endpoint-aware anchor discovery and ancestry completeness/filtering;
- endpoint filtering consistently affecting IDs, suggestions, warnings, and
  provenance, with the endpoint tag recorded on provenance;
- invalid inputs failing before scans on an empty store;
- exactly one of `changed_fields` or `change_set` being required;
- every existing `changed_fields` report and anchor call remaining unchanged;
- snapshot, PostgreSQL, schema, and YAML round trips remaining unchanged;
- executable README workflow and aligned architecture, policy, and roadmap
  claims.

Completion requires focused tests, the full pytest suite, `compileall`,
`git diff --check`, a whole-branch review, merge-result tests on `main`, and a
verified push to `origin/main`.
