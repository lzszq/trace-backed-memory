# GateSession recovery worker

**English** | [简体中文](gate-recovery-worker-v3.zh-CN.md)

`GateSessionRecoveryWorker` performs one bounded recovery pass over the
unlocked candidate snapshot returned by either SQLite or PostgreSQL
`list_due()`. It never treats discovery as a lock or durable claim.

The worker validates the complete bounded page before mutation, then processes
each candidate once:

- `PREPARED` and `AWAITING_DECISION` attempt an exact-version transition to
  `EXPIRED`. This succeeds only when the session expiry, not merely its lease,
  has been reached.
- lease-only expiry with the same head returns `recovery_required`;
- `DECIDED`, `FINALIZED`, and `EXECUTING` return `recovery_required` because
  the current state graph has no legal due-worker terminal transition;
- a newer current revision returns `superseded` and is not retried blindly.

Every successful expiry receipt preserves all immutable session fields and is
read back exactly. Current-state reads must retain the original session
identity and may not move to an older version. Discovery, read, receipt, and
limit failures use bounded stable errors.

One pass is a sequence of independent CAS operations, not a batch transaction.
An infrastructure failure after earlier candidates were committed may produce
partial progress; the next bounded pass re-discovers current heads. A separate
operator or recovery service must handle `recovery_required` results without
reconstructing process-local Store tokens.
