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
| Immutable MemoryRevision v3 | `memory_revision_v3.py`, `memory_publication_v3.py` | proposal/approval/activation Schemas/examples, lineage/artifact/evidence/authorization/actor/head rejection tests, protocol docs |
| MemoryRevision publication authority v3 | `sqlite_memory_publication_v3.py`, `postgres_memory_publication_v3.py` | SQLite DDL, PostgreSQL install/rollback, attestation/provenance/head-CAS/idempotency/drift/rollback tests, protocol docs |
| Encrypted Artifact authority v3 | `artifact_v3.py`, `artifact_service_v3.py`, `sqlite_artifact_v3.py`, `postgres_artifact_v3.py`, `schemas/sqlite-v3-artifact-authority.sql`, `schemas/postgres-v3-artifact-authority*.sql` | per-operation authorization, provider/AAD, retention/legal hold, immutable ciphertext, SQLite schema/PostgreSQL catalog and rollback drift checks, savepoint/concurrency tests, bilingual protocol docs |
| ActivatedRevision source v3 | `activated_revision_v3.py` | proposal/evidence/publication provenance, trusted verifier identities, authorized artifact read, current-head recheck, SQLite/PostgreSQL composition tests, bilingual protocol docs |
| Replayable retrieval v3 | `retrieval_v3.py` | snapshot Schema/example, rank/hash/version/truncation rejection tests, protocol docs |
| Retrieval preparation v3 | `retrieval_policy_v3.py`, `retrieval_preparation_v3.py` | policy Schema/example, authorization-first discovery, classification/applicability/eval-leakage/ancestry filters, deterministic fusion/System Gate/head-policy recheck tests, bilingual protocol docs |
| Durable retrieval preparation v3 | `durable_retrieval_preparation_v3.py` | one-authorization same-scope composition, durable GateSession creation, exact Gate evidence store/read-back, PREPARED CAS, replay/compensation/shared-transaction tests, bilingual protocol docs |
| Managed retrieval indexes v3 | `managed_index_v3.py`, `sqlite_managed_index_v3.py`, `postgres_managed_index_v3.py` | five-view content-addressed bundle Schema/example, semantic query evidence, SQLite/PostgreSQL exact-byte CAS authorities, install/rollback SQL, bilingual protocol docs |
| Gate evaluation v3 | `gate_evaluation_v3.py` | System/Semantic Gate Schemas/examples, monotonicity/provenance/retry-shape tests, protocol docs |
| Semantic Gate artifact v3 | `semantic_gate_artifact_v3.py` | exact prompt/response byte binding, classification/encryption metadata, strict JSON/schema/example tests, protocol docs |
| Authenticated Semantic Gate service v3 | `semantic_gate_service_v3.py` | provider registration/authentication, trusted timing, exact retry-parent and monotonic Gate verification, atomic artifact-authority append/read-back tests, protocol docs |
| Durable Semantic Gate composition v3 | `durable_semantic_gate_v3.py` | PREPARED→AWAITING_DECISION→DECIDED CAS, complete attempt-chain linkage, provider-failure retry, exact replay/retained-success recovery, shared-transaction SQLite/PostgreSQL tests, bilingual protocol docs |
| UsageDecision v3 | `usage_decision_v3.py` | immutable content-addressed final Gate decision, exact System/Semantic narrowing and block provenance, render/component linkage, Schema/example and parser tests, bilingual protocol docs |
| Durable finalization composition v3 | `durable_finalization_v3.py` | authenticated DECIDED→FINALIZED CAS, deterministic bounded rendering, complete replay-bundle retention/read-back, exact terminal replay, recovery/shared-transaction SQLite/PostgreSQL tests, bilingual protocol docs |
| SQLite Semantic Gate v3 ledger | `sqlite_semantic_gate_v3.py`, `schemas/sqlite-v3-semantic-gate.sql` | Gate evidence linkage, linear head CAS, exact replay, schema-drift/savepoint/concurrency tests, protocol docs |
| SQLite Semantic Gate artifact v3 | `sqlite_semantic_gate_artifact_v3.py`, `schemas/sqlite-v3-semantic-gate-artifacts.sql` | atomic attempt/byte/binding writes, SQL digest/descriptor guards, exact replay, schema-drift/savepoint/concurrency tests, protocol docs |
| PostgreSQL Semantic Gate v3 ledger | `postgres_semantic_gate_v3.py`, `schemas/postgres-v3-semantic-gate*.sql` | row-lock linearization, deferred chain consistency, exact catalog/rollback/savepoint/concurrency tests, protocol docs |
| PostgreSQL Semantic Gate artifact v3 | `postgres_semantic_gate_artifact_v3.py`, `schemas/postgres-v3-semantic-gate-artifacts*.sql` | atomic attempt/byte/binding writes, database digest/descriptor guards, exact catalog/rollback/savepoint/concurrency tests, protocol docs |
| Run outcome and attribution v3 | `outcome_v3.py` | outcome/attribution Schemas/examples, completed-session linkage and causal-boundary tests, protocol docs |
| GateSession completion v3 | `gate_completion_v3.py`, `sqlite_outcome_v3.py`, `postgres_outcome_v3.py`, `schemas/sqlite-v3-outcome.sql`, `schemas/postgres-v3-outcome*.sql` | atomic outcome/session writes, trusted adapter/database time, exact replay/read-back, schema/catalog/rollback/savepoint/concurrency tests, protocol docs |
| SQLite OutcomeAttribution v3 ledger | `sqlite_outcome_attribution_v3.py`, `schemas/sqlite-v3-outcome-attribution.sql` | immutable multi-claim append, exact outcome/session/usage/revision linkage, descriptor/read-back/schema-drift/savepoint/concurrency tests, protocol docs |
| PostgreSQL OutcomeAttribution v3 ledger | `postgres_outcome_attribution_v3.py`, `schemas/postgres-v3-outcome-attribution*.sql` | immutable multi-claim append, exact outcome/session/usage/revision linkage, database hash and row locks, exact catalog/rollback/savepoint/concurrency tests, protocol docs |
| Completion outbox v3 | `completion_outbox_v3.py`, `completion_outbox_worker_v3.py`, `sqlite_completion_outbox_v3.py`, `postgres_completion_outbox_v3.py`, `schemas/sqlite-v3-completion-outbox.sql`, `schemas/postgres-v3-completion-outbox*.sql` | atomic completion/event insert, append-only delivery revisions, bounded at-least-once dispatch, sanitized consumer failures, lease/retry/dead-letter transitions, exact replay, schema/catalog-drift/rollback/savepoint/concurrency tests, protocol docs |
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
| Ordered Trace events v1 | `trace_event_v1.py` | canonical event registry/schema resources, SQLite/PostgreSQL append parity, source/artifact/tool/permission/parent-subagent rejection tests, bilingual protocol docs |
| Git observations v1 | `git_observation_v1.py`, `capture.py` | sealed registry/schema resources, legacy capture return compatibility, Artifact-only diff linkage, SQLite/PostgreSQL append parity, missing/shallow/ancestry rejection tests, bilingual protocol docs |
| SQLite persistence | `sqlite.py`, `schemas/sqlite.sql` | Store conformance, migration docs |
| PostgreSQL persistence | `postgres.py`, `schemas/postgres*.sql` | integration, concurrency, migration tests |
| Installed resources | `resources.py`, `pyproject.toml` | `_resources/`, distribution verifier |
| Operational CLI | `cli.py` | deterministic JSON and exit-code tests |

External protocols must call the same Store/application lifecycle. Search and
vector indexes, when added, remain derived and non-authoritative.
