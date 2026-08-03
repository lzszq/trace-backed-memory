# Full Persistence Progress Contract

**English** | [简体中文](full-persistence-progress.zh-CN.md)

The machine-readable source is
[`full-persistence-progress.json`](full-persistence-progress.json). It freezes
the denominator used for every progress report against the desktop execution
plan. A later report may change a numerator only by naming the affected atom
IDs and evidence; it may not replace the denominator with a phase-local or
top-level-bullet count.

## Fixed denominator

The 490 atoms are reconstructed from the complete plan as follows:

| Source | Count |
| --- | ---: |
| F0-F6 release-train bullets | 312 |
| Deduplicated test matrix | 74 |
| Definition of Done | 67 |
| Retention and erasure requirements | 33 |
| Cross-cutting global gates | 4 |
| **Total** | **490** |

The fixed phase denominators are F0 48, F1 90, F2 62, F3 117, F4 38, F5 48,
and F6 87. These counts are classification buckets for the same 490 atoms, not
additional work.

## Reporting rule

Formal progress counts only committed atoms. Candidate progress is always
reported separately and names every uncommitted atom. Unknown, weakly tested,
or contradictory evidence counts as incomplete. Historical formal phase
allocation is not reconstructed from the total because doing so would invent
evidence.

The current formal baseline is 182/490 (37.14%). It comprises the audited
162-atom prior baseline plus the promoted 20-atom F2 event-first tranche:
finalization/replay, GateSession/Gate-evidence/Semantic events and reducers,
RunOutcome and OutcomeAttribution projections, and the local EffectQueue
delivery-history/dead-letter slice. The machine-readable contract retains the
last promoted atom IDs and evidence paths. The uncommitted provider-effect
batch is not mapped to a complete plan atom, so there is currently no additional
candidate atom and its generic compensation slice is not promoted.

A local-daemon child-process hard-restart test now covers acknowledged
`PREPARED`, `DECIDED`, `FINALIZED`, `EXECUTING`, and `COMPLETED` commits with
exact retries and final reducer parity. This is partial F2 crash-matrix evidence,
not a completed atom, so it does not change 182/490.

Additional SQLite `SIGKILL` probes cover committed authorization, `CREATED`,
and Gate-evidence boundaries; rollback inside finalization replay and
completion/outbox transactions; and consumer-return-before-ack durability with
lease reclaim and at-least-once redelivery. Exact `CREATED` recovery performs
fresh authorization without rewriting orphan evidence, and finalization
rebuilds one deterministic claim-time bundle. Post-commit response-loss probes
now cover `DECIDED`, event-first `FINALIZED`, `EXECUTING`, combined
completion/outbox, and committed acknowledgement without duplicate replay or
redelivery. The local happy path also has real JSON-RPC STDIO MCP parity across
21 global events, eight stream heads, and all nine registered reducer
projections alongside the Python facade, Python HTTP sync/async SDKs, and
TypeScript HTTP SDK. The configured explicit SQLite runtime now has Semantic
provider request/attempt/receipt/reconciliation evidence and hard-kill probes
before and after provider boundaries. The current batch adds atomic request-only
claiming, provider/policy-bound requests, one-compensation-per-original
enforcement, receipt-backed generic compensation for supporting contracts, and
cross-transport Semantic invocation parity. Configured reconciliation,
server-attested owner fencing, and bounded retry/dead-letter revalidate their
exact retained evidence. Semantic provider effects do not support compensation
or claim remote exactly-once. Completion-provider integration,
concrete remote adapters, automatic background sweep/lease fencing, shared-
service workers, the locally unexecuted PostgreSQL crash probes, and the
remaining crash matrix stay incomplete. The exact legacy
SQLite timestamp trigger is also repaired atomically on reopen. These are
additional evidence and a corruption repair only, so formal and candidate
progress both remain 182/490 (37.14%) with no promoted atom.

