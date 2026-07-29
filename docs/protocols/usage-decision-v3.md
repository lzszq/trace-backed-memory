# UsageDecision v3

**English** | [简体中文](usage-decision-v3.zh-CN.md)

`tbm.usage-decision.v3` is the immutable, content-addressed audit record for
one final memory use decision. It records what retrieval proposed, what the
System Gate allowed or blocked, what the Semantic Gate retained, what the
bounded renderer actually used, and the exact injection artifact produced.

## Content identity

`usage_decision_id` is the SHA-256 identity of the canonical record without
the ID field. The same unsigned canonical JSON bytes are stored as an internal
`ContentAddressedArtifact`; therefore
`usage_decision_artifact_id(usage_decision_id)` deterministically locates the
exact retained decision bytes. Parsing recomputes both relationships.

The canonical external contract and example are
`schemas/usage_decision_v3.schema.json` and
`examples/usage_decision_v3.example.json`. They are also installed as
byte-identical package resources.

## Narrowing and audit

The ordered sets must narrow monotonically:

1. retrieval candidates;
2. System Gate allowed revisions;
3. Semantic Gate allowed revisions; and
4. revisions actually rendered.

The final set can never add a revision removed by an earlier stage.
`blocked_memory_revision_ids` is the exact ordered complement of the final
set. `system_blocked` separately records the exact System Gate block reason and
rule for every revision excluded at that stage, so a later model decision
cannot hide or reopen a deterministic block.

The record also binds tenant-authorized retrieval evidence through its
authorization event, RetrievalSnapshot, System Gate evaluation, successful
Semantic Gate attempt, policy digest, renderer identity/version, Trace, run,
decision, session, risk, reason, and recommended rendering mode.

## Replay linkage

Every UsageDecision contains the fixed eight-component replay map defined by
`tbm.replay.v3`. Its injection component must derive the declared
`injection_artifact_id`. The durable finalization composition retains the
UsageDecision plus the exact retrieval, System Gate, Semantic Gate
prompt/response, ancestry commitment, policy, renderer, and injection bytes in
the replay authority before publishing `FINALIZED`.

An ancestry component is the exact retained reference to the prepared
retrieval context commitment. It does not turn a context hash into a full Git
graph archive; deployments that require independent reconstruction must
retain the referenced Git/index evidence under their retention policy.

## Parsing and trust boundary

External JSON is bounded at 1 MiB, depth 24, and 4,096 nodes. Parsers reject
duplicate keys, invalid UTF-8, non-finite numbers, unknown or missing fields,
invalid timestamps, malformed component sets, noncanonical ordering, and
content-derived identity mismatches.

Hashes prove byte identity, not authorization or truth. A UsageDecision is
trusted only when a service has revalidated the current authorization, active
revision heads, policy, Semantic Gate chain, retained artifacts, and durable
GateSession linkage.

## Integration boundary

The contract and opt-in finalization service are not wired into the active
snapshot-v2 Store, local Agent, STDIO MCP, HTTP, or SDK adapters. The current
plaintext replay authorities accept only `public` or `internal` component
bytes. Confidential or restricted finalization requires a future replay
authority that preserves exact identity through authenticated encryption.
