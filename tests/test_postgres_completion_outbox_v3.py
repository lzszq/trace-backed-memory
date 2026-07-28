from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import pytest

from tests.postgres_support import PostgresCluster


ROOT = Path(__file__).resolve().parents[1]
GATE_INSTALL = ROOT / "schemas" / "postgres-v3-gate-session.sql"
GATE_ROLLBACK = ROOT / "schemas" / "postgres-v3-gate-session-rollback.sql"
OUTCOME_INSTALL = ROOT / "schemas" / "postgres-v3-outcome.sql"
OUTCOME_ROLLBACK = ROOT / "schemas" / "postgres-v3-outcome-rollback.sql"
INSTALL = ROOT / "schemas" / "postgres-v3-completion-outbox.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-completion-outbox-rollback.sql"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for script in (GATE_INSTALL, OUTCOME_INSTALL, INSTALL):
        result = cluster.run_script(script)
        assert result.returncode == 0, result.stderr


def _request(*, suffix: str = "001"):
    import trace_backed_memory as tbm

    return tbm.GateCompletionRequest(
        session_id=f"gate_session_{suffix}",
        expected_version=6,
        result="pass",
        evaluator_id="evaluation_service",
        evaluator_version="1.2.0",
        output_sha256=DIGEST_A,
        evidence_artifact_sha256s=(DIGEST_B,),
        latency_ms=250,
        cost_usd=0.25,
    )


def _executing(repository, *, suffix: str = "001") -> None:
    sessions = repository.outcomes.gate_sessions
    created = sessions.create_or_get(
        session_id=f"gate_session_{suffix}",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id=f"trace_{suffix}",
        run_id=f"run_{suffix}",
        request_fingerprint=DIGEST_A,
        idempotency_key=f"request-{suffix}",
        expires_in_seconds=3600,
    ).session
    prepared = sessions.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=1200,
        retrieval_snapshot_id=f"retrieval_{suffix}",
        system_gate_evaluation_id=f"system_gate_{suffix}",
    )
    awaiting = sessions.transition(
        prepared.session_id,
        "awaiting_decision",
        expected_version=prepared.version,
    )
    decided = sessions.transition(
        awaiting.session_id,
        "decided",
        expected_version=awaiting.version,
        semantic_gate_attempt_ids=(f"semantic_attempt_{suffix}",),
        decision_id=f"decision_{suffix}",
    )
    finalized = sessions.transition(
        decided.session_id,
        "finalized",
        expected_version=decided.version,
        final_memory_revision_ids=(REVISION_A,),
        injection_artifact_id=f"injection_{suffix}",
        usage_decision_id=f"usage_{suffix}",
    )
    executing = sessions.transition(
        finalized.session_id,
        "executing",
        expected_version=finalized.version,
    )
    assert executing.version == 6


