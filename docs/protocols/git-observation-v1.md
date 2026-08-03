# Git Observation Protocol v1

Simplified Chinese: [git-observation-v1.zh-CN.md](git-observation-v1.zh-CN.md)

## Purpose

`tbm.git-observation.v1` records point-in-time Git and workspace evidence as
typed `tbm.event.v1` observations. Git remains an external evidence provider;
it is not the event-ledger authority, an authorization source, or an eternal
repository truth service.

The protocol reuses the generic SQLite/PostgreSQL event ledger and adds no SQL
schema or compatibility-version bump. Raw diffs, paths, remote URLs, command
output, and error text are never payload fields. Exact diff bytes belong in an
authorized Artifact authority and the event carries only its Artifact ID and
canonical descriptor.

## Event types

The sealed registry contains eight version-1 observation types:

- `tbm.git.checkout_observed`;
- `tbm.git.commit_observed`;
- `tbm.git.ref_observed`;
- `tbm.git.worktree_status_observed`;
- `tbm.git.diff_captured`;
- `tbm.git.commit_relation_observed`;
- `tbm.git.object_availability_observed`;
- `tbm.git.shallow_state_observed`.

Every record binds one canonical repository ID, local checkout alias, Trace/run
identity, ordered sequence, canonical occurrence time, authorization decision,
`EventSource`, Git version, runner name/version, algorithm name/version,
classification, retention policy, and optional causation event. Repository and
tenant scope come from the trusted ledger context, never a Git command payload.

The kind-specific `details` object is strict:

- checkout: current commit, ref or detached state, and optional remote digest;
- commit: exact 40- or 64-hex object ID;
- ref: exact resolved commit plus ref or detached state;
- worktree status: commit, ref/detached, dirty, and optional diff Artifact ID;
- diff: commit, optional base commit, and required exact diff Artifact ID;
- ancestry: ancestor, descendant, and `ancestor`, `not_ancestor`, or `unknown`;
- object availability: object/type, `available`, `unavailable`, or `unknown`,
  plus a bounded reason when it is not available;
- shallow state: explicit boolean and at most 100 unique boundary commits.

Attached ref names use a bounded ASCII Git-ref subset. Absolute paths, URI
syntax, whitespace, control characters, option-like prefixes, hidden or
`.lock` path segments, reflog syntax, traversal, and empty path segments are
rejected. Git, runner, and algorithm versions are similarly restricted to
bounded version tokens rather than arbitrary command output.

`unknown` is never converted to `not_ancestor`. A current Git object database
can change after force-push, fetch, garbage collection, or shallow-boundary
movement, so the original observation remains immutable evidence.

## Identity, batches, and replay

One Git observation stream is derived from the trusted partition, repository,
Trace, run, and checkout alias. Event IDs are partition-scoped. Batches contain
1 through 100 contiguous events, share a partition-scoped identity key, and
share one content-bound command digest over every record, source and Artifact
descriptor, version field, classification, retention, trusted context, and
recorded time.

`build_git_observation_batch()` constructs and verifies the complete parent
chain. `append_git_observation_batch()` is the required typed persistence entry
point: it rejects truncated/mixed batches and mismatched ledger access context
before calling generic atomic append. Raw generic-ledger append methods do not
enforce Git-specific semantics. Exact retries return the retained receipt;
reusing an identity for changed evidence is an idempotency conflict.

## Capture compatibility

`capture_trace_metadata()` still returns the original frozen `TraceMetadata`.
`capture_commit_ancestry()` still returns the original frozen
`CommitAncestryEvidence`, keeps its 1,000-anchor pre-deduplication budget, runs
outside Store locks, uses `GIT_NO_LAZY_FETCH=1`, and accepts only ancestry exit
codes 0 and 1.

Both functions now accept an optional keyword-only `observation_recorder`.
Without it, command order, return types, errors, and persistence behavior are
unchanged. With `GitObservationEventRecorder`, a successful metadata capture
adds checkout/commit/ref/worktree/diff-if-present/shallow observations, and a
successful ancestry capture adds object-availability and relation observations
to the same ordered stream. A failed ancestry probe still raises the existing
capture error and records only `unknown/capture_failed` object evidence; it
never manufactures a false relation.

The recorder requires adapter-authenticated ledger context and explicit Git,
runner, and algorithm versions. It prebuilds the complete record set before
the first write, then respects the ledger's 100-event atomic batch bound. If an
append loses its response after commit, the recorder retains the exact pending
events, parent, positions, and idempotency command. Call `resume_pending()`
before any new capture; do not rerun the capture operation as recovery. The
adapter must also serialize the recorder as the ledger's global-position owner
or reserve the full interval before capture. If another stream consumes a
prebuilt position, exact replay correctly remains in conflict; stop that
recorder and require operator recovery rather than renumbering retained events
or claiming that the logical observation completed.

## Security boundary

- Git observations are evidence, never default prompt memory.
- Checkout aliases are bounded opaque identifiers; absolute paths are absent.
- Remote identities are SHA-256 digests; URLs and credentials are absent.
- Diff bytes and path listings are Artifact content, not ledger metadata.
- Protected diff descriptors require normal encryption-key metadata and an
  event classification at least as restrictive as the Artifact.
- Revision arguments remain bounded and shell-free; ancestry retains the `--`
  option terminator and no-lazy-fetch behavior.
- Source quality, timestamps, Git version, and runner/algorithm versions are
  trusted-adapter evidence claims. Validation binds them but does not
  independently attest their truth.
- Ordering and repository matching are not authorization.

## Current boundary

This increment delivers the typed Git protocol, ledger recorder, strict
registry schemas, capture compatibility seam, and SQLite/PostgreSQL parity
tests. The default compatibility Agent/MCP profile does not configure the
recorder. Automatic Git-version/remote/diff Artifact capture, checkout-binding
authority, GitGraphReducer/projections, force-push reconciliation, Codex
Hook/App Server ingestion, and default cutover remain separate work.
