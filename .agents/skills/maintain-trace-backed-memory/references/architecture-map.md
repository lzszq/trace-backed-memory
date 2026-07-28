# Architecture map

| Concern | Source of truth | Required companions |
|---|---|---|
| Domain records | `src/trace_backed_memory/models.py` | lifecycle, Store, schemas, examples |
| Review and activation | `lifecycle.py` | Store, docs, state-transition tests |
| Gate and rendering policy | `policy.py` | Store prepare/finalize, schemas, policy tests |
| Cross-record invariants | `store.py` | snapshot tests, both repositories |
| Agent application façade | `agent.py` | package exports, CLI capabilities, agent schemas |
| Authorization v3 contract | `authorization_v3.py` | policy/decision Schemas and examples, evaluator rejection tests, protocol docs |
| SQLite authorization v3 authority | `sqlite_authorization_v3.py`, `schemas/sqlite-v3-authorization.sql` | request/policy verification, immutable decision audit, request uniqueness, schema-drift/savepoint tests |
| PostgreSQL authorization v3 authority | `postgres_authorization_v3.py`, `schemas/postgres-v3-authorization*.sql` | request/policy verification, immutable decision audit, exact catalog/rollback/savepoint/concurrency tests |
| Structured regression evidence v3 | `evidence_v3.py` | evidence Schema/example, content/linkage/self-verification rejection tests, protocol docs |
| Immutable MemoryRevision v3 | `memory_revision_v3.py` | revision Schema/example, lineage/artifact/evidence rejection tests, protocol docs |
| Replayable retrieval v3 | `retrieval_v3.py` | snapshot Schema/example, rank/hash/version/truncation rejection tests, protocol docs |
| Gate evaluation v3 | `gate_evaluation_v3.py` | System/Semantic Gate Schemas/examples, monotonicity/provenance/retry-shape tests, protocol docs |
| SQLite Semantic Gate v3 ledger | `sqlite_semantic_gate_v3.py`, `schemas/sqlite-v3-semantic-gate.sql` | Gate evidence linkage, linear head CAS, exact replay, schema-drift/savepoint/concurrency tests, protocol docs |
| Run outcome and attribution v3 | `outcome_v3.py` | outcome/attribution Schemas/examples, completed-session linkage and causal-boundary tests, protocol docs |
| Audit and recovery v3 | `audit_v3.py` | event/recovery Schemas/examples, parent/linkage/remediation rejection tests, protocol docs |
| SQLite audit v3 ledger | `sqlite_audit_v3.py`, `schemas/sqlite-v3-audit.sql` | append-only stream CAS, recovery/event atomicity, schema-drift/savepoint/concurrency tests, audit protocol docs |
| PostgreSQL audit v3 ledger | `postgres_audit_v3.py`, `schemas/postgres-v3-audit*.sql` | row-lock stream CAS, recovery/event atomicity, exact catalog/rollback/savepoint/concurrency tests, audit protocol docs |
| Durable-session v3 contract | `gate_session_v3.py` | GateSession Schema/example, transition tests, protocol docs |
| SQLite GateSession v3 repository | `sqlite_gate_session_v3.py`, `schemas/sqlite-v3-gate-session.sql` | append-only/CAS/idempotency/concurrency tests, protocol docs |
| PostgreSQL GateSession v3 repository | `postgres_gate_session_v3.py`, `schemas/postgres-v3-gate-session*.sql` | row-lock/database-time/CAS/idempotency/catalog/rollback tests, protocol docs |
| Replay v3 contracts | `replay_v3.py` | injection/manifest Schemas and examples, parser tests, protocol docs |
| SQLite replay v3 ledger | `sqlite_replay_v3.py`, `schemas/sqlite-v3-replay.sql` | exact-byte/idempotency/conflict/schema-drift/savepoint/concurrency tests, replay protocol docs |
| PostgreSQL replay v3 ledger | `postgres_replay_v3.py`, `schemas/postgres-v3-replay*.sql` | exact-byte/idempotency/conflict/schema-drift/savepoint/concurrency/install/rollback tests, replay protocol docs |
| Callback orchestration | `execution.py` | recovery/error tests |
| SQLite persistence | `sqlite.py`, `schemas/sqlite.sql` | Store conformance, migration docs |
| PostgreSQL persistence | `postgres.py`, `schemas/postgres*.sql` | integration, concurrency, migration tests |
| Installed resources | `resources.py`, `pyproject.toml` | `_resources/`, distribution verifier |
| Operational CLI | `cli.py` | deterministic JSON and exit-code tests |

External protocols must call the same Store/application lifecycle. Search and
vector indexes, when added, remain derived and non-authoritative.