def test_postgres_completion_outbox_flow_and_rollback(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        first = repository.complete_session(_request())
        assert first.event_inserted is True
        assert first.completion.inserted is True
        assert first.delivery.status == "pending"
        assert repository.get_event(first.event.event_id) == first.event
        assert repository.get_delivery(first.event.event_id) == first.delivery

        replay = repository.complete_session(_request())
        assert replay.event_inserted is False
        assert replay.event == first.event
        assert replay.delivery == first.delivery

        claim = repository.claim_due(
            worker_id="worker_001",
            lease_seconds=60,
        )
        assert len(claim) == 1
        assert claim[0].event == first.event
        assert claim[0].delivery.status == "leased"
        delivered = repository.acknowledge(
            first.event.event_id,
            expected_version=claim[0].delivery.version,
            worker_id="worker_001",
            response_sha256=DIGEST_B,
        )
        assert delivered.status == "delivered"
        assert len(repository.list_delivery_history(first.event.event_id)) == 3
        replay_after_delivery = repository.complete_session(_request())
        assert replay_after_delivery.event_inserted is False
        assert replay_after_delivery.delivery == delivered

    for script in (ROLLBACK, OUTCOME_ROLLBACK, GATE_ROLLBACK):
        result = postgres_cluster.run_script(script)
        assert result.returncode == 0, result.stderr


def test_postgres_completion_outbox_worker_dispatches_real_claim(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        completed = repository.complete_session(_request())
        seen: list[str] = []
        worker = tbm.CompletionOutboxDeliveryWorker(
            repository,
            lambda event: (
                seen.append(event.event_id)
                or tbm.CompletionOutboxConsumerReceipt(DIGEST_B)
            ),
        )

        result = worker.run_once(
            worker_id="dispatcher_001",
            lease_seconds=60,
        )

        assert seen == [completed.event.event_id]
        assert len(result) == 1
        assert result[0].outcome == "delivered"
        assert result[0].current.response_sha256 == DIGEST_B
        assert repository.get_delivery(
            completed.event.event_id
        ) == result[0].current


def test_postgres_completion_outbox_retry_and_concurrent_claim(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        first = repository.complete_session(_request())

    barrier = __import__("threading").Barrier(2)

    def claim(worker_id: str):
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = tbm.PostgresCompletionOutboxV3Repository(connection)
            barrier.wait()
            return repository.claim_due(
                worker_id=worker_id,
                lease_seconds=60,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(claim, ("worker_a", "worker_b"))
        )
    assert sorted(len(result) for result in results) == [0, 1]
    winner = next(result[0] for result in results if result)

    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        retry = repository.fail_delivery(
            first.event.event_id,
            expected_version=winner.delivery.version,
            worker_id=winner.delivery.worker_id or "",
            error_code="temporary",
            retry_delay_seconds=1,
            max_attempts=2,
        )
        assert retry.status == "retry_wait"


def test_postgres_completion_outbox_input_and_closed_errors():
    import trace_backed_memory as tbm

    assert tbm.POSTGRES_COMPLETION_OUTBOX_V3_SCHEMA_VERSION == 1
    assert tbm.POSTGRES_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE == 1000
    assert (
        tbm.POSTGRES_COMPLETION_OUTBOX_V3_CONTRACT_VERSION
        == "tbm.completion-outbox.v3"
    )
    with pytest.raises(ValueError):
        tbm.PostgresCompletionOutboxV3Repository(None)


def test_postgres_completion_outbox_atomic_failure_and_orphan(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm
    import trace_backed_memory.postgres_completion_outbox_v3 as module

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)

        original = module.PostgresCompletionOutboxV3Repository._insert_bundle

        def reject_bundle(cls, cursor, event, delivery):
            original(cursor, event, delivery)
            raise tbm.PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_TEST",
                "synthetic outbox insert failure",
            )

        monkeypatch.setattr(
            module.PostgresCompletionOutboxV3Repository,
            "_insert_bundle",
            classmethod(reject_bundle),
        )
        with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
            repository.complete_session(_request())
        assert (
            repository.outcomes.gate_sessions.get("gate_session_001").status
            == "executing"
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_outcome.run_outcomes"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM "
                "trace_backed_memory_v3_completion_outbox.events"
            )
            assert cursor.fetchone()[0] == 0

        monkeypatch.setattr(
            module.PostgresCompletionOutboxV3Repository,
            "_insert_bundle",
            original,
        )
        repository.outcomes.complete_session(_request())
        with pytest.raises(
            tbm.PostgresCompletionOutboxV3PersistenceError
        ) as orphan:
            repository.complete_session(_request())
        assert (
            orphan.value.code
            == "TBM_POSTGRES_COMPLETION_OUTBOX_ORPHANED_OUTCOME"
        )


def test_postgres_completion_outbox_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm
    import trace_backed_memory.postgres_completion_outbox_v3 as module

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_state (value integer)")
                cursor.execute("INSERT INTO caller_state VALUES (7)")

            original = (
                module.PostgresCompletionOutboxV3Repository._insert_bundle
            )

            def reject_bundle(cls, cursor, event, delivery):
                original(cursor, event, delivery)
                raise tbm.PostgresCompletionOutboxV3PersistenceError(
                    "TBM_POSTGRES_COMPLETION_OUTBOX_TEST",
                    "synthetic outbox insert failure",
                )

            monkeypatch.setattr(
                module.PostgresCompletionOutboxV3Repository,
                "_insert_bundle",
                classmethod(reject_bundle),
            )
            with pytest.raises(
                tbm.PostgresCompletionOutboxV3PersistenceError
            ):
                repository.complete_session(_request())
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_state")
                assert cursor.fetchone()[0] == 7
            assert (
                repository.outcomes.gate_sessions.get(
                    "gate_session_001"
                ).status
                == "executing"
            )


def test_postgres_completion_outbox_dead_letter_reclaim_and_stale(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        first = repository.complete_session(_request())
        claim = repository.claim_due(
            worker_id="worker_001",
            lease_seconds=1,
        )[0]
        with pytest.raises(tbm.PostgresCompletionOutboxV3ConflictError):
            repository.acknowledge(
                first.event.event_id,
                expected_version=1,
                worker_id="worker_001",
            )
        with pytest.raises(tbm.CompletionOutboxContractError):
            repository.acknowledge(
                first.event.event_id,
                expected_version=claim.delivery.version,
                worker_id="another_worker",
            )

        time.sleep(1.05)
        reclaimed = repository.claim_due(
            worker_id="worker_002",
            lease_seconds=30,
        )[0]
        assert reclaimed.delivery.attempt_count == 2
        dead = repository.fail_delivery(
            first.event.event_id,
            expected_version=reclaimed.delivery.version,
            worker_id="worker_002",
            error_code="permanent",
            retry_delay_seconds=1,
            max_attempts=2,
        )
        assert dead.status == "dead_letter"
        assert repository.claim_due(
            worker_id="worker_003",
            lease_seconds=30,
        ) == ()


def test_postgres_completion_outbox_concurrent_exact_completion(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)

    barrier = __import__("threading").Barrier(2)

    def complete(_value: int):
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = tbm.PostgresCompletionOutboxV3Repository(connection)
            barrier.wait()
            return repository.complete_session(_request())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(complete, (1, 2)))
    assert sorted(result.event_inserted for result in results) == [False, True]
    assert results[0].event == results[1].event


def test_postgres_completion_outbox_immutability_and_schema_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        first = repository.complete_session(_request())
        statements = (
            "UPDATE "
            "trace_backed_memory_v3_completion_outbox.schema_metadata "
            "SET schema_version = 1",
            "DELETE FROM "
            "trace_backed_memory_v3_completion_outbox.schema_metadata",
            "TRUNCATE "
            "trace_backed_memory_v3_completion_outbox.schema_metadata",
            "UPDATE trace_backed_memory_v3_completion_outbox.events "
            "SET event_type = 'execution_completed'",
            "DELETE FROM "
            "trace_backed_memory_v3_completion_outbox.events",
            "TRUNCATE trace_backed_memory_v3_completion_outbox.events",
            "UPDATE "
            "trace_backed_memory_v3_completion_outbox.delivery_revisions "
            "SET attempt_count = attempt_count",
            "DELETE FROM "
            "trace_backed_memory_v3_completion_outbox.delivery_revisions",
            "TRUNCATE "
            "trace_backed_memory_v3_completion_outbox.delivery_revisions",
            "UPDATE trace_backed_memory_v3_completion_outbox.delivery_heads "
            "SET current_version = current_version + 1",
            "DELETE FROM "
            "trace_backed_memory_v3_completion_outbox.delivery_heads",
            "TRUNCATE "
            "trace_backed_memory_v3_completion_outbox.delivery_heads",
        )
        for statement in statements:
            with pytest.raises(psycopg.Error):
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(statement)
        assert repository.get_event(first.event.event_id) == first.event

        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE INDEX unexpected_outbox_index ON "
                "trace_backed_memory_v3_completion_outbox.events (trace_id)"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresCompletionOutboxV3SchemaError):
            repository.get_event(first.event.event_id)
        rollback = postgres_cluster.run_script(ROLLBACK)
        assert rollback.returncode != 0
        assert "catalog mismatch" in rollback.stderr


@pytest.mark.parametrize(
    "drift_sql",
    (
        "ALTER FUNCTION "
        "trace_backed_memory_v3_completion_outbox.validate_event_insert() "
        "SET search_path = public",
        "ALTER TABLE trace_backed_memory_v3_completion_outbox.events "
        "DISABLE TRIGGER completion_outbox_events_validate_insert",
        "GRANT SELECT ON "
        "trace_backed_memory_v3_completion_outbox.events TO PUBLIC",
        "CREATE POLICY unexpected_outbox_policy ON "
        "trace_backed_memory_v3_completion_outbox.events USING (true)",
    ),
)
def test_postgres_completion_outbox_catalog_drift_matrix(
    postgres_cluster: PostgresCluster,
    drift_sql: str,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        with pytest.raises(tbm.PostgresCompletionOutboxV3SchemaError):
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(drift_sql)
                repository.get_event(
                    "completion_outbox_event_sha256_" + "0" * 64
                )
        with pytest.raises(tbm.PostgresCompletionOutboxV3NotFoundError):
            repository.get_event(
                "completion_outbox_event_sha256_" + "0" * 64
            )


def test_postgres_completion_outbox_not_found_validation_and_close(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        with pytest.raises(tbm.PostgresCompletionOutboxV3NotFoundError):
            repository.get_event(
                "completion_outbox_event_sha256_" + "0" * 64
            )
        with pytest.raises(tbm.PostgresCompletionOutboxV3NotFoundError):
            repository.get_delivery(
                "completion_outbox_event_sha256_" + "0" * 64
            )
        with pytest.raises(tbm.CompletionOutboxContractError):
            repository.claim_due(worker_id=" ", lease_seconds=30)
        with pytest.raises(tbm.CompletionOutboxContractError):
            repository.claim_due(worker_id="worker", lease_seconds=0)
        with pytest.raises(ValueError):
            repository.claim_due(
                worker_id="worker",
                lease_seconds=30,
                limit=1001,
            )
        repository.close()
        repository.close()
        with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
            repository.get_event("anything")

    owned = tbm.PostgresCompletionOutboxV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )
    with owned as entered:
        assert entered is owned
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        owned.outcomes


def test_postgres_completion_outbox_database_recomputes_content_ids(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        completion = repository.outcomes.complete_session(_request())
        event = tbm.build_completion_outbox_event(
            completion.outcome,
            completion.session,
        )
        forged_event_id = "completion_outbox_event_sha256_" + "0" * 64
        forged_event_descriptor = tbm.dumps_completion_outbox_event(
            event
        ).replace(event.event_id, forged_event_id)
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_completion_outbox.events (
                                event_id, event_type, tenant_id, repository_id,
                                session_id, trace_id, run_id,
                                usage_decision_id, run_outcome_id,
                                outcome_descriptor_sha256, occurred_at,
                                descriptor
                            )
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            forged_event_id,
                            event.event_type,
                            event.tenant_id,
                            event.repository_id,
                            event.session_id,
                            event.trace_id,
                            event.run_id,
                            event.usage_decision_id,
                            event.run_outcome_id,
                            event.outcome_descriptor_sha256,
                            event.occurred_at,
                            forged_event_descriptor,
                        ),
                    )
def test_postgres_completion_outbox_database_recomputes_delivery_id(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresCompletionOutboxV3Repository(connection)
        _executing(repository)
        first = repository.complete_session(_request())
        claimed_at = (
            datetime.fromisoformat(
                first.delivery.updated_at.replace("Z", "+00:00")
            )
            + timedelta(microseconds=1)
        ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        claimed = tbm.claim_completion_outbox_delivery(
            first.delivery,
            worker_id="worker_001",
            claimed_at=claimed_at,
            lease_seconds=60,
        )
        forged_id = "completion_outbox_delivery_sha256_" + "0" * 64
        descriptor = tbm.dumps_completion_outbox_delivery(claimed).replace(
            claimed.delivery_revision_id,
            forged_id,
        )
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_completion_outbox.
                                delivery_revisions (
                                    event_id, version, delivery_revision_id,
                                    status, attempt_count, updated_at,
                                    available_at, worker_id, lease_expires_at,
                                    delivered_at, last_error_code,
                                    response_sha256, descriptor
                                )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            claimed.event_id,
                            claimed.version,
                            forged_id,
                            claimed.status,
                            claimed.attempt_count,
                            claimed.updated_at,
                            claimed.available_at,
                            claimed.worker_id,
                            claimed.lease_expires_at,
                            claimed.delivered_at,
                            claimed.last_error_code,
                            claimed.response_sha256,
                            descriptor,
                        ),
                    )
        with pytest.raises(psycopg.Error, match="committed head"):
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_completion_outbox.
                                delivery_revisions (
                                    event_id, version, delivery_revision_id,
                                    status, attempt_count, updated_at,
                                    available_at, worker_id, lease_expires_at,
                                    delivered_at, last_error_code,
                                    response_sha256, descriptor
                                )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            claimed.event_id,
                            claimed.version,
                            claimed.delivery_revision_id,
                            claimed.status,
                            claimed.attempt_count,
                            claimed.updated_at,
                            claimed.available_at,
                            claimed.worker_id,
                            claimed.lease_expires_at,
                            claimed.delivered_at,
                            claimed.last_error_code,
                            claimed.response_sha256,
                            tbm.dumps_completion_outbox_delivery(claimed),
                        ),
                    )
        assert repository.claim_due(
            worker_id="worker_002",
            lease_seconds=60,
        )[0].delivery.version == 2


