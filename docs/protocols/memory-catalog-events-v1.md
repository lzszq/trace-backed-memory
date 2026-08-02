# MemoryCatalog events v1

`tbm.memory-catalog-event.v1` is the opt-in event and reducer boundary for
Memory revision lifecycle state. It rebuilds `MemoryCatalog` and
`ActivatedMemoryHead` from canonical events without treating a proposal,
ranking result, or compatibility `Lesson` as active memory.

The implementation is in
`trace_backed_memory.memory_catalog_event_v1`. The default compatibility
Store is unchanged; default cutover belongs to F5.

## Event stream

One repository-scoped memory uses one `memory_catalog_<sha256>` stream. The
sealed registry accepts version 1 of:

- `revision_proposed`;
- `revision_reviewed` and `revision_rejected`;
- `fix_evidence_recorded` and `regression_evidence_recorded`;
- `revision_approved` and `revision_activated`;
- `revision_suspended`, `revision_superseded`, and `revision_obsoleted`;
- `relationship_recorded` and `counterexample_recorded`.

Every payload retains canonical JSON plus its content digest. Replay parses the
exact `MemoryRevision`, `FixEvidence`, `StructuredRegressionEvidence`, review,
state-change, relationship, or counterexample contract. Approval and activation
events retain `StoredMemoryRevision*Publication`, including the exact policy,
request, decision, authorization event, and trusted attestation-verifier ID.

The producer binds the record actor and tenant/repository to the authenticated
`LedgerAccessContext`. Replay repeats the record/envelope partition, actor, and
occurrence-time checks, so a caller cannot hide one tenant's record under
another tenant's event or substitute reviewer/provenance identity in a
rehashed envelope. The reducer configuration digest includes the complete
trusted attestation-verifier allowlist.

## Reducer rules

The deterministic reducer enforces:

- contiguous immutable revision lineage;
- independent proposal and review actors;
- exact evidence IDs and a verified fix/regression bundle;
- approval only after an accepted review and not before its timestamp;
- approval evidence digest equality with the replayed evidence set;
- exact stored authorization provenance for approval and activation;
- activation only after approval and by an actor independent of proposer and
  approver;
- one active head, with explicit relationship evidence before supersession;
- forward-only suspension, supersession, and obsolescence;
- failing or errored structured evidence for counterexamples;
- content-addressed head fields for scope applicability, Artifact content,
  evidence bundle, authorization events, attestation verifiers, activation
  time, and the exact activation event hash.

The public replay entry points require a bounded, non-empty trusted-verifier
configuration. Per-stream, aggregate rebuild, and ledger-scan limits fail
closed.

## Durable append and rebuild

`append_memory_catalog_records()` accepts either SQLite or PostgreSQL
`EventLedgerPort`. It reads and verifies the retained stream, builds canonical
events, runs the reducer before append, atomically appends, verifies the append
receipt, reads the durable stream again, and requires byte-equivalent projected
state. Global-position conflicts receive bounded retries; stream conflicts are
not silently merged.

`rebuild_memory_catalog_from_ledger()` freezes the first observed global high
watermark, scans only through that boundary, groups MemoryCatalog events by
stream, and returns a content-addressed `DurableMemoryCatalogSnapshot`. The
snapshot binds the reducer descriptor and trusted-verifier configuration
digests as well as its partition, watermark, source count, and catalog. SQLite
and PostgreSQL use the same code and have focused conformance tests.

## Formal retrieval source

`EventActivatedMemoryHeadSource` implements the existing
`ActivatedRevisionRetrievalSource` protocol. It requires an event-rebuilt head
reader, verifies the head against its source catalog, delegates exact
publication/evidence/Artifact verification to `ActivatedRevisionSource`, then
rechecks the event head. Candidate and head must match revision, approval,
activation, applicability, content, evidence, authorization, trusted verifier,
and activation time.

`LegacyLessonCompatibilityProjection` is explicit and always has
`eligible_for_activated_head=false`. It is not an activated-revision source.

## Boundaries still open

- Durable-rebuild acceptance is currently blocked: the global scan repeats an
  event at a page boundary, and rebuild access that excludes `internal`
  classification can silently produce an empty partial catalog. F4-03/F4-04
  remain uncredited until both cases fail closed and have regression coverage.
- The F4-01/F4-02 FailureCase producer security acceptance remains open, so
  that producer is not yet an accepted upstream source for new MemoryRevision
  events.
- F4-05 supplies the active policy and renderer limit; F4-06 supplies rebuilt
  indexes; F4-07 supplies outcome/harm projections.
- F5 must route the default compatibility Store and transports through the
  explicit compatibility projection and event-derived head. Until then this
  profile is opt-in and `full_persistence=false` remains correct.

Canonical resources:

- `schemas/memory_catalog_event_payload_registry_v1.schema.json`
- `examples/memory_catalog_event_type_registry_v1.example.json`

See also the [Chinese reference](memory-catalog-events-v1.zh-CN.md).
