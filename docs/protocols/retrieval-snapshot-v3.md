# Retrieval snapshot v3

**English** | [简体中文](retrieval-snapshot-v3.zh-CN.md)

`tbm.retrieval-snapshot.v3` is the immutable, storage-neutral record of one
authorized retrieval result. It makes ranking explainable and replayable
without treating similarity as permission, applicability, verification, or a
gate decision.

The snapshot binds:

- the GateSession, request, trace, and run identities;
- the authorization event and exact context/query digests;
- retriever implementation/version and immutable index identities;
- ordered memory revision hits and candidate-content digests;
- per-stage metadata, lexical, semantic, and evidence-graph scores;
- the deterministic fused score and stages contributing to each hit;
- the candidate count, top-K bound, and explicit truncation reasons; and
- a canonical timestamp and content-derived snapshot identity.

`RetrievalHit` ranks are contiguous and unique. Memory revisions and candidate
digests cannot repeat. Index identities are unique and canonically ordered.
All scores are finite JSON numbers with one canonical float representation.
Selected stages exactly match recorded stage scores, every selected stage has
a matching index version, retrieval mode always has matching index provenance
even for zero hits, hybrid hits use at least two ranking stages, and the
candidate count is capped at 1,000,000. Builders normalize only
representational order and timestamps; the immutable snapshot hash detects
semantic changes. These cross-field invariants are enforced by the runtime
parser in addition to the structural JSON Schema.

## Trust boundary

Authorization must occur before retrieval. `authorization_event_id` is a
reference to that decision, not proof that the caller is authentic and not a
replacement for service-side authorization enforcement. Context, query,
candidate, and index hashes are content identities, not signatures.

The snapshot records ranking evidence only. System Gate evaluation and
Semantic Gate attempts remain separate records. Neither semantic similarity
nor a high fused score may reopen a System Gate block, activate memory, or
authorize artifact access.

Exact replay consumes the recorded ordered hits, scores, versions, and hashes;
it does not silently recompute from mutable catalogs or indexes. A future
service repository must verify the referenced authorization event, GateSession,
memory revisions, candidate bytes, index artifacts, access control, retention,
and transaction boundaries before attaching this snapshot to a prepared
session.

The active snapshot-v2 Store, SQLite-v1/PostgreSQL-v2 adapters, local agent, and
MCP runtime do not yet emit this contract. That integration requires explicit
versioned migrations and service orchestration.
