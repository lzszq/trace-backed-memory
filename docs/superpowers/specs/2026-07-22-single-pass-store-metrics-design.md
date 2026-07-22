# Single-Pass Store Metrics Design

## Problem

`TraceBackedMemoryStore.metrics()` currently traverses the complete usage-log
list separately for candidate, used, blocked, obsolete, evaluated-with-memory,
evaluated-without-memory, unevaluated, and wrong-memory counts. It also
materializes two result lists solely to compute pass-rate numerators and
denominators.

The method remains O(u), where `u` is the number of usage logs, but repeated
full traversals and result-list allocation amplify reporting cost for large
stores.

## Design

Use one loop over `_usage_logs` and maintain scalar accumulators for:

- candidate, used, and blocked memory counts;
- obsolete candidate-status entries;
- evaluated-with-memory total and pass counts;
- evaluated-without-memory total and pass counts;
- unevaluated decisions;
- wrong-memory failures.

For each log, classification continues to depend on `used_memory_ids`, not the
requested decision flag. `pass`, `fail`, and `error` remain evaluated results;
`unknown` and `None` remain unevaluated. The wrong-memory counter continues to
count the stored attribution flag independently after record validation has
enforced its legal combinations.

Replace the private result-list rate helper with a count-based helper. A zero
evaluated denominator returns `None`; otherwise Python float division returns
the exact existing pass-count/total-count value, including `0.0` for a nonempty
all-failure cohort.

## Lesson Confidence

Average lesson confidence remains a separate pass over `_lessons`. It is a
different collection and is not part of the usage-log traversal contract. An
empty lesson collection continues to report `0.0`.

## Scope

This phase changes only `metrics()`. `memory_outcome_metrics()` already uses
one usage-log pass, while memory-run audits retain their decision-ID ordering.
The CLI continues to call the three public metrics APIs independently, so its
existing lock boundaries and cross-call consistency semantics do not change.

## Complexity

Usage-log aggregation performs one O(u) traversal with O(1) accumulator space.
Lesson confidence remains O(l), where `l` is the lesson count. No ordering,
sorting, or persisted-data behavior changes.

## Compatibility

No public signature, model field, error, dependency, snapshot field, JSON
Schema, active-lessons YAML, packaged resource, PostgreSQL DDL, or schema
version changes. Snapshot version remains 2 and PostgreSQL schema version
remains 1.
