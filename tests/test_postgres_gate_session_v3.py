from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import time

import pytest

from tests.postgres_support import PostgresCluster, assert_sql_succeeds


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-gate-session.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-gate-session-rollback.sql"
FINGERPRINT = "sha256:" + "a" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    installed = cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr


def _create(repository, *, session_id: str = "session_001", **changes):
    values = {
        "session_id": session_id,
        "tenant_id": "tenant_001",
        "repository_id": "repo_001",
        "principal_id": "principal_001",
        "agent_client_id": "agent_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "request_fingerprint": FINGERPRINT,
        "idempotency_key": "idempotency_001",
        "expires_in_seconds": 3600,
    }
    values.update(changes)
    return repository.create_or_get(**values)


def test_postgres_gate_session_install_repository_lifecycle_and_rollback(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version, contract_version "
        "FROM trace_backed_memory_v3_gate_session.schema_metadata",
    ) == "1|tbm.gate-session.v3"
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version FROM public.trace_backed_memory_schema",
    ) == "2"

    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        created = _create(repository)
        assert created.inserted is True
        assert created.session.status == "created"
        replayed = _create(repository, session_id="ignored_by_replay")
        assert replayed.inserted is False
        assert replayed.session == created.session

        prepared = repository.transition(
            created.session.session_id,
            "prepared",
            expected_version=1,
            lease_seconds=60,
            retrieval_snapshot_id="retrieval_001",
            system_gate_evaluation_id="system_gate_001",
        )
        assert prepared.status == "prepared"
        assert prepared.version == 2
        renewed = repository.renew_lease(
            prepared.session_id,
            expected_version=2,
            lease_seconds=120,
        )
        assert renewed.version == 3
        assert len(repository.history(prepared.session_id)) == 3
        assert repository.get(prepared.session_id) == renewed
        assert repository.list_due() == ()

    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT to_regnamespace("
        "'trace_backed_memory_v3_gate_session') IS NULL",
    ) == "t"
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version FROM public.trace_backed_memory_schema",
    ) == "2"


def test_postgres_gate_session_conflicts_and_stale_version_are_atomic(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        created = _create(repository).session
        with pytest.raises(
            tbm.PostgresGateSessionConflictError,
        ) as idempotency:
            _create(
                repository,
                session_id="session_other",
                trace_id="trace_other",
            )
        assert (
            idempotency.value.code
            == "TBM_POSTGRES_GATE_SESSION_IDEMPOTENCY_CONFLICT"
        )
        with pytest.raises(
            tbm.PostgresGateSessionConflictError,
        ) as session_id:
            _create(
                repository,
                idempotency_key="idempotency_other",
                trace_id="trace_other",
            )
        assert (
            session_id.value.code
            == "TBM_POSTGRES_GATE_SESSION_ID_CONFLICT"
        )
        with pytest.raises(tbm.GateSessionContractError) as stale:
            repository.transition(
                created.session_id,
                "prepared",
                expected_version=2,
                lease_seconds=60,
                retrieval_snapshot_id="retrieval_001",
                system_gate_evaluation_id="system_gate_001",
            )
        assert stale.value.code == "TBM_GATE_SESSION_STALE_VERSION"
        assert repository.history(created.session_id) == (created,)


def test_postgres_gate_session_direct_sql_guards_and_schema_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        _create(repository)

    for statement in (
        "UPDATE trace_backed_memory_v3_gate_session.gate_session_heads "
        "SET tenant_id = 'other'",
        "DELETE FROM "
        "trace_backed_memory_v3_gate_session.gate_session_heads",
        "UPDATE trace_backed_memory_v3_gate_session.gate_session_revisions "
        "SET status = 'canceled'",
        "DELETE FROM "
        "trace_backed_memory_v3_gate_session.gate_session_revisions",
        "TRUNCATE "
        "trace_backed_memory_v3_gate_session.gate_session_revisions",
    ):
        rejected = postgres_cluster.run(statement)
        assert rejected.returncode != 0
        assert "immutable" in rejected.stderr

    illegal_revision = postgres_cluster.run(
        "INSERT INTO "
        "trace_backed_memory_v3_gate_session.gate_session_revisions "
        "(session_id, version, status, updated_at, expires_at, "
        "lease_expires_at, payload) "
        "SELECT session_id, 2, 'completed', clock_timestamp(), "
        "expires_at, NULL, payload FROM "
        "trace_backed_memory_v3_gate_session.gate_session_revisions "
        "WHERE version = 1"
    )
    assert illegal_revision.returncode != 0
    assert "transition is invalid" in illegal_revision.stderr

    orphan_revision = postgres_cluster.run(
        "INSERT INTO "
        "trace_backed_memory_v3_gate_session.gate_session_revisions "
        "(session_id, version, status, updated_at, expires_at, "
        "lease_expires_at, payload) "
        "SELECT session_id, 2, 'prepared', clock_timestamp(), "
        "expires_at, clock_timestamp() + interval '1 minute', payload "
        "FROM trace_backed_memory_v3_gate_session.gate_session_revisions "
        "WHERE version = 1"
    )
    assert orphan_revision.returncode != 0
    assert "head and revision are inconsistent" in orphan_revision.stderr

    assert_sql_succeeds(
        postgres_cluster,
        "CREATE INDEX unexpected_gate_index ON "
        "trace_backed_memory_v3_gate_session.gate_session_heads "
        "(trace_id)",
    )
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        with pytest.raises(tbm.PostgresGateSessionSchemaError):
            repository.get("session_001")


def test_postgres_gate_session_detects_trigger_function_body_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        """
        CREATE OR REPLACE FUNCTION
            trace_backed_memory_v3_gate_session.reject_immutable_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RETURN NEW;
        END
        $$
        """,
    )
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        with pytest.raises(tbm.PostgresGateSessionSchemaError):
            repository.get("session_001")


