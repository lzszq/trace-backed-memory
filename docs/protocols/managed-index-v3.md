# Managed index bundle v3

Status: delivered as an opt-in, isolated v3 retrieval component. It is not
wired to default compatibility Agent/MCP retrieval. An operator may supply it
to the explicit durable runtime graph.

[简体中文](managed-index-v3.zh-CN.md)

## Purpose

The managed-index bundle replaces caller-owned retrieval scores with one
bounded, content-addressed source of discovery evidence. A bundle contains
five independently versioned views over the same activated-revision catalog:

- metadata scope and classification;
- deterministic Unicode lexical tokens;
- explicit local semantic vectors;
- query-token-to-structured-evidence edges;
- an immutable Git commit DAG and lesson anchors.

`ManagedIndexDiscovery` implements the existing `CandidateDiscovery` port.
`AuthenticatedRetrievalPreparationService` still authorizes first, loads and
verifies the current activated revisions, applies the Retrieval Policy and
System Gate, rechecks heads, and publishes only the final allowed set. Index
matching is discovery evidence; it is not authorization.

`ManagedIndexDiscovery` is a trusted in-process port adapter, not an
authorization authority. Invoke it only through
`AuthenticatedRetrievalPreparationService`, which creates and verifies the
durable authorization decision before discovery. The adapter checks that the
scope is bound to the authenticated principal/client/context, but it does not
load an authorization ledger itself. Calling `discover()` or a managed-index
repository directly never grants access.

## Build contract

`build_managed_index_bundle()` accepts only an exact
`ManagedIndexBuildInput`. Every source is an exact
`ActivatedRevisionCandidate`, and every lesson must already carry structured
fix and regression evidence. The builder:

1. verifies repository scope;
2. derives a canonical source-catalog digest;
3. tokenizes caller-supplied, explicitly redacted index text;
4. normalizes explicit finite semantic vectors without network access;
5. validates evidence references and the complete Git DAG;
6. creates exactly one content-addressed `IndexVersion` for each index kind;
7. derives the bundle ID from the complete canonical descriptor.

Source order does not affect the result. Duplicate or unsorted persisted
records, unknown evidence, unknown Git commits, cycles, mixed semantic
dimensions, non-finite values, and hash mismatches fail closed.

Confidential and restricted candidates cannot carry lexical tokens, semantic
vectors, or content-derived evidence-graph query tokens. Operators must keep
content-derived data outside a managed bundle unless the candidate
classification permits it.

## Query contract

Discovery loads the current bundle for the exact authorized tenant,
repository, and environment. Retriever identity and version must match the
request.

Semantic and semantic-enabled hybrid queries use `SemanticQueryVector`.
Provider identity, provider version, vector dimension, exact vector values,
and the SHA-256 of the original query bytes form
`query_evidence_sha256`. The preparation service binds that digest into the
prepared context hash. A semantic index result without matching query-vector
evidence is rejected before any activated revision is loaded.

Git ancestry is computed locally from the immutable bundle DAG when the
retrieval policy requires it. An absent current commit fails closed. When the
policy explicitly disables ancestry, no ancestry relation is emitted.

## Persistence

Two isolated repositories implement the same publication contract:

- `SQLiteManagedIndexV3Repository`, backed by
  `schemas/sqlite-v3-managed-index.sql`;
- `PostgresManagedIndexV3Repository`, backed by
  `schemas/postgres-v3-managed-index.sql`.

Bundles are immutable exact UTF-8 bytes. One head per tenant/repository/
environment advances by compare-and-swap. Exact publication replay is
idempotent. Stale expected heads, content conflicts, catalog drift, disabled
triggers, function-body changes, and read-back mismatches fail closed.

`purge_managed_index_revisions()` constructs an immutable successor without
the named candidates and without evidence edges no retained candidate uses. It
recomputes every catalog/index digest while preserving Git history. The
Artifact retention coordinator may CAS-publish that successor as the current
head; the prior bundle remains loadable history and is never an authorization
source for current retrieval.

The PostgreSQL install remains isolated beside active schema version 2. Its
rollback verifies the exact relations, columns, constraints, functions,
function bodies, triggers, ACLs, and active-schema precondition before
dropping explicitly enumerated objects.

## Bounds and non-goals

- at most 1,000 complete candidates per bundle;
- at most 4,096 lexical tokens or semantic dimensions per candidate;
- at most 64 scope attributes and 4,096 evidence IDs per candidate;
- at most 50,000 evidence edges and 50,000 Git edges;
- at most 20,000 Git commits;
- at most 64 MiB of canonical bundle JSON.

These limits make the reference implementation replayable and locally
auditable. Production sharding, background index workers, native FTS/ANN
engines, external embedding providers, object-store distribution, and
cross-shard ranking remain future work. The current implementation does not
claim an enterprise-scale managed indexing service.

## Canonical resources

The JSON Schema validates the bounded external shape. The strict runtime
loader remains normative for canonical Unicode tokenization, UTF-8 byte
bounds, sorting, content hashes, graph references, and cross-record
classification rules that JSON Schema cannot express.

- `schemas/managed_index_bundle_v3.schema.json`
- `examples/managed_index_bundle_v3.example.json`
- `schemas/sqlite-v3-managed-index.sql`
- `schemas/postgres-v3-managed-index.sql`
- `schemas/postgres-v3-managed-index-rollback.sql`

The installed copies are byte-identical resources and are verified in wheel,
sdist, editable, SQLite, and PostgreSQL tests.
