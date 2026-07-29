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
  decisions for every post-prepare GateSession mutation, and reject services
  that do not share one authority graph. Do not treat it as transport
  authentication or default Agent/MCP wiring.
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

For architecture and invariant details, use the repository skill
`maintain-trace-backed-memory`.
