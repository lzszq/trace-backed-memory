# Test guide

- Focused tests are iteration aids; the full suite is the handoff gate.
- Cover success, exact replay, stale state, invalid input, partial failure, and
  atomic rollback for every lifecycle change.
- Keep SQLite and PostgreSQL behavior aligned through conformance assertions.
- PostgreSQL tests may skip locally only when `TBM_REQUIRE_POSTGRES` is absent.
  CI and release qualification must set it to `1`.
- Documentation tests intentionally publish compatibility contracts. Update
  current statements and filenames in lockstep, but preserve explicitly marked
  historical phase baselines.
- Resource tests compare canonical and installed bytes exactly.
- MCP tests must cover strict transport bounds, runtime-only tool exposure,
  fixed Git provenance, and process restart with durable records but no
  durable pending request.
- Restart tests must allocate a new request before presenting a stale handle,
  proving stale finalize/cancel cannot collide with new process-local state.
- Never loosen a test merely to accept a weaker invariant.
