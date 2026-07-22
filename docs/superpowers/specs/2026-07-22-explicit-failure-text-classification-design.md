# Explicit Failure Text Classification Design

## Summary

Failure classification currently concatenates every top-level `name` and
`error` from `Trace.tool_calls`. A tool identifier can therefore select a
failure taxonomy entry even when it contains no failure evidence. For example,
successful calls named `format_document`, `stale_cache_reader`, or
`enum_lookup` can be classified as prompt-contract, stale-context, or
hallucinated-enum failures.

Phase 69 limits classification evidence to explicit failure text. Tool names
remain available to label a symptom when the same call or output has a truthy
top-level `error`, but a name never selects the failure taxonomy.

## Evidence Contract

`classify_failure_type()` searches these values in stored order:

1. non-empty `Trace.error`;
2. non-null top-level `error` values from `Trace.tool_calls`;
3. non-null top-level `error` values from `Trace.tool_outputs`.

Values retain the existing defensive string conversion. Arbitrary arguments,
results, nested payloads, tool names, and other record fields are not searched.
Classifier keyword precedence and the `eval_result == "fail"` fallback remain
unchanged.

Symptom and root-cause behavior do not change. A named tool call or output with
a truthy top-level `error` can still produce the deterministic `tool call
failed for ...` symptom, and root cause still prefers `Trace.error`, then call
errors, then output errors.

## Compatibility

Only heuristic classifications that depended on a keyword in a tool name
change. Explicit trace, call, and output errors retain their current taxonomy
results, symptoms, and root causes. Public signatures, taxonomy IDs, models,
dependencies, CLI behavior, snapshot version 2, JSON Schemas, the 18 packaged
resources, and PostgreSQL schema version 1 remain unchanged.

This intentionally supersedes the Phase 28 rule that tool-call names
contribute to classification text. Tool names are identifiers and useful
labels, but they are not evidence that the named failure occurred.

## Tests

- Successful tool-call names containing every classifier marker use the
  existing evaluator/unknown fallback.
- Errored tool-call names containing classifier markers do not override an
  unrelated explicit error.
- An errored tool name still labels the drafted failure-case symptom.
- Recognized errors on calls and outputs retain their existing classifications.
- Documentation publishes the explicit-error-only boundary and unchanged
  persistence/schema versions.
