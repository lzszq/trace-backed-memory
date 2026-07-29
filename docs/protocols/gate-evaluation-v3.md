# Gate evaluation v3

**English** | [简体中文](gate-evaluation-v3.zh-CN.md)

This protocol publishes two immutable, storage-neutral records:

- `tbm.system-gate-evaluation.v3` records the deterministic decision for every
  ordered retrieval hit, including candidate hash, allow/block outcome,
  reason/rule, exact authorization event, policy bundle, and evaluator version.
- `tbm.semantic-gate-attempt.v3` records one ordered model attempt, including
  provider/model/endpoint provenance, prompt/response artifact hashes, prompt
  template and generation-config identities, provider request, status,
  latency/token counts, result, and failure code.

Both records have canonical content-derived identities and bounded strict JSON.
They store hashes rather than raw prompt/response content. Those hashes are
content identities, not signatures or authentication.

## Monotonic gate rule

`verify_system_gate_evaluation()` requires the evaluation to cover the exact
ordered retrieval revisions and candidate hashes under the same session,
authorization event, and snapshot, after retrieval completed.

`verify_semantic_gate_attempt()` requires the same session/snapshot/System Gate
identity and chronological order. A successful semantic result must partition
every System Gate candidate into final allowed/blocked sets. Final allowed IDs
must be a subset of System Gate allowed IDs, and every deterministic block must
remain blocked. Every omitted System-allowed candidate must be placed in the
final blocked set; incomplete partitions are rejected. A failed attempt records
provenance and an error only; it cannot produce a decision.

This enforces the permanent rule that a model can narrow deterministic policy
but can never reopen it.

## Trust and persistence boundary

The evaluation contracts themselves do not call a model, authenticate the
provider, or attach themselves to GateSession. The storage-neutral
[Semantic Gate artifact binding](semantic-gate-artifact-v3.md) now verifies
that exact prompt/response bytes match the role-specific attempt digests, but
does not persist those bytes. The opt-in SQLite and PostgreSQL attempt/artifact
authorities persist exact bytes and one bounded linear retry chain. The
[authenticated Semantic Gate service](semantic-gate-service-v3.md) verifies
provider registration, trusted server timing, complete-chain parentage, and
monotonic narrowing. The
[durable composition](durable-semantic-gate-v3.md) verifies the exact
RetrievalSnapshot/System Gate/session linkage and CAS-attaches the complete
attempt chain to `DECIDED`.

The remaining boundary includes tenant authorization for those references;
classification-backed encryption, retention, and artifact access control;
signed provider attestation beyond the trusted internal callback; atomic
cross-authority finalization and replay-manifest linkage; and active
Agent/MCP/HTTP/SDK integration. The SQLite ledger uses a unique
`(system_gate_evaluation_id, sequence)` key and CAS head; the low-level parent
verifier checks one link, while `verify_semantic_gate_attempt_chain()` verifies
the complete bounded chain. Shared deployments use the equivalent
[PostgreSQL ledger](postgres-semantic-gate-v3.md).

The active snapshot-v2 Store, SQLite-v1/PostgreSQL-v2 adapters, Agent, and MCP
do not emit these records yet. The side-by-side SQLite ledger does not change
that active compatibility boundary.

The runtime parser rejects oversized strings before UTF-8 encoding and also
enforces cross-field invariants that structural JSON
Schema cannot express: unique System decisions, sorted/disjoint final sets,
timestamp ordering, content-derived IDs, and exact cross-record linkage.
