# Git observation protocol v1

**English** | [简体中文](git-observation-v1.zh-CN.md)

`tbm.git-observation.v1` is the opt-in, storage-neutral Git evidence protocol
for F3-02. It records what a bounded local Git runner observed without treating
repository scope as authorization and without changing the compatibility
`TraceMetadata` or `CommitAncestryEvidence` contracts.

## Observation points

The sealed registry admits exactly seven version-1 observation event types:

| Point | Event type | Persisted evidence |
|---|---|---|
| checkout | `tbm.git.checkout_observed` | path-derived checkout identity, repository name, object format, HEAD, dirty state |
| ref | `tbm.git.ref_observed` | full symbolic ref or explicit detached state, target object |
| commit | `tbm.git.commit_observed` | commit, tree, and ordered parent object IDs |
| diff | `tbm.git.diff_observed` | HEAD-to-index/worktree byte digest, size, and exact protected Artifact descriptor |
| ancestry | `tbm.git.ancestry_observed` | `ancestor`, `not_ancestor`, or `unknown` per anchor |
| object availability | `tbm.git.object_availability_observed` | `present`, `missing`, or `unknown` per complete object ID |
| shallow state | `tbm.git.shallow_state_observed` | `full`, `shallow`, or `unknown` |

Each payload also persists `runner_id`, `runner_version`, `algorithm_id`,
`algorithm_version`, and the observed Git version. The canonical envelope is an
`observation` event with an exact `EventSource`; sequence and stream hash-chain
rules are inherited from `tbm.event.v1` and the access-bound ledger port.

## Capture and compatibility

`capture_trace_metadata()` keeps its original signature, four-command order,
and exact `TraceMetadata` return type. `capture_commit_ancestry()` likewise
keeps its original signature and `CommitAncestryEvidence` return type.

The explicit F3 path adds:

- `capture_trace_metadata_detailed()`, which returns the compatibility
  metadata plus checkout/ref/commit/diff/shallow observation drafts;
- `capture_commit_ancestry_detailed()`, which observes object availability
  before producing ancestry evidence and returns availability/ancestry drafts;
- `capture_and_append_git_observations()`, which captures all seven points and
  appends their events as one bounded, atomic, exactly idempotent ledger batch.

The detailed ancestry result contains no compatibility evidence when any
required object is missing or indeterminate. Its relations are `unknown`; a
missing object is never translated into `not_ancestor`.

## Diff and runner boundary

Raw diff bytes never enter event payloads. The detailed capture requires a
trusted Artifact writer. Its returned descriptor must bind the exact bytes,
use `application/vnd.git.diff`, be available, be classified confidential or
restricted, and name an encryption key. A restricted descriptor promotes its
event to restricted classification. The event stores only that descriptor, its
digest, and its size, and the event verifier independently rechecks the complete
binding rather than trusting draft construction alone.

The Artifact writer runs before ledger append. It therefore must be
content-addressed and exactly idempotent. If event append fails, an unreferenced
protected blob may remain for governed garbage collection, but it is not a Git
observation or source of truth until a committed event references it; no
projection is dual-written at this boundary.

Git subprocesses are bounded to 30 seconds. Metadata output is limited to
64 KiB and diff output to 64 MiB. The detailed diff command disables external
diffs and text conversion. The entire default detailed path, including its
compatibility metadata projection, plus the ancestry runner sets
`GIT_NO_LAZY_FETCH=1`, so a missing local object cannot trigger an implicit
network fetch. The standalone compatibility API keeps its prior environment
behavior. Complete SHA-1 or SHA-256 object IDs are required on the detailed
path. Batch idempotency binds the expected stream head, next global position,
capture time, and every draft.

## Current boundary

This protocol and runtime composition are opt-in. The default compatibility
Agent/MCP/HTTP profiles are not cut over. F3-03 now supplies the separate
opt-in [Git graph reducer and projection v1](git-graph-reducer-v1.md); Codex/App
Server diff notifications may separately enter TraceEvent through the opt-in
[Codex ingestion adapter](codex-ingestion-v1.md), but they do not select this
seven-observation Git protocol. Artifact authorization and the authenticated
ledger access context remain trusted runtime inputs, never request JSON.
