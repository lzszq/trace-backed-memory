# SQLite Semantic Gate attempt ledger v3

**English** | [简体中文](sqlite-semantic-gate-v3.zh-CN.md)

This opt-in, side-by-side ledger durably stores the ordered
`SemanticGateAttempt` chain for one immutable `SystemGateEvaluation`. It
depends on the SQLite Gate evidence v3 authority and does not replace active
SQLite schema version 1 or the process-local Agent/MCP gate lifecycle.

## Append contract

`SQLiteSemanticGateV3Repository.store_attempt()` accepts an exact
`SemanticGateAttempt`, reloads its `SystemGateEvaluation` and
`RetrievalSnapshot`, verifies all three records, and then appends the attempt
in one SQLite transaction. The first attempt must have sequence 1 and no
parent. Every later attempt must use the next sequence and the exact current
attempt ID as its parent.

Each System Gate evaluation has one CAS head. The schema enforces:

- one attempt per `(system_gate_evaluation_id, sequence)`;
- a maximum chain length of 100 attempts;
- same-session and same-snapshot scope for the head and every attempt;
- insertion only at the current head;
- exact one-step head advancement; and
- immutable attempts, head identity, and head deletion. Dedicated insert
  conflict guards also prevent `INSERT OR REPLACE` from replacing an existing
  head or attempt when recursive replacement-delete triggers are disabled.

Replaying an already stored attempt with identical canonical content is
idempotent and returns `inserted=False`. A sibling fork, skipped sequence,
different content under the same ID, or attempt that does not extend the
current head fails closed.

## Read and transaction contract

`load_attempt()` and `load_chain()` reparse canonical descriptors, compare
every relational column with its descriptor, reload and verify the gate
evidence, and run the bounded whole-chain verifier. Missing rows, gaps,
tampered heads, malformed descriptors, or cross-record mismatches are errors;
the repository never repairs stored data.

Top-level writes use `BEGIN IMMEDIATE`. When a caller already owns a
transaction, the repository uses a savepoint and leaves the outer transaction
open. Every operation requires foreign keys and recursive triggers and
compares all named SQLite definitions with the packaged canonical schema.

The canonical resource is `schemas/sqlite-v3-semantic-gate.sql`. Install the
Gate evidence schema first. Do not call Python `sqlite3.executescript()` while
the connection contains caller-owned uncommitted work: Python commits that
work before running the script. If direct installation fails, roll back the
script's still-open transaction to remove partial schema objects. Prefer
`SQLiteSemanticGateV3Repository.connect(initialize=True)`, which installs on a
new repository-owned connection.

## Current boundary

This attempt ledger stores provenance descriptors and artifact hashes, not
prompt/response bytes by itself. The opt-in
[SQLite Semantic Gate artifact repository](sqlite-semantic-gate-artifact-v3.md)
now composes it with exact public/internal byte storage in one transaction.
The low-level ledger itself does not authenticate providers, choose trusted
timestamps, or append GateSession revisions. The authenticated coordinator and
explicit durable runtime do, and they append the canonical attempt event before
this ledger projection; default compatibility Store/Agent/MCP paths do not. The
[PostgreSQL peer](postgres-semantic-gate-v3.md) now provides shared-database
persistence parity for attempts and exact Artifact bytes. Sensitive Artifact
storage and full event-sourced projection cutover remain outstanding.

SQLite database administrators remain inside the local trust boundary.
Repository operations reject disabled required PRAGMAs, and the insert
conflict guards continue to block replacement writes if only
`recursive_triggers` is disabled. An administrator with DDL or offline file
authority can still remove and later restore guards while replacing the
entire chain with a different, internally valid canonical chain; this single
database cannot distinguish that rewrite from original history. Detecting a
trusted-administrator or offline file rewrite requires an external signed
audit/checkpoint authority, which this ledger does not provide.
