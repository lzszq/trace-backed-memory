# Runtime Cardinality Limits Plan

## Scope

Bound LLM decision ID arrays and Git ancestry anchor iteration before they can
amplify memory, CPU, or child-process work.

## Steps

1. Add exact-boundary and limit-plus-one tests for parsed and direct
   `MemoryDecision` inputs.
2. Extend the private string-list validator with an optional item budget and
   apply the 50-candidate budget only at external decision boundaries.
3. Publish `maxItems: 50` in both canonical and packaged memory-decision
   schemas, retaining byte parity.
4. Add exact-boundary, overflow, duplicate-heavy, bounded-generator, and
   no-runner ancestry tests.
5. Replace eager anchor list materialization with validation that consumes at
   most 1,001 values and exports the fixed 1,000-anchor budget.
6. Update README, architecture, usage policy, product, roadmap, and document
   contract tests for Phase 41.
7. Run focused and full tests, build and verify both distributions, review the
   compatibility surface, merge to `main`, push, and require every CI job.

## Compatibility

- No valid result at or below either new limit changes.
- Oversized input is rejected, never truncated.
- The decision JSON Schema becomes stricter and its packaged copy changes with
  it; the resource inventory remains 18.
- Snapshot version remains 2 and PostgreSQL schema version remains 1.
- No model, PostgreSQL DDL, dependency, or persisted field changes.

