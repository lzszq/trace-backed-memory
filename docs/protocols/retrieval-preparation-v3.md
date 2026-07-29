# Authenticated retrieval preparation v3

**English** | [简体中文](retrieval-preparation-v3.zh-CN.md)

`AuthenticatedRetrievalPreparationService` is the storage-neutral reference
kernel that turns one authenticated, repository-scoped retrieval request into
a paired `RetrievalSnapshot` and `SystemGateEvaluation`. It composes the
authorization service and `ActivatedRevisionSource`; it does not use or
reinterpret the version-2 Store.

## Policy and input identity

`tbm.retrieval-policy.v3` is a content-addressed policy bundle. It binds:

- allowed data classifications;
- the allowed memory types for every planning, repair, debug, eval, and
  production task mode;
- required Git ancestry, or an explicit reason for disabling it;
- positive metadata, lexical, semantic, and evidence-graph fusion weights;
- the minimum fused score and rendered-payload byte budget; and
- a fail-closed rule that always blocks evaluation-leaking memory.

The canonical Schema and example are
`schemas/retrieval_policy_v3.schema.json` and
`examples/retrieval_policy_v3.example.json`. The runtime parser additionally
enforces canonical order, exact task-mode coverage, finite numbers, strict
bounded JSON, and the content-derived policy ID.

`RetrievalPreparationContext` records the exact tenant, canonical repository,
environment, task mode, commit, supported applicability attributes, the
mandatory suite/case identity for eval mode. Non-eval modes cannot declare
that identity. The snapshot context digest binds those request facts together
with the exact Git-ancestry relations returned by trusted discovery. Raw query
bytes are bounded and used only by the discovery adapter; the durable snapshot
contains only `query_sha256`.

## Preparation order

The service executes this fail-closed order:

1. persist and reload an authorization decision before calling discovery;
2. require the preparation context to match the authorized principal, client,
   tenant, repository, and environment;
3. load one immutable policy bundle;
4. call a trusted `CandidateDiscovery` adapter for a complete, bounded
   candidate set, exact Git-ancestry relations, and the exact index versions
   it consulted; the same immutable policy bundle is passed to discovery;
5. load every discovered candidate through the already-authorized
   `ActivatedRevisionSource.load_authorized` path;
6. reject candidate-hash, authorization-receipt, repository-scope, structured
   evidence, or index-provenance substitution;
7. filter classification, exact applicability attributes, evaluation leakage,
   current evaluation-suite/case overlap, and required Git ancestry;
8. deterministically fuse the selected stage scores, apply the minimum score,
   sort by descending score then revision ID, and enforce top-K and the payload
   byte budget;
9. emit one content-addressed `RetrievalSnapshot`;
10. emit one deterministic `SystemGateEvaluation` covering every ordered hit,
    blocking memory types disallowed for the task mode;
11. recheck every selected publication head and reload the policy; and
12. reject the result if either a head or policy changed before return.

Metadata is a binary eligible-candidate score of `1.0`. Fusion divides the
weighted score sum by the weights of the stages actually present. Every score
used for filtering or ranking must have a recorded immutable index version.
All omission causes are represented by the existing snapshot truncation
reasons. System Gate decisions cannot be overridden by later model logic.

## Adapter contract and boundary

`CandidateDiscovery` is a trusted adapter boundary, not an authorization
authority. It must return the complete set considered by this reference
preparation, at most 1,000 records, with unique memory IDs and candidate
digests. It may report at most one immutable index version per index kind, so
each stage score and ancestry relation has one unambiguous recorded version.
The service includes all returned ancestry relations in the snapshot context
digest and requires the exact `git_graph` version when ancestry is enabled.
If a semantic index participates, the result must also carry query evidence
derived from the exact provider, provider version, vector, and raw-query
digest. The service binds it into the prepared context before revision reads.

The adapter is trusted to derive its scores and relations from those recorded
index bytes; an index content hash is an identity, not a signature or
attestation. `ManagedIndexDiscovery` now provides a bounded local concrete
adapter over one content-addressed five-view bundle with exact SQLite and
PostgreSQL publication-head CAS. It validates the complete immutable inputs
used by its computation, but does not independently sign them. Production
sharding, external FTS/ANN providers, and background index workers remain
outside this reference profile. See
[managed index bundle v3](managed-index-v3.md).

This stage deliberately supports repository-scoped activated revisions only.
The current authorization service resolves one repository permission and does
not provide a tenant-wide discovery authorization. Global or tenant-wide
selection must not be inferred from missing values.

The returned `PreparedRetrievalEvidence` alone is not a completed GateSession.
`DurableRetrievalPreparationService` now provides an opt-in composition layer
that creates the session first, persists and verifies the exact pair, and
CAS-publishes `PREPARED`. The underlying preparation service still does not
perform that lifecycle transition itself. See
[durable retrieval preparation v3](durable-retrieval-preparation-v3.md).
Semantic Gate attempts, `DECIDED -> FINALIZED`, rendering, injection, artifact
retention, and active durable Agent/MCP/HTTP/SDK wiring remain separate work. The
active snapshot-v2 Store and local MCP still do not emit this v3 preparation.
