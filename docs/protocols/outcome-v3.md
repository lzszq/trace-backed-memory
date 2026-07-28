# Run Outcome and Attribution v3

**English** | [简体中文](outcome-v3.zh-CN.md)

`RunOutcome` records the immutable measured result of one completed
`GateSession`. It binds the session, trace, run, and usage decision to an
evaluator version, execution-output digests, at least one evidence artifact,
bounded execution measurements, and a canonical measurement time. Its
`run_outcome_id` is derived from the complete payload.

`OutcomeAttribution` is a separate content-addressed record. An
`association` says only that listed memory revisions were present in the run;
it must use `runtime_observation`, may retain an `unknown` effect, and cannot
name a verifier. A `causal` claim requires a controlled experiment, manual
review, or external evaluation, a known effect, and a verifier distinct from
the evaluator.

The runtime verifiers bind outcomes to completed GateSessions, require
measurement no earlier than session completion, bind attributions to the exact
outcome and completed session, require every attributed revision to be among
the session's finalized revisions, and reject time reversal. Observed association
must never be promoted to causation by an adapter, metric, or migration.
Existing version-2 `Trace.eval_result` and `MemoryUsageLog` outcome fields
remain supported; they do not become complete v3 records without an explicit
mapping and evidence.

The JSON Schemas are structural preflight, not complete verification: they
cannot recompute content IDs, enforce canonical array ordering, authenticate
identities, or perform cross-record checks. Consumers must use the runtime
parser and verifiers. Numeric builder inputs are normalized to JSON floats
before hashing. The content hashes detect canonical-payload changes; they are
not signatures.
A service must authenticate evaluators and verifiers, validate referenced
artifact bytes, use a trusted time source, enforce immutable uniqueness, and
write the outcome, session transition, and any attribution atomically. Raw
tool output and secrets belong in controlled artifacts, not these records.

The opt-in SQLite completion authority now writes one RunOutcome and the
matching `EXECUTING` to `COMPLETED` GateSession revision atomically with one
trusted timestamp, exact replay, schema guards, and pre-commit read-back. See
[SQLite RunOutcome completion v3](sqlite-outcome-v3.md). PostgreSQL parity,
attribution persistence, evaluator authentication, artifact verification,
outbox delivery, and active runtime integration remain outstanding.

Canonical schemas:

- `schemas/run_outcome_v3.schema.json`
- `schemas/outcome_attribution_v3.schema.json`