def test_postgres_completion_outbox_unit_error_paths(
    monkeypatch: pytest.MonkeyPatch,
):
    import trace_backed_memory as tbm
    import trace_backed_memory.postgres_completion_outbox_v3 as module

    class DummyConnection:
        closed = False

    class Cursor:
        rowcount = 0

        def __init__(self, rows):
            self.rows = rows

        def execute(self, *args, **kwargs):
            return None

        def fetchall(self):
            return self.rows

    repository = tbm.PostgresCompletionOutboxV3Repository(
        DummyConnection()
    )
    with pytest.raises(TypeError):
        repository.complete_session(object())
    with pytest.raises(ValueError):
        repository.get_event(1)
    with pytest.raises(ValueError):
        repository.get_delivery(1)
    with pytest.raises(ValueError):
        repository.list_delivery_history(1)
    with pytest.raises(ValueError):
        repository.acknowledge(
            "event",
            expected_version=0,
            worker_id="worker",
        )
    with pytest.raises(ValueError):
        repository.acknowledge(
            1,
            expected_version=1,
            worker_id="worker",
        )

    with pytest.raises(tbm.PostgresCompletionOutboxV3SchemaError):
        repository._catalog_names(Cursor([{"name": 1}]), "SELECT")
    with pytest.raises(tbm.PostgresCompletionOutboxV3SchemaError):
        repository._schema_drift("unit")
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._event_from_row({})
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._event_from_row({"descriptor": "{}"})
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._delivery_from_row({})
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._delivery_from_row({"descriptor": "{}"})
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._select_event(
            Cursor([{"descriptor": "{}"}, {"descriptor": "{}"}]),
            "event",
        )
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._select_event_by_outcome(
            Cursor([{"descriptor": "{}"}, {"descriptor": "{}"}]),
            "outcome",
        )
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._select_current_delivery(
            Cursor([{"descriptor": "{}"}, {"descriptor": "{}"}]),
            "event",
        )

    class MissingError(Exception):
        sqlstate = "42P01"

    class OtherError(Exception):
        sqlstate = "XX000"

    with pytest.raises(tbm.PostgresCompletionOutboxV3SchemaError):
        repository._raise_database_error(MissingError(), "missing")
    with pytest.raises(tbm.PostgresCompletionOutboxV3PersistenceError):
        repository._raise_database_error(OtherError(), "other")

    module._expected_function_bodies.cache_clear()
    monkeypatch.setattr(module, "read_packaged_resource", lambda name: b"")
    with pytest.raises(
        tbm.PostgresCompletionOutboxV3SchemaError,
        match="incomplete",
    ):
        module._expected_function_bodies()
    module._expected_function_bodies.cache_clear()
    monkeypatch.setattr(
        module,
        "read_packaged_resource",
        lambda name: b"\xff",
    )
    with pytest.raises(
        tbm.PostgresCompletionOutboxV3SchemaError,
        match="could not read",
    ):
        module._expected_function_bodies()
    module._expected_function_bodies.cache_clear()
