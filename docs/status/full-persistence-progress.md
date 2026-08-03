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
last promoted atom IDs and evidence paths. There is currently no additional
uncommitted atom candidate, and durable compensation is not counted.

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
17 global events, seven stream heads, and all eight registered reducer
projections alongside the Python facade, Python HTTP sync/async SDKs, and
TypeScript HTTP SDK. Provider receipt/reconciliation, PostgreSQL parity, the
remaining crash matrix, complete F2 cross-transport conformance, and durable
compensation remain incomplete. The exact legacy SQLite timestamp trigger is
also repaired atomically on reopen. These are additional evidence and a
corruption repair only, so formal and candidate progress both remain 182/490
(37.14%) with no promoted atom.

The next F3 foundation adds one strict provider-transition event, content-
addressed attempt/invocation/receipt/reconciliation identities,
`effect-queue` reducer version 2, and an authenticated generic-ledger service.
SQLite verifies exact append replay, receipt mismatch rejection, conservative
orphan/unknown recovery, reconciliation-gated retry, and post-commit response
loss; the same storage-neutral path has a PostgreSQL integration test when the
required executables are available. Active semantic/completion callbacks and
provider-specific reconciliation adapters do not yet select it, so no complete
F3 effect atom is promoted. Formal and candidate progress remain 182/490
(37.14%).
