# Architecture map

| Concern | Source of truth | Required companions |
|---|---|---|
| Domain records | `src/trace_backed_memory/models.py` | lifecycle, Store, schemas, examples |
| Review and activation | `lifecycle.py` | Store, docs, state-transition tests |
| Gate and rendering policy | `policy.py` | Store prepare/finalize, schemas, policy tests |
| Cross-record invariants | `store.py` | snapshot tests, both repositories |
| Agent application façade | `agent.py` | package exports, CLI capabilities, agent schemas |
| Durable-session v3 contract | `gate_session_v3.py` | GateSession Schema/example, transition tests, protocol docs |
| SQLite GateSession v3 repository | `sqlite_gate_session_v3.py`, `schemas/sqlite-v3-gate-session.sql` | append-only/CAS/idempotency/concurrency tests, protocol docs |
| Replay v3 contracts | `replay_v3.py` | injection/manifest Schemas and examples, parser tests, protocol docs |
| Callback orchestration | `execution.py` | recovery/error tests |
| SQLite persistence | `sqlite.py`, `schemas/sqlite.sql` | Store conformance, migration docs |
| PostgreSQL persistence | `postgres.py`, `schemas/postgres*.sql` | integration, concurrency, migration tests |
| Installed resources | `resources.py`, `pyproject.toml` | `_resources/`, distribution verifier |
| Operational CLI | `cli.py` | deterministic JSON and exit-code tests |

External protocols must call the same Store/application lifecycle. Search and
vector indexes, when added, remain derived and non-authoritative.
