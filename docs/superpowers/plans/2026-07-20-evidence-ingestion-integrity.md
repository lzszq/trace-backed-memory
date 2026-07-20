# Evidence Ingestion Integrity Implementation Plan

## Scope

Implement the accepted Phase 28 design in the existing extraction and Store
YAML adapter boundaries. Do not add dependencies or change persisted formats.

## Steps

1. Add extraction tests for tool-output-only classification, drafted symptom
   and root cause, precedence, and non-error output isolation.
2. Add taxonomy tests that reject a repeated description without changing the
   existing duplicate-ID behavior.
3. Add active-lessons tests that reject duplicate record and scope keys and
   prove the Store remains unchanged when parsing fails.
4. Extend extraction helpers to consume top-level structured evidence from
   tool calls followed by tool outputs while preserving classifier precedence.
5. Add explicit duplicate checks to the two constrained YAML parsers.
6. Update README, architecture, usage policy, product status, and roadmap with
   the strict evidence-ingestion contract and unchanged version boundaries.
7. Run focused tests, the full suite, compile checks, documentation contracts,
   and independent staged-diff review before merging and pushing `main`.

## Acceptance

- No supported valid input changes result.
- Tool-output-only errors produce the same classification quality as equivalent
  tool-call errors.
- Duplicate supported YAML keys cannot overwrite earlier evidence.
- Parsing a duplicate lesson key does not partially mutate the Store.
- Core dependencies remain empty.
- Snapshot version 2 and PostgreSQL schema version 1 remain unchanged.
