# Conservative Failure Extraction Design

## Summary

The public evidence-ingestion contract says successful tool data cannot create
a false classifier match or tool-failure symptom. Two extraction shortcuts
violate that contract:

- `_symptom()` labels every named tool call as failed, even when the call has
  no top-level `error` evidence.
- `classify_failure_type()` treats the bare word `required` in any tool error
  as `invalid_tool_argument`, so messages such as `required permission denied`
  are misclassified as schema/argument failures.

Phase 50 makes both rules conservative without changing public signatures,
taxonomy IDs, classifier precedence, or stored shapes.

## Classification Evidence

Keep the existing ordered evidence sources and precedence. Missing-required-
context signals remain ahead of tool-argument detection, and an explicit
`invalid argument` signal remains sufficient wherever it appears in the
structured extraction text.

Replace the bare `required` shortcut with explicit tool-error markers:

- `required argument`
- `required parameter`
- `required field`
- `required property`

These markers cover common schema-validator messages such as `missing required
argument` and `'query' is a required property` while avoiding permission,
authentication, context, and unrelated requirement text. Other failed traces
continue through stale-context, enum, evaluator, prompt-contract, and existing
`eval_result` fallbacks.

## Symptom Evidence

Build a tool-failure symptom from named tool calls only when that same call has
truthy top-level `error` evidence. If no failed call supplies a name, retain the
existing fallback to a named errored tool output. If neither source supplies a
failed tool name, use `Trace.error`; otherwise use the trace-ID fallback.

This preserves stored order, duplicate-name behavior, wording, root-cause
priority, and failure-type output. Successful calls may coexist with a later
Trace-level failure without being blamed for it.

## Compatibility

Only heuristic outputs for previously ambiguous inputs change. Records with
explicit `invalid argument` evidence, named failed calls, named failed outputs,
or existing context/stale/schema/evaluator signals retain their results.

The change adds no model, public API, dependency, CLI command, snapshot field,
JSON Schema, YAML shape, packaged resource, or PostgreSQL DDL. Snapshot version
2 and PostgreSQL schema version 1 remain current.

## Tests

- A successful named tool call plus `Trace.error` uses the trace error symptom.
- A successful named tool call without any error uses the trace-ID fallback.
- A failed call still takes symptom precedence over failed output and trace
  error evidence.
- Permission/authentication messages containing `required` do not classify as
  invalid tool arguments.
- Explicit required argument/parameter/field/property errors do classify as
  invalid tool arguments.
- Existing extraction and full repository tests remain green.
