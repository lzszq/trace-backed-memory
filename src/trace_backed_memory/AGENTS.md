# Package implementation guide

This directory implements the trusted runtime kernel and its adapters.

- Read the exact code you will modify; do not infer behavior from the README.
- Keep domain and policy decisions in the existing kernel, not in CLI, agent,
  MCP, or persistence adapters.
- Preserve the sequence: retrieve -> System Gate -> semantic narrowing ->
  stale-state recheck -> render -> usage audit -> measured completion.
- Never expose `MemoryGateRequest._store_token` or reconstruct a pending request
  from caller-controlled fields.
- Persistence adapters load and sync validated Store state. They may enforce
  stronger database constraints but may not weaken Store invariants.
- Version-3 migration bundles and staging repositories are inert preparation
  records. They may not activate memory or change the active snapshot,
  SQLite, or PostgreSQL compatibility versions.
- The version-3 GateSession domain record is persistence-neutral. Its opt-in
  side-by-side SQLite and isolated PostgreSQL repositories store revisions but
  are not wired to the active Agent/MCP; do not claim distributed durable
  runtime until expiry/recovery workers, service integration, authorization,
  and conformance exist.
- Explicit durable runtime composition binds the GateSession revision-event
  sink exactly once. Every subsequent creation, transition, lease renewal, or
  completion must append and reduce the canonical event before the existing
  revision projection is written in the same transaction. Never bypass this
  sink or present `baseline_imported` as complete native history.
- Explicit durable runtime composition also binds the Gate evidence companion
  and Semantic-attempt sinks exactly once. Keep raw retrieval, prompt,
  response, injection, and replay bytes in Artifact authorities; event and
  reducer state may retain only bounded linkage/descriptors. Ledger replay
  export must remain the existing `tbm.replay-export.v3` and fail closed on
  classification, size, linkage, or digest drift.
- Outcome/Effect event reducers must reproduce exact RunOutcome,
  OutcomeAttribution, and completion-outbox delivery records. Preserve
  at-least-once semantics, and require compensation to be a distinct new
  effect; never infer provider receipts, exactly-once delivery, or
  compensation from a legacy response digest.
- FailureCase event reduction must bind extractor proposals to an exact
  TraceEvent chain and keep them candidate-only. Only an independent accepted
  review plus exact FixEvidence and passing StructuredRegressionEvidence may
  set new-Memory eligibility. A legacy regression boolean is always
  `legacy_unstructured` and never sufficient for new Memory.
- MemoryCatalog event reduction must bind the exact record to the event
  partition, actor, occurrence time, trusted attestation-verifier
  configuration, and activation-event hash. Rebuild durable views only through
  bounded EventLedgerPort scans. A legacy Lesson compatibility projection is
  never eligible to become an ActivatedMemoryHead.
- Active policy registration and activation require exact `policy:create_global`
  and `policy:approve_global` decisions, independent actors, a full trusted
  partition, forward-only predecessor/time checks, and trusted attestation
  verifiers. Candidate/renderer budgets may only stay within kernel hard caps,
  and Semantic Gate remains required; the event projection is opt-in.
- Retrieval-index events must bind all five managed-index versions to one
  source watermark, exact bundle digest, complete classification view, trusted
  embedding provider/model configuration, and independent activation actor.
  Keep the event-selected managed-index adapter read-only; do not add another
  index SQL authority or treat discovery as authorization.
- Outcome/harm projection must keep association distinct from verified
  causality, require exact usage/replay/retrieval/injection context before an
  attribution enters derived views, and require explicit experiment cohorts.
  Harm may produce only a read-only suspension recommendation; a separate
  authorized MemoryCatalog command owns any actual state change.
- Artifact retention must record intent before managed-index or KMS effects,
  require an atomic hold-epoch authorization and exact independently verified
  receipts, and recover ambiguous KMS results only through non-mutating
  reconciliation. Old Artifact/index/replay rows remain immutable; a tombstone
  is an erased availability overlay, not a claim of physical deletion.
- Git graph replay must consume a complete access-bound Git observation stream.
  Preserve raw ancestry observations, but require same-capture full-repository
  and present endpoint evidence before emitting a known effective relation.
  Missing/unknown/shallow always fails closed; PR anchors remain source commits,
  and graph confidence is never authorization or ranking.
- `tbmd local` binds the Outcome/Effect projector and uses one outer SQLite
  command transaction. Validate before writes, append before completion or
  delivery projections, rebuild critical views synchronously, and commit only
  after constructing the response. Keep external consumer calls outside that
  transaction; claim and acknowledgement/failure remain separate atomic steps.
- The opt-in durable finalization service may move a verified `DECIDED`
  GateSession to `FINALIZED` only after deterministic rendering and complete
  UsageDecision/replay-bundle read-back. It is not active Agent/MCP behavior,
  and confidential/restricted rendering remains unavailable.
- The opt-in durable execution service may replay that exact retained bundle
  and move `FINALIZED` to `EXECUTING` only with current owner-matched
  transition authorization. Resume and abandonment require exact revisions;
  completion requires a registered authenticated evaluator and the atomic
  outcome/outbox authority. External effects remain idempotent by `run_id`.
  This is not active Agent/MCP behavior.
- The authenticated durable Agent facade is the only adapter-neutral
  composition of the complete v3 lifecycle. It must recover the original
  retrieval scope from retained Gate evidence, append fresh transition
  decisions for every post-prepare GateSession mutation, append and recheck a
  fresh artifact-read decision before session-bound replay export, and reject
  services that do not share one authority graph. Never accept replay
  manifest/artifact IDs from caller input. Do not treat the facade as
  transport authentication or default Agent/MCP wiring.
- The optional durable Agent wire dispatcher must keep identity, provider,
  evaluator, repository resolution, authorization events, and authority
  handles out of request JSON. Content exposure is explicit and fail closed.
  The dispatcher is not a transport authenticator or an active adapter.
- The local STDIO MCP profile is runtime-only. Keep its repository root and
  optional tenant server-owned, preserve bounded strict transport parsing,
  require Git ancestry capture, and expose no curator or activation surface.
- MCP pending requests and replay tombstones remain process-local even when
  durable storage is configured. Never reconstruct private Store tokens after
  restart.
- Gate request IDs are opaque and include a fresh Store-session namespace.
  Preserve restart collision resistance so stale finalize/cancel handles
  cannot target a new request.
- Use stable `TBM_*` codes for new agent-facing errors. Bound and sanitize
  external messages.
- Package-root exports in `__init__.py` are public compatibility commitments.
- Run `python tools/verify.py --fast` after focused tests.
- Run `python tools/verify.py --all` before a cross-language release handoff.

For architecture and invariant details, use the repository skill
`maintain-trace-backed-memory`.
