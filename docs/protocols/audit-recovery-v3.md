# Audit Event and Recovery Action v3

**English** | [简体中文](audit-recovery-v3.zh-CN.md)

`AuditEvent` is a storage-neutral, append-only event envelope. Its
content-derived ID covers the stream, monotonic sequence, exact parent,
tenant/repository/session/run identity, authenticated actor context, bounded
reason code, payload digest, canonical typed references, and server timestamp.
The parent verifier rejects gaps, forks presented as a linear continuation,
cross-stream parents, and time reversal.

`RecoveryAction` records one completed recovery attempt. A memory-run action
must be verified against exact before/after derived `MemoryRunRemediation`
values; it does not replace that Store-owned source of truth. A GateSession
action records the expected immutable session version and is verified against
the permitted before state and exact resulting revision (or unchanged state
on failure). Request fingerprint, requesting principal, and event executor are
also bound to the GateSession. Every recovery action must be referenced by a matching
`recovery_succeeded` or `recovery_failed` AuditEvent.

Neither contract executes recovery, authorizes an actor, authenticates an
identity, or persists itself. The Schemas are structural preflight only.
Services must use runtime self-hash and cross-record verification, derive
identity from authenticated context, enforce stream sequence and request-hash
uniqueness transactionally, authenticate the bound identity slots, use trusted
time, prohibit update/delete/truncate,
and atomically write the action, event, and underlying Store or GateSession
transition. Raw prompts, tool output, secrets, and unbounded errors belong in
controlled artifacts; event payloads contain hashes and identifiers only.

The existing derived `MemoryRunAudit`, `MemoryRunRemediation`, health metrics,
and version-2 usage log remain unchanged. An event ledger is evidence about
operations, not a competing lifecycle or outcome authority.

Canonical schemas:

- `schemas/audit_event_v3.schema.json`
- `schemas/recovery_action_v3.schema.json`
