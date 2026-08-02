# Retrieval index events v1

Status: opt-in F4-06 domain reducer over the shared event-ledger ports. It is
not selected by the default compatibility Agent, MCP, HTTP, SDK, or Store
profiles.

## Contract

`RetrievalIndexManifest` binds one immutable `ManagedIndexBundle` to:

- exactly one content digest for each `metadata`, `lexical`, `semantic`,
  `evidence_graph`, and `git_graph` index;
- the source event watermark and source event digest;
- the exact source catalog and sorted memory-revision IDs;
- retriever, tokenizer, embedding provider/model, and Git-graph versions; and
- the exact managed-index build digest and fresh/stale lifecycle boundary.

The manifest reuses the existing managed-index hash recipes and bundle
validation. It does not implement a second tokenizer, vector normalizer,
evidence graph, Git graph, or ranking path.

Four internal events form one partition-local stream:

- `tbm.index.build_requested`
- `tbm.index.build_completed`
- `tbm.index.activated`
- `tbm.index.marked_stale`

Every event embeds the exact authorization policy, request, decision, and
attestation-verifier identity. Build request/completion require
`memory:create`; activation/stale marking require `memory:activate`.
Activation requires a different principal from the completing builder and an
exact predecessor bundle. Source watermarks and activation time are
forward-only.

## Replay and persistence

`retrieval-index-current` is a deterministic `tbm.reducer.v1` reducer. Its
configuration digest binds both trusted authorization-attestation verifiers
and trusted embedding provider/model pairs. It rebuilds a content-addressed
current head and preserves the exact source-event hash and global position for
each projected lifecycle record.

`append_retrieval_index_records()` and
`rebuild_retrieval_index_from_ledger()` use only `EventLedgerPort`. The same
path is tested with SQLite and PostgreSQL event-ledger implementations. Rebuild
requires the complete public/internal/confidential/restricted classification
view, verifies the retained stream, rejects non-forward pagination cursors,
and double-reads the stream before returning a snapshot bound to the reducer
descriptor and configuration hashes.

`EventManagedIndexRepository` is a read-only selection adapter. It loads only
the fresh event-selected bundle, verifies every manifest field against the
exact immutable bundle, and rechecks the head after the read. Direct
publication through this adapter is rejected. Index presence or similarity is
still discovery evidence, never authorization.

## Boundaries

- Existing isolated SQLite/PostgreSQL managed-index repositories remain an
  opt-in immutable bundle store; they are not an event source of truth.
- No new SQL table, migration, network call, vector provider call, or default
  runtime cutover is introduced here.
- F4-03/F4-04 MemoryCatalog acceptance blockers, F4-07 outcome reducers, and
  F5 default migration/cutover remain separate work.
- Raw Trace bytes never enter the manifest or default prompt memory.

Canonical resources:

- `schemas/retrieval_index_manifest_v1.schema.json`
- `schemas/retrieval_index_event_payload_registry_v1.schema.json`
- `examples/retrieval_index_manifest_v1.example.json`
- `examples/retrieval_index_event_type_registry_v1.example.json`
