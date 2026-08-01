# Reducer and Projection Runtime v1

**English** | [简体中文](reducer-v1.zh-CN.md)

`tbm.reducer.v1` is the opt-in, storage-neutral execution contract for
versioned deterministic reducers and rebuildable projections. It is available
through the package API and through explicit `tbmd ledger ...` / `tbmd
projection ...` operator commands. It is not selected by the default Agent,
durable lifecycle, HTTP, MCP, or SDK composition, so the current persistence
model remains `authority_graph` and `full_persistence=false`.

## Reducer descriptor and registry

Every reducer has an immutable `ReducerDescriptor` binding:

- reducer ID and positive version;
- sorted input event types, or the explicit envelope-only wildcard;
- output projection name and schema version;
- reducer code and configuration SHA-256 digests;
- optional typed-event target versions;
- `deterministic=true`.

`ReducerRegistry` rejects duplicate reducer versions and projection/version
ownership, becomes immutable after `seal()`, resolves an explicit or latest
version, and verifies expected code/configuration digests. Its catalog has a
canonical registry digest. Typed reducers consume events through a sealed
`EventTypeRegistry`; unknown types, unknown versions, or missing upcasters
block a rebuild. The built-in `canonical-event-inventory` reducer is an
envelope-only diagnostic projection: it counts canonical event types without
reading payload content.

## Pure deterministic execution

Projection state is bounded canonical JSON with an object root, signed 64-bit
integers, strings, booleans, nulls, arrays, and objects. Floating-point values,
non-string keys, unsupported objects, excessive depth/nodes, and state over
1 MiB are rejected. Each initial state and transition is executed twice from
separate immutable state views. Different canonical projection digests fail
with a stable nondeterminism error. A reducer receives no clock, filesystem,
network, random source, model, provider, or mutable source event from this
runtime.

The canonical projection digest is domain-separated and binds the projection
name, output schema version, and complete state. It does not contain the
current Python version, operating system, path, locale, clock, or line ending.

## Checkpoints and rebuild

A `ProjectionCheckpoint` binds the exact reducer descriptor, tenant partition,
last scanned global position, ledger high-watermark, canonical state digest,
reducer/event registry digests, owner, creation time, and rebuild generation.
Its content-addressed build ID does not make it a fact source; canonical events
remain immutable and authoritative for this opt-in ledger.

`ProjectionRuntime.rebuild()` requires an all-classification access view for
the selected partition, freezes the first observed ledger high-watermark,
reads bounded global pages in order, and saves periodic plus final
checkpoints. `resume=True` accepts only a checkpoint with the exact reducer,
code, configuration, output schema, and registry digests. A checkpoint ahead
of the ledger or a mismatched descriptor fails closed. In particular, a store
returning a checkpoint for another reducer ID or version is rejected with
`TBM_REDUCER_VERSION_MISMATCH` before any retained state is executed.

A failing event is never skipped. The result is `blocked` and carries
`ProjectionBlocked` evidence with the event and payload digests, reducer
version, projection, last good position, stable error code, retryability, and
required upcaster/migration hint. Repair requires a new reducer, upcaster,
compensating event, or approved migration; it never edits the source event.

## Shadow compare, activation, and rollback

`compare()` checks two builds from the same projection and partition. It binds
the complete state/global-position digests and emits at most 256 path-level
differences containing value digests rather than projection content.

Activation requires explicit approval. The append-only activation chain uses
an expected head version/current build CAS and exact read-back. Replacing an
existing head requires a comparison bound to that active and shadow build.
Rollback appends another head selection pointing at a previously active,
retained build; it never deletes events or checkpoints.

SQLite stores immutable checkpoints and append-only activation records in the
event-ledger schema under the same owner lock and transaction machinery.
PostgreSQL provides the same repository protocol with fixed schema locks,
row-locked head selection, caller savepoints, exact catalog verification, and
fail-closed `RESTRICT` rollback.

## Operator commands

The operator commands target an existing, explicit SQLite event-ledger file.
They do not reuse `.tbm/durable.sqlite3` implicitly. If the database retains
multiple partitions, pass `--partition-sha256`.

```text
tbmd ledger verify --database .tbm/event-ledger.sqlite3
tbmd ledger stats --database .tbm/event-ledger.sqlite3
tbmd projection list --database .tbm/event-ledger.sqlite3
tbmd projection rebuild --database .tbm/event-ledger.sqlite3 --generation 1
tbmd projection compare --database .tbm/event-ledger.sqlite3 ACTIVE SHADOW
tbmd projection activate --database .tbm/event-ledger.sqlite3 SHADOW --approve
tbmd projection rollback --database .tbm/event-ledger.sqlite3 PROJECTION
```

All output is one canonical, deterministic JSON value. The commands hold the
ledger owner lock, discover and validate its exact partition identity, use a
complete classification view, return metadata/digests rather than raw event
payloads, and preserve stable public errors. `projection activate` and
`rollback` accept optional expected-head arguments for an operator precondition
in addition to the storage CAS.

## Cross-platform determinism evidence

`tests/fixtures/event_projection_v1.golden.json` binds the public canonical
event fixture to the inventory reducer descriptor, initial-state digest,
complete projection, and projection digest. The expected digest is:

```text
sha256:9a257f398b55db473403a66d17cafc01983baa50aeb68ca70d69783c0444e9d4
```

`tools/verify_projection_determinism.py` is part of the repository verification
gate. CI runs that same committed golden fixture on Python 3.11, 3.12, and 3.13
on both Windows and Linux. A platform does not generate its own expected value;
all six jobs must match the same bytes.

This F1 capability proves the generic runtime and operator lifecycle. It does
not yet rebuild GateSession, MemoryCatalog, activated heads, retrieval indexes,
outbox, audit, metrics, or PR risk, and it does not make any active product
transport event-first.
