# PR Report CLI Design

## Problem

The Store already produces endpoint-aware, ancestry-filtered PR memory reports,
but CI users must write Python glue to load a snapshot, construct two immutable
request objects, discover commit anchors, capture Git ancestry, and serialize
the report. Add one read-only CLI adapter that performs exactly that workflow
without copying matching, provenance, or fail-closed rules out of the Store.

## Command

Expose:

```text
tbm pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON --repo-path REPO_PATH
```

`SNAPSHOT` uses the existing bounded snapshot loader. `CONTEXT_JSON` and
`CHANGE_SET_JSON` use the existing strict, bounded CLI JSON reader: 8 MiB,
100,000 JSON nodes, depth 100, duplicate-key rejection, strict UTF-8, and
non-finite-number rejection. `--repo-path` is required so the Git object
database used for ancestry is explicit. Relative and absolute paths are both
accepted.

The context document is one exact JSON object. It requires `mode`, `repo`, and
`commit_sha`, accepts the remaining `MemoryContext` field names, and rejects
unknown fields. Construction is followed by the existing
`validate_memory_context()` contract, so field types, supported modes, bounded
strings, and the `input_hash`/`eval_suite` relationship are not reimplemented.

The change-set document has this exact shape:

```json
{
  "field_changes": [
    {
      "field_name": "model",
      "old_value": "model-v1",
      "new_value": "model-v2"
    }
  ]
}
```

The top-level object and every entry reject unknown or missing fields.
`field_changes` is a non-empty array with the common 10,000-item input ceiling;
each endpoint is a string or `null`. The adapter converts the array once into
the tuple-backed `PRChangeSet`. The Store remains authoritative for supported
field names, uniqueness, non-blank and bounded endpoint values, old/new
difference, and exact equality between every new value and the post-change
context.

## Orchestration

The command performs this sequence:

1. load and fully validate the snapshot;
2. load and validate the context and change-set documents;
3. call `pr_report_commit_anchors(context, change_set=change_set)`;
4. call `capture_commit_ancestry(context.commit_sha, anchors,
   repo_path=...)` outside the Store lock;
5. call `pr_memory_report()` with the same context, the same immutable change
   set, and the captured evidence;
6. serialize the evidence and report without mutating or saving the Store.

An empty anchor set is valid and requires no Git subprocess. For non-empty
anchors, Git exit 0 records `true`, exit 1 records `false`, and every other
result fails the command. `GIT_NO_LAZY_FETCH=1` keeps capture local. Insert
Git's `--` option terminator before both revision arguments so caller or
snapshot values beginning with `-` cannot become command options.

The CLI intentionally does not expose legacy broad `changed_fields`, accept
caller-authored ancestry evidence, infer repository paths, compare against
`HEAD`, fetch missing objects, or implement a second endpoint matcher.

## Output And Errors

Success emits one canonical JSON value with two keys:

```json
{
  "commit_ancestry": {
    "commit_relations": [["source-commit", true]],
    "current_commit_sha": "pr-head"
  },
  "report": {
    "related_case_ids": ["case-1"],
    "related_case_provenance": [],
    "suggested_regression_tests": [],
    "warnings": []
  }
}
```

Store ordering and sorted ancestry anchors make output deterministic. Input
usage, file, JSON, context, and change-set failures are structured input errors
with exit code 2. Git ancestry capture failures and report-state failures are
structured state errors with exit code 3. Unexpected failures use exit code 1.
Because the command is read-only, it has no `--write`, never calls
`save_json()`, and a stdout failure is not treated as a committed success.

## Compatibility And Non-Goals

This phase adds only a CLI adapter and a Git argument-boundary hardening. It
does not change `MemoryContext`, `PRChangeSet`, `CommitAncestryEvidence`,
`PRMemoryReport`, Store matching, persisted records, or public exports. It adds
no context/change-set JSON Schema or packaged resource. Snapshot version 2,
existing JSON Schemas, active-lessons YAML, packaged resource bytes,
`schemas/postgres.sql`, and PostgreSQL schema version 1 remain unchanged.

## Verification

Tests cover exact JSON shapes, unknown/missing/wrong fields, `null` endpoints,
bounded documents and item counts, Store semantic rejection as input errors,
same-object anchor/report orchestration, true/false ancestry filtering,
deterministic evidence/report output, Git capture failures, no snapshot writes,
module and console entry points, the Git option terminator, documentation, the
full suite, and isolated wheel/source-distribution smoke tests.
