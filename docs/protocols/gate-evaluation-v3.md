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

The evaluation contracts do not call a model, authenticate the provider, or
attach themselves transactionally to GateSession. The storage-neutral
[Semantic Gate artifact binding](semantic-gate-artifact-v3.md) now verifies
that exact prompt/response bytes match the role-specific attempt digests, but
does not persist those bytes. The opt-in
[SQLite Semantic Gate attempt ledger](sqlite-semantic-gate-v3.md) now persists
an exact ordered retry chain beside the SQLite Gate evidence authority. A
complete service must still:

- authorize and validate the RetrievalSnapshot/System Gate references;
- persist verified prompt/response artifacts under classification,
  encryption, retention, and access-control policy;
- verify provider identity and trusted server timestamps;
- enforce one linear sequence per System Gate authority. The SQLite ledger
  does this with a unique `(system_gate_evaluation_id, sequence)` key and CAS
  head; the low-level parent verifier checks one link, while
  `verify_semantic_gate_attempt_chain()` verifies the complete bounded chain;
- use the equivalent
  [PostgreSQL ledger](postgres-semantic-gate-v3.md) for shared database
  deployments; and
- append GateSession references and replay components atomically.

The active snapshot-v2 Store, SQLite-v1/PostgreSQL-v2 adapters, Agent, and MCP
do not emit these records yet. The side-by-side SQLite ledger does not change
that active compatibility boundary.

The runtime parser rejects oversized strings before UTF-8 encoding and also
enforces cross-field invariants that structural JSON
Schema cannot express: unique System decisions, sorted/disjoint final sets,
timestamp ordering, content-derived IDs, and exact cross-record linkage.
