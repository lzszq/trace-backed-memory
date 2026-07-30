# ADR-0001: v2 compatibility and durable-v3 cutover

**Status:** Accepted
**Date:** 2026-07-30
**简体中文:** [0001-v2-compatibility-durable-v3-cutover.zh-CN.md](0001-v2-compatibility-durable-v3-cutover.zh-CN.md)

## Context

The runnable product uses snapshot 2, SQLite 1, PostgreSQL 2, and
`tbm.agent.v1`. The opt-in v3 authorities and durable lifecycle are more
complete internally, but default MCP, HTTP, CLI, and SDK surfaces do not select
them. Extending both paths would perpetuate incompatible meanings for identity,
evidence, sessions, publication, and retrieval.

## Decision

- Freeze `tbm.agent.v1` as the compatibility protocol; change it only for
  security, corruption, and compatibility defects.
- Keep `tbm.durable-agent-wire.v1` as the durable transport contract. Do not
  introduce `tbm.agent.v2` before the durable transport is stable.
- Initially expose explicit `compat-v2` and `durable-v3` profiles.
- New projects may default to durable-v3 only after restart, migration, and
  cross-adapter exit tests pass. Existing projects never switch implicitly.
- Move v2 to read-only and later removal only through a documented release and
  deprecation window.
- Never dual-write independent v2 and v3 stores without a single atomic
  transaction or an append-only compatibility projection.

## Consequences

Transport and migration work has priority over new standalone v3 capabilities.
Documentation must distinguish `active`, `opt-in`, `contract-only`, and
`planned`. A durable profile is not active merely because direct Python
composition exists.

## Exit evidence

The cutover requires process-kill continuation through prepare, decide,
finalize, execute, complete, outbox delivery, and authorized replay export,
with an explicit compatibility rollback path.