def test_postgres_gate_session_borrowed_savepoint_preserves_outer_work(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        with connection.transaction():
            connection.execute(
                "CREATE TEMP TABLE outer_gate_state (value text)"
            )
            connection.execute(
                "INSERT INTO outer_gate_state VALUES ('before')"
            )
            _create(repository)
            with pytest.raises(tbm.PostgresGateSessionConflictError):
                _create(
                    repository,
                    session_id="other",
                    trace_id="other",
                )
            connection.execute(
                "INSERT INTO outer_gate_state VALUES ('after')"
            )
            assert connection.execute(
                "SELECT value FROM outer_gate_state ORDER BY value"
            ).fetchall() == [("after",), ("before",)]


def test_postgres_gate_session_concurrent_idempotency_is_single_insert(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)

    def create_once(session_id: str):
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            return _create(
                tbm.PostgresGateSessionRepository(connection),
                session_id=session_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(create_once, ("session_concurrent_a", "session_concurrent_b"))
        )
    assert sorted(result.inserted for result in results) == [False, True]
    assert results[0].session.session_id == results[1].session.session_id
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT count(*) FROM "
        "trace_backed_memory_v3_gate_session.gate_session_heads",
    ) == "1"
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT count(*) FROM "
        "trace_backed_memory_v3_gate_session.gate_session_revisions",
    ) == "1"


def test_postgres_gate_session_concurrent_transition_has_one_cas_winner(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        created = _create(repository).session
        prepared = repository.transition(
            created.session_id,
            "prepared",
            expected_version=1,
            lease_seconds=60,
            retrieval_snapshot_id="retrieval_001",
            system_gate_evaluation_id="system_gate_001",
        )

    def transition_once() -> int | str:
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = tbm.PostgresGateSessionRepository(connection)
            try:
                return repository.transition(
                    prepared.session_id,
                    "awaiting_decision",
                    expected_version=2,
                ).version
            except tbm.GateSessionContractError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: transition_once(), range(2)))
    assert sorted(results, key=str) == [
        3,
        "TBM_GATE_SESSION_STALE_VERSION",
    ]
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        assert len(repository.history(prepared.session_id)) == 3


def test_postgres_gate_session_samples_clock_after_waiting_for_head_lock(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as setup:
        repository = tbm.PostgresGateSessionRepository(setup)
        created = _create(repository).session
        prepared = repository.transition(
            created.session_id,
            "prepared",
            expected_version=1,
            lease_seconds=1,
            retrieval_snapshot_id="retrieval_001",
            system_gate_evaluation_id="system_gate_001",
        )

    started = Event()

    def blocked_transition() -> str:
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = tbm.PostgresGateSessionRepository(connection)
            started.set()
            try:
                repository.transition(
                    prepared.session_id,
                    "awaiting_decision",
                    expected_version=2,
                )
            except tbm.PostgresGateSessionConflictError as error:
                return error.code
        return "unexpected-success"

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as holder:
            with holder.transaction():
                holder.execute(
                    "SELECT 1 FROM "
                    "trace_backed_memory_v3_gate_session.gate_session_heads "
                    "WHERE session_id = %s FOR UPDATE",
                    (prepared.session_id,),
                )
                future = pool.submit(blocked_transition)
                assert started.wait(timeout=5)
                time.sleep(1.2)
        assert future.result(timeout=5) == (
            "TBM_POSTGRES_GATE_SESSION_LEASE_EXPIRED"
        )
    finally:
        pool.shutdown(wait=True)


def test_postgres_gate_session_install_and_rollback_fail_closed(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    replay = postgres_cluster.run_script(INSTALL)
    assert replay.returncode != 0
    assert 'schema "trace_backed_memory_v3_gate_session" already exists' in (
        replay.stderr
    )
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE TABLE trace_backed_memory_v3_gate_session.unexpected "
        "(value text)",
    )
    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT to_regnamespace("
        "'trace_backed_memory_v3_gate_session') IS NOT NULL",
    ) == "t"


def test_postgres_gate_session_runtime_and_rollback_reject_active_v1(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 1 WHERE singleton",
    )
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresGateSessionRepository(connection)
        with pytest.raises(tbm.PostgresGateSessionSchemaError):
            repository.get("session_001")
    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert "requires active schema version 2" in rollback.stderr