The F3 provider-effect foundation includes one strict provider-transition
event, content-addressed attempt/invocation/receipt/reconciliation identities,
`effect-queue` reducer version 3, and an authenticated generic-ledger service.
SQLite verifies exact append replay, receipt mismatch rejection, conservative
orphan fail-closed behavior, request-only atomic claim, exact owner-fence
attestation, provider-bound recovery, unknown-result reconciliation, retained
retry timing, bounded retry/dead-letter, receipt-backed generic compensation,
and post-commit response loss; the same storage-neutral path has a
PostgreSQL integration test when the
required executables are available. Configured explicit durable runtimes now
select server-owned Semantic invocation; when supplied, trusted reconciliation,
owner-fence verification, and bounded retry/dead-letter validate exact retained
evidence. The receipt binds the complete structured result, fresh same-scope
authorization can reconcile, and all configured transports produce the same
provider-effect sequence. PostgreSQL provider hard-crash tests are present but were skipped on
this machine because PostgreSQL executables are unavailable. Completion-provider
integration, concrete remote-provider adapters, automatic background sweep/
lease fencing, shared-service workers, the remaining crash matrix, and remote
exactly-once remain incomplete. The plan has no explicit safe atom-ID mapping for
this partial F3 cluster, so no complete F3 effect atom is promoted. Formal and
candidate progress remain 182/490 (37.14%) with `atom_ids=[]`.

The ordered Trace event protocol now adds one registered observation family,
strict source/time/Artifact/tool/permission/parent-subagent linkage, and a
ledger-compatible partition identity/content digest for atomic batches of at
most 100 contiguous events. Typed preflight plus SQLite continuation and exact
append/replay are verified, and an exact SQLite/PostgreSQL receipt/page parity
test is present, but PostgreSQL executables are not
available locally. Codex Hook/App Server ingestion, Trace reducers, and default
Trace persistence cutover remain incomplete. The fixed
progress contract contains no auditable F3 atom-ID-to-plan-line map, so this
protocol evidence is not assigned an invented atom ID. Formal and candidate
progress therefore remain 182/490 (37.14%) with `atom_ids=[]`.

The Git observation protocol now adds eight registered checkout, commit, ref,
worktree-status, diff, commit-relation, object-availability, and shallow-state
event types. An opt-in recorder preserves the frozen legacy capture return
types while binding canonical Git/runner/algorithm versions, partition and
checkout identity, exact Artifact-only diff references, and conservative
unknown ancestry into the generic ledger. Focused SQLite append/replay and
legacy capture compatibility are verified, and PostgreSQL parity coverage is
present but was skipped locally because PostgreSQL executables are unavailable.
Raw paths, remote URLs, diff bytes, stdout, and stderr are excluded from event
payloads. Automatic Git/diff capture, checkout authority, force-push
reconciliation, Codex Hook/App Server ingestion, and default cutover remain
incomplete. No complete fixed-plan atom has a safe
atom-ID mapping for this increment, so formal and candidate progress remain
182/490 (37.14%) with `atom_ids=[]`.

The F3 Git graph reducer now consumes all eight typed Git observation events
through the sealed registry and deterministically rebuilds strict-scope commit
nodes, checkout/ref history, pairwise ancestry assertion/confidence summaries,
current missing-object state, and exact last-observation provenance. It keeps
contradictory ancestry `unknown/conflicted`, uses ledger global order rather
than wall-clock order, and leaves direct-parent edges, force-push claims,
source/fix/verification relationships, and PR anchors empty when version-1
events do not prove them. Default-registry checkpoint rotation, focused
determinism/contradiction/scope tests, and public operator resolution are
covered. Active applicability and PR-risk consumers, exact evidence/PR joins,
automatic capture, and default cutover remain incomplete. The fixed contract
still has no safe atom-ID mapping for this partial F3 cluster, so formal and
candidate progress remain 182/490 (37.14%) with `atom_ids=[]`.
