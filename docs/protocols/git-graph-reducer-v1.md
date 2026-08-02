# Git graph reducer and projection v1

**English** | [简体中文](git-graph-reducer-v1.zh-CN.md)

`tbm.git-graph.v1` is the opt-in, storage-neutral F3-03 reducer over the seven
canonical Git observation types. It deterministically rebuilds an immutable
`GitGraphProjection`; it does not query Git, read the wall clock, grant
repository authorization, or change the default Agent/MCP/HTTP profiles.

## Replay contract

`reduce_git_graph_events()` requires one complete Git observation stream from
version one, an authenticated ledger access context for the exact organization,
tenant, repository, environment, and visible classifications, and an optional
bounded set of existing `FixEvidence`, `StructuredRegressionEvidence`, and
`PRCaseProvenance` records. Replay verifies every canonical event, sealed typed
payload, parent hash, stream version, trusted partition, classification, and
nondecreasing observation time before executing the versioned deterministic
reducer. It also reconstructs every capture command from the complete event
group and verifies its command/idempotency digests, contiguous positions, and
derived event/source identities; `request_sha256` alone is never trusted as a
capture boundary. The reducer state remains subject to the generic 1 MiB,
32-depth, and 32,768-node limits, plus 10,000 input events, 20,000 commit nodes,
50,000 parent edges, and 1,000 evidence or PR-case inputs.

The projection retains:

- repository partition and observed checkout identity;
- observed commits, placeholder commit IDs referenced by relationships, and
  parent edges;
- raw and effective ancestry status;
- object availability and explicit missing/unknown-object reasons;
- exact runner, algorithm, Git version, source record, event digest, position,
  and observation time for the latest observation;
- independently verified source-to-fix and fix-to-verification relationships;
- sorted, unique PR source-commit anchors with their matched endpoint and case
  provenance; and
- the reducer descriptor digest, latest validated time, and content-addressed
  projection digest.

Repeated replay of the same ordered inputs produces the same projection digest.
Commit contents, object format, checkout identity, trusted scope, and known
ancestry cannot conflict. Duplicate points in one capture request, graph cycles,
sequence gaps, parent-hash drift, and mismatched evidence chains fail closed.
One capture command must occupy one contiguous stream segment and cannot be
reopened after another command.

## Relation confidence

Confidence is an explanatory enum, never a probability, authorization result,
or retrieval ranking input:

| Value | Meaning |
|---|---|
| `independently_verified` | an immutable Fix/Regression evidence relation exists and both endpoints are locally present in a full repository |
| `locally_observed` | a parent or ancestry relationship is supported by the exact Git observation set required for local validation |
| `degraded` | relation evidence is retained, but shallow state or local object availability prevents current revalidation |
| `indeterminate` | the effective ancestry result is `unknown` and cannot be converted to a compatibility boolean |

For ancestry, a known effective status requires the same capture request to
record `full` shallow state and `present` availability for both current and
anchor commits. Otherwise the projection preserves `reported_status` but forces
effective `status=unknown`, `confidence=indeterminate`, and no validation time.
A later missing/unknown-object or shallow observation also downgrades affected
relationships. `missing` is therefore never equivalent to `not_ancestor`.

## Evidence relationships and PR anchors

Supplemental relationship inputs are existing immutable authorities, not new
Git observations. A regression record is accepted only when an exact
`FixEvidence` record matches its case, source trace, source commit, and fix
commit. The projection then records each evidence ID, directed source→fix or
fix→verification edge, verifier, verification time, regression result, and
derived confidence. Complete SHA-1 or SHA-256 IDs must match the observed object
format.

PR anchors use `PRCaseProvenance.commit_sha`, preserving the compatibility
meaning of a PR **source** commit. Fix and verification commits remain separate
provenance and relationship fields. Anchors are sorted and deduplicated by
commit while retaining all unique case IDs, fix commits, and old/new/both/legacy
endpoint tags. Missing ancestry or unavailable objects yields an explicit
unknown anchor. `pr_anchor_commit_ancestry_evidence()` emits compatibility
booleans only when every anchor is locally validated; otherwise it fails closed.

Scope matching and Git ancestry remain applicability evidence, not tenant or
repository authorization. System Gate and Semantic Gate authority are
unchanged.

## Current boundary

The reducer is an opt-in rebuild/read model. It adds no database schema, does
not persist a new authority, and does not select itself in compatibility or
durable transports. The opt-in Codex ingestion adapter does not select this
Git read model. Effect receipts, retention/crypto-erasure completion, active
projection persistence, and the F3 exit gate remain separate work.
