# Unified SQLite v3 bundle

**English** | [简体中文](sqlite-bundle-v3.zh-CN.md)

## Status and boundary

The unified SQLite v3 bundle is an opt-in local durable-storage contract. It
does not change the active snapshot version 2, SQLite schema version 1,
PostgreSQL schema version 2, or `tbm.agent.v1` compatibility boundary. Default
compatibility MCP/HTTP/CLI/SDK profiles do not select it; explicit durable HTTP
and MCP profiles plus the durable SDK clients do.

`schemas/sqlite-v3.components.json` is the ordered component manifest.
`schemas/sqlite-v3.sql` is generated from its 16 non-migration authority
components. `schemas/sqlite-v3-migration.sql` remains isolated staging and is
not part of the runtime bundle. The event-ledger component is installed and
fingerprinted. The durable runtime selects it for event-first GateSession,
Retrieval/System Gate evidence, Semantic attempt, and finalization slices, and
uses ledger metadata with replay-authority bytes for finalized replay export.
It is not yet the sole source for every authority or projection.

## Install and verification

For a new database, `install_sqlite_v3_bundle()` applies one outer
`BEGIN IMMEDIATE` transaction on one caller-supplied connection. The
deterministic order preserves revision before publication, Gate evidence before
Semantic Gate, and GateSession before outcome and completion outbox. It can
install beside the active SQLite v1 tables without changing them.

The bundle records:

- bundle contract and schema versions;
- the exact ordered component-set SHA-256;
- every component version and optional contract version;
- the full controlled SQLite catalog SHA-256.

`verify_sqlite_v3_bundle()` requires foreign keys and recursive triggers,
checks every component metadata row, and fingerprints all controlled tables,
indexes, automatic indexes, and triggers in both `sqlite_master` and
`sqlite_temp_master`. Missing, changed, partial, or extra `v3_*` objects fail
closed. Bundle metadata is immutable.

The application factory uses the same installer and verifier:

```python
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeFactory,
)

runtime = DurableRuntimeFactory(dependencies).open_sqlite(
    ".tbm/durable-v3.sqlite3",
    initialize=True,
)
```

Reopen an installed database with `initialize=False`. Initialization requires
an idle connection; an installation error rolls back the bundle transaction.

## GateSession timestamp compatibility repair

The separately packaged
`schemas/sqlite-v3-gate-session-timestamp-hotfix.sql` is compatibility-repair
input, not a seventeenth bundle component. The public
`apply_sqlite_v3_gate_session_timestamp_hotfix()` boundary requires an idle
connection and either the exact current bundle or the one supported legacy
component set. The legacy path verifies the exact bundle metadata, catalog,
GateSession schema, and variable-precision timestamp trigger after acquiring a
`BEGIN IMMEDIATE` lock. It then replaces the trigger, updates component-set
metadata, verifies the current 16-component bundle, and commits. Any failed
precondition, replacement, read-back, or bundle verification rolls back the
transaction. The current path is an idempotent verify-only operation.

`install_sqlite_v3_bundle()` invokes this repair automatically when it finds an
existing bundle metadata table. Operators should call the Python boundary,
not execute the packaged SQL directly, because exact source-schema verification
is part of the application boundary. Unrelated schema drift fails closed and
is never overwritten. There is no in-place downgrade; restore a pre-upgrade
backup if operational rollback is required.

## Authoring

Component SQL remains the authoring source. After deliberately changing one
of those files, regenerate and verify both generated layers:

```text
python tools/generate_sqlite_v3_bundle.py --refresh
python tools/generate_sqlite_v3_bundle.py --check
python tools/generate_resources.py --refresh
python tools/generate_resources.py --check
```

The generators use no network access. `python tools/verify.py --all` runs both
checks before tests and distribution verification.
