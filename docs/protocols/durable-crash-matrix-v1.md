# Durable SQLite crash matrix v1

**English** | [简体中文](durable-crash-matrix-v1.zh-CN.md)

F2-08 verifies the explicit event-first SQLite lifecycle against hard process
termination. The harness launches a child process on a real file-backed
SQLite v3 database and calls `os._exit()` at each semantic commit point. The
fault hooks exist only in the test child; production dependencies, public
request JSON, and `tbm.durable-agent-wire.v1` are unchanged.

## Fixed matrix

The eleven commit points are:

1. authorization decision;
2. `CREATED`;
3. retrieval evidence;
4. `PREPARED`;
5. provider call;
6. `DECIDED`;
7. replay retention;
8. `FINALIZED`;
9. `EXECUTING`;
10. outcome;
11. outbox event and initial delivery.

Every point runs in two modes. A pre-commit kill occurs after the selected
write/callback but before the event-first command guard commits. Reopening the
database must pass `PRAGMA integrity_check` and reproduce the complete prior
database snapshot across every managed table. Reissuing the command must then
reach the intended state exactly.

A response-loss kill lets the real command guard commit first and then exits
the child before a response reaches the caller. Reopening must expose the
committed session and event projections. Reissuing the original request with
its original expected version must retain the exact GateSession, event
sequence, event hashes, and aggregate projection digest, with no second
logical transition.

These assertions jointly prove the four F2-08 requirements:

- no acknowledged canonical event is lost;
- an exact retry creates no duplicate logical transition;
- recovery uses the retained identities, bytes, versions, and digests;
- each crash is either the exact prior prefix or the exact committed result,
  never an unknown partial database state.

## Provider and outbox boundaries

The durable wire receives exact provider response bytes from a trusted adapter.
A pre-commit kill after that callback may cause the callback to run again, but
it leaves no persisted duplicate attempt or decision. A retained successful
decision is replayed without invoking the provider again. This is not a
provider exactly-once guarantee.

Completion atomically includes the RunOutcome, completed GateSession,
Outcome/Effect events, outbox event, and initial pending delivery. External
consumer calls remain outside the database transaction. Delivery is still
at-least-once, uses immutable `event_id` deduplication, and relies on lease
reclamation plus explicit `recovery_required`/`superseded` classification when
an acknowledgement write is uncertain.

## Current qualification

This matrix qualifies the explicit local SQLite event-first composition used
by `tbmd local` and standalone SQLite durable HTTP/MCP/SDK profiles. The
standalone PostgreSQL command coordinator and Outcome/Effect projection have
not made the equivalent cutover or crash-matrix run. F2-08 is complete under
its fixed fifteen acceptance points, but the F2 exit gate and Full Persistence
are not complete: F2-03, PostgreSQL parity, remaining reducer cutovers, and
later release-train gates remain open. `full_persistence` therefore stays
`false`.

Executable evidence is in `tests/test_durable_crash_matrix.py` and
`tests/durable_crash_matrix_child.py`.
