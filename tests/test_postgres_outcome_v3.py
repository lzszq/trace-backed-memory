from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path

import pytest

import trace_backed_memory.postgres_outcome_v3 as postgres_outcome_module
from tests.postgres_support import PostgresCluster, assert_sql_succeeds


ROOT = Path(__file__).resolve().parents[1]
GATE_INSTALL = ROOT / "schemas" / "postgres-v3-gate-session.sql"
GATE_ROLLBACK = ROOT / "schemas" / "postgres-v3-gate-session-rollback.sql"
INSTALL = ROOT / "schemas" / "postgres-v3-outcome.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-outcome-rollback.sql"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    gate = cluster.run_script(GATE_INSTALL)
    assert gate.returncode == 0, gate.stderr
    outcome = cluster.run_script(INSTALL)
    assert outcome.returncode == 0, outcome.stderr


def _request(*, session_id: str = "gate_session_001", **changes):
    import trace_backed_memory as tbm

    values = {
        "session_id": session_id,
        "expected_version": 6,
        "result": "pass",
        "evaluator_id": "evaluation_service",
        "evaluator_version": "1.2.0",
        "output_sha256": DIGEST_A,
        "evidence_artifact_sha256s": (DIGEST_B,),
        "latency_ms": 250,
        "cost_usd": 0.25,
    }
    values.update(changes)
    return tbm.GateCompletionRequest(**values)


def _executing(repository, *, suffix: str = "001") -> None:
    sessions = repository.gate_sessions
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
    assert executing.status == "executing"
    assert executing.version == 6


def test_postgres_outcome_install_completion_replay_and_rollback(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version, contract_version "
        "FROM trace_backed_memory_v3_outcome.schema_metadata",
    ) == "1|tbm.run-outcome.v3"

    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)
        first = tbm.GateSessionCompletionService(repository).complete(
            _request()
        )
        assert first.inserted is True
        assert first.session.status == "completed"
        assert first.session.version == 7
        assert first.session.run_outcome_id == first.outcome.run_outcome_id
        assert first.outcome.measured_at == first.session.updated_at
        assert repository.get_session(first.session.session_id) == first.session
        assert repository.get_outcome(first.outcome.run_outcome_id) == first.outcome

        replay = repository.complete_session(_request())
        assert replay == tbm.GateCompletionResult(
            session=first.session,
            outcome=first.outcome,
            inserted=False,
        )
        assert len(repository.gate_sessions.history(first.session.session_id)) == 7

    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    gate_rolled_back = postgres_cluster.run_script(GATE_ROLLBACK)
    assert gate_rolled_back.returncode == 0, gate_rolled_back.stderr


def test_postgres_outcome_conflicts_guard_and_orphan_fail_closed(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)

        with pytest.raises(tbm.PostgresGateSessionConflictError) as guarded:
            repository.gate_sessions.transition(
                "gate_session_001",
                "completed",
                expected_version=6,
                run_outcome_id="run_outcome_sha256_" + "f" * 64,
            )
        assert guarded.value.code == (
            "TBM_POSTGRES_GATE_SESSION_COMPLETION_AUTHORITY"
        )

        lower_level = tbm.PostgresGateSessionRepository(connection)
        lower_level.transition(
            "gate_session_001",
            "completed",
            expected_version=6,
            run_outcome_id="run_outcome_sha256_" + "f" * 64,
        )
        lower_level.close()
        with pytest.raises(tbm.PostgresOutcomeV3PersistenceError) as orphaned:
            repository.complete_session(_request())
        assert orphaned.value.code == "TBM_POSTGRES_OUTCOME_ORPHANED_SESSION"


def test_postgres_outcome_state_stale_replay_and_savepoint_are_atomic(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        created = repository.gate_sessions.create_or_get(
            session_id="gate_session_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            principal_id="principal_001",
            agent_client_id="agent_001",
            trace_id="trace_001",
            run_id="run_001",
            request_fingerprint=DIGEST_A,
            idempotency_key="request-001",
            expires_in_seconds=3600,
        ).session
        with pytest.raises(tbm.PostgresOutcomeV3ConflictError):
            repository.complete_session(
                _request(expected_version=created.version)
            )

        _executing(repository, suffix="002")
        with connection.transaction():
            connection.execute(
                "CREATE TEMP TABLE outer_outcome_state (value text)"
            )
            connection.execute(
                "INSERT INTO outer_outcome_state VALUES ('before')"
            )
            with pytest.raises(tbm.GateSessionContractError):
                repository.complete_session(
                    _request(
                        session_id="gate_session_002",
                        expected_version=5,
                    )
                )
            connection.execute(
                "INSERT INTO outer_outcome_state VALUES ('after')"
            )
            assert connection.execute(
                "SELECT value FROM outer_outcome_state ORDER BY value"
            ).fetchall() == [("after",), ("before",)]
        assert repository.get_session("gate_session_002").status == "executing"


def test_postgres_outcome_direct_sql_guards_catalog_and_rollback_fail_closed(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)
        result = repository.complete_session(_request())
        _executing(repository, suffix="002")
        lower_level = tbm.PostgresGateSessionRepository(connection)
        completed = lower_level.transition(
            "gate_session_002",
            "completed",
            expected_version=6,
            run_outcome_id="run_outcome_sha256_" + "f" * 64,
        )
        lower_level.close()
        invalid_outcome = tbm.build_run_outcome(
            session_id=completed.session_id,
            trace_id=completed.trace_id,
            run_id=completed.run_id,
            usage_decision_id="usage_002",
            result="pass",
            evaluator_id="evaluation_service",
            evaluator_version="1.2.0",
            evidence_artifact_sha256s=(DIGEST_B,),
            measured_at=completed.updated_at,
            output_sha256=DIGEST_A,
        )
        invalid_values = list(
            repository._outcome_values(invalid_outcome)
        )
        forged_id = "run_outcome_sha256_" + "f" * 64
        invalid_values[0] = forged_id
        forged_descriptor = json.loads(invalid_values[-1])
        forged_descriptor["run_outcome_id"] = forged_id
        invalid_values[-1] = json.dumps(
            forged_descriptor,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        insert_sql = """
            INSERT INTO
                trace_backed_memory_v3_outcome.run_outcomes (
                    run_outcome_id,
                    session_id,
                    trace_id,
                    run_id,
                    usage_decision_id,
                    result,
                    evaluator_id,
                    evaluator_version,
                    output_sha256,
                    tool_outputs_sha256,
                    evidence_artifact_sha256s_json,
                    latency_ms,
                    cost_usd_json,
                    error_code,
                    measured_at,
                    descriptor
                )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        with pytest.raises(psycopg.Error, match="invalid PostgreSQL"):
            with connection.transaction():
                connection.execute(insert_sql, tuple(invalid_values))

        malformed_identifier = list(invalid_values)
        malformed_identifier[6] = " "
        malformed_descriptor = dict(forged_descriptor)
        malformed_descriptor["evaluator_id"] = " "
        malformed_identifier[-1] = json.dumps(
            malformed_descriptor,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(psycopg.Error, match="invalid.*identifier"):
            with connection.transaction():
                connection.execute(
                    insert_sql,
                    tuple(malformed_identifier),
                )

        malformed_session = list(invalid_values)
        malformed_session[1] = " gate_session_002 "
        malformed_descriptor = dict(forged_descriptor)
        malformed_descriptor["session_id"] = " gate_session_002 "
        malformed_session[-1] = json.dumps(
            malformed_descriptor,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(psycopg.Error, match="invalid.*identifier"):
            with connection.transaction():
                connection.execute(insert_sql, tuple(malformed_session))

        malformed_error = list(invalid_values)
        malformed_error[5] = "error"
        malformed_error[13] = " "
        malformed_descriptor = dict(forged_descriptor)
        malformed_descriptor["result"] = "error"
        malformed_descriptor["error_code"] = " "
        malformed_error[-1] = json.dumps(
            malformed_descriptor,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(psycopg.Error, match="invalid.*error text"):
            with connection.transaction():
                connection.execute(insert_sql, tuple(malformed_error))

        malformed_timestamp = list(invalid_values)
        malformed_timestamp[14] = "10000-01-01T00:00:00Z"
        malformed_descriptor = dict(forged_descriptor)
        malformed_descriptor["measured_at"] = "10000-01-01T00:00:00Z"
        malformed_timestamp[-1] = json.dumps(
            malformed_descriptor,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(psycopg.Error, match="invalid.*timestamp"):
            with connection.transaction():
                connection.execute(insert_sql, tuple(malformed_timestamp))

    for statement in (
        "UPDATE trace_backed_memory_v3_outcome.run_outcomes "
        "SET result = 'fail'",
        "DELETE FROM trace_backed_memory_v3_outcome.run_outcomes",
        "TRUNCATE trace_backed_memory_v3_outcome.run_outcomes",
    ):
        rejected = postgres_cluster.run(statement)
        assert rejected.returncode != 0
        assert "immutable" in rejected.stderr

    assert_sql_succeeds(
        postgres_cluster,
        "CREATE INDEX unexpected_outcome_index ON "
        "trace_backed_memory_v3_outcome.run_outcomes (result)",
    )
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        with pytest.raises(tbm.PostgresOutcomeV3SchemaError):
            repository.get_outcome(result.outcome.run_outcome_id)

    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT to_regnamespace('trace_backed_memory_v3_outcome') "
        "IS NOT NULL",
    ) == "t"


def test_postgres_outcome_concurrent_exact_completion_is_one_insert(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)

    def complete_once():
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = tbm.PostgresOutcomeV3Repository(connection)
            return repository.complete_session(_request())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: complete_once(), range(2)))

    assert sorted(result.inserted for result in results) == [False, True]
    assert results[0].session == results[1].session
    assert results[0].outcome == results[1].outcome
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT count(*) FROM trace_backed_memory_v3_outcome.run_outcomes",
    ) == "1"


@pytest.mark.parametrize(
    "cost_usd",
    [None, 0.0, 1.0, 1e-7, 1e20, 1.7976931348623157e308],
)
def test_postgres_outcome_accepts_canonical_float_costs(
    postgres_cluster: PostgresCluster,
    cost_usd: float | None,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)
        completed = repository.complete_session(_request(cost_usd=cost_usd))
        assert completed.outcome.cost_usd == cost_usd


def test_postgres_outcome_missing_schema_exports_and_resources(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    postgres_cluster.load_schema()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        with pytest.raises(tbm.PostgresOutcomeV3SchemaError):
            repository.get_outcome("run_outcome_sha256_" + "f" * 64)

    assert tbm.POSTGRES_OUTCOME_V3_SCHEMA_VERSION == 1
    assert (
        tbm.read_packaged_resource("schemas/postgres-v3-outcome.sql")
        == INSTALL.read_bytes()
    )
    assert (
        tbm.read_packaged_resource(
            "schemas/postgres-v3-outcome-rollback.sql"
        )
        == ROLLBACK.read_bytes()
    )


def test_postgres_outcome_replay_conflict_missing_and_closed_errors(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)
        repository.complete_session(_request())
        with pytest.raises(tbm.PostgresOutcomeV3ConflictError) as conflict:
            repository.complete_session(_request(output_sha256=DIGEST_B))
        assert conflict.value.code == (
            "TBM_POSTGRES_OUTCOME_COMPLETION_CONFLICT"
        )
        with pytest.raises(tbm.PostgresOutcomeV3NotFoundError):
            repository.get_session("missing")
        with pytest.raises(tbm.PostgresOutcomeV3NotFoundError):
            repository.get_outcome("run_outcome_sha256_" + "f" * 64)
        with pytest.raises(TypeError):
            repository.complete_session(object())
        with pytest.raises(ValueError):
            repository.get_session(1)
        with pytest.raises(ValueError):
            repository.get_outcome(1)
        repository.close()
        repository.close()
        with pytest.raises(tbm.PostgresOutcomeV3PersistenceError):
            repository.get_outcome("run_outcome_sha256_" + "f" * 64)

    with pytest.raises(ValueError):
        tbm.PostgresOutcomeV3Repository(None)


def test_postgres_outcome_cross_session_link_fails_closed(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository, suffix="001")
        _executing(repository, suffix="002")
        second = repository.complete_session(
            _request(session_id="gate_session_002")
        )
        lower_level = tbm.PostgresGateSessionRepository(connection)
        lower_level.transition(
            "gate_session_001",
            "completed",
            expected_version=6,
            run_outcome_id=second.outcome.run_outcome_id,
        )
        lower_level.close()
        with pytest.raises(tbm.PostgresOutcomeV3PersistenceError) as linked:
            repository.complete_session(_request())
        assert linked.value.code == "TBM_POSTGRES_OUTCOME_ORPHANED_SESSION"
        assert isinstance(linked.value.__cause__, tbm.OutcomeContractError)


def test_postgres_outcome_readback_mismatch_rolls_back(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)
        _executing(repository)
        original = repository._select_outcome

        def changed(cursor, run_outcome_id):
            stored = original(cursor, run_outcome_id)
            return tbm.build_run_outcome(
                session_id=stored.session_id,
                trace_id=stored.trace_id,
                run_id=stored.run_id,
                usage_decision_id=stored.usage_decision_id,
                result=stored.result,
                evaluator_id=stored.evaluator_id,
                evaluator_version="changed",
                evidence_artifact_sha256s=(
                    stored.evidence_artifact_sha256s
                ),
                measured_at=stored.measured_at,
                output_sha256=stored.output_sha256,
                tool_outputs_sha256=stored.tool_outputs_sha256,
                latency_ms=stored.latency_ms,
                cost_usd=stored.cost_usd,
                error_code=stored.error_code,
            )

        monkeypatch.setattr(repository, "_select_outcome", changed)
        with pytest.raises(
            tbm.PostgresOutcomeV3PersistenceError,
            match="read-back",
        ):
            repository.complete_session(_request())
        assert repository.get_session("gate_session_001").status == "executing"


def test_postgres_outcome_dependency_errors_are_mapped(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeV3Repository(connection)

        cases = (
            (
                tbm.PostgresGateSessionNotFoundError(
                    "TBM_TEST",
                    "missing",
                ),
                tbm.PostgresOutcomeV3NotFoundError,
            ),
            (
                tbm.PostgresGateSessionConflictError(
                    "TBM_TEST",
                    "conflict",
                ),
                tbm.PostgresOutcomeV3ConflictError,
            ),
            (
                tbm.PostgresGateSessionPersistenceError(
                    "TBM_TEST",
                    "corrupt",
                ),
                tbm.PostgresOutcomeV3PersistenceError,
            ),
        )
        for dependency_error, expected_type in cases:
            with monkeypatch.context() as patch:
                patch.setattr(
                    repository._gate_sessions,
                    "_select_current",
                    lambda *_args, error=dependency_error, **_kwargs: (
                        (_ for _ in ()).throw(error)
                    ),
                )
                with pytest.raises(expected_type) as mapped:
                    repository.complete_session(_request())
                assert mapped.value.__cause__ is dependency_error

        dependency_error = tbm.PostgresGateSessionPersistenceError(
            "TBM_TEST",
            "corrupt",
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                repository._gate_sessions,
                "_select_current",
                lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(dependency_error)
                ),
            )
            with pytest.raises(tbm.PostgresOutcomeV3PersistenceError) as mapped:
                repository.get_session("gate_session_001")
            assert mapped.value.__cause__ is dependency_error

        schema_error = tbm.PostgresGateSessionSchemaError(
            "TBM_TEST",
            "schema drift",
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                repository._gate_sessions,
                "_select_current",
                lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(schema_error)
                ),
            )
            with pytest.raises(tbm.PostgresOutcomeV3SchemaError) as mapped:
                repository.get_session("gate_session_001")
            assert mapped.value.__cause__ is schema_error

        with monkeypatch.context() as patch:
            patch.setattr(
                repository._gate_sessions,
                "_lock_schema",
                lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(schema_error)
                ),
            )
            with pytest.raises(tbm.PostgresOutcomeV3SchemaError) as completion:
                repository.complete_session(_request())
            assert completion.value.__cause__ is schema_error
            with pytest.raises(tbm.PostgresOutcomeV3SchemaError) as read:
                repository.get_outcome(
                    "run_outcome_sha256_" + "f" * 64
                )
            assert read.value.__cause__ is schema_error


def test_postgres_outcome_private_validation_helpers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    import trace_backed_memory as tbm

    with pytest.raises(tbm.PostgresOutcomeV3PersistenceError):
        tbm.PostgresOutcomeV3Repository._outcome_from_row({})

    outcome = tbm.build_run_outcome(
        session_id="gate_session_001",
        trace_id="trace_001",
        run_id="run_001",
        usage_decision_id="usage_001",
        result="pass",
        evaluator_id="evaluation_service",
        evaluator_version="1.2.0",
        evidence_artifact_sha256s=(DIGEST_B,),
        measured_at="2026-07-29T00:00:00Z",
        output_sha256=DIGEST_A,
    )
    names = (
        "run_outcome_id",
        "session_id",
        "trace_id",
        "run_id",
        "usage_decision_id",
        "result",
        "evaluator_id",
        "evaluator_version",
        "output_sha256",
        "tool_outputs_sha256",
        "evidence_artifact_sha256s_json",
        "latency_ms",
        "cost_usd_json",
        "error_code",
        "measured_at",
        "descriptor",
    )
    row = dict(
        zip(
            names,
            tbm.PostgresOutcomeV3Repository._outcome_values(outcome),
            strict=True,
        )
    )
    row["measured_at"] = datetime.fromisoformat(
        outcome.measured_at.replace("Z", "+00:00")
    )
    invalid = dict(row)
    invalid["evaluator_id"] = "changed"
    with pytest.raises(tbm.PostgresOutcomeV3PersistenceError, match="columns"):
        tbm.PostgresOutcomeV3Repository._outcome_from_row(invalid)
    invalid = dict(row)
    invalid["descriptor"] = "{}"
    with pytest.raises(tbm.PostgresOutcomeV3PersistenceError, match="validation"):
        tbm.PostgresOutcomeV3Repository._outcome_from_row(invalid)
    invalid = dict(row)
    invalid["measured_at"] = None
    with pytest.raises(tbm.PostgresOutcomeV3PersistenceError) as timestamp:
        tbm.PostgresOutcomeV3Repository._outcome_from_row(invalid)
    assert isinstance(
        timestamp.value.__cause__,
        tbm.PostgresGateSessionPersistenceError,
    )

    class RowsCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return [{"name": 1}]

    with pytest.raises(tbm.PostgresOutcomeV3SchemaError, match="invalid shape"):
        tbm.PostgresOutcomeV3Repository._catalog_names(
            RowsCursor(),
            "ignored",
        )

    class MultipleRowsCursor(RowsCursor):
        def fetchall(self):
            return [{}, {}]

    with pytest.raises(
        tbm.PostgresOutcomeV3PersistenceError,
        match="invalid shape",
    ):
        tbm.PostgresOutcomeV3Repository._select_outcome(
            MultipleRowsCursor(),
            "run_outcome_sha256_" + "f" * 64,
        )
    with pytest.raises(tbm.PostgresOutcomeV3SchemaError):
        tbm.PostgresOutcomeV3Repository._schema_drift("test")

    class UndefinedError(Exception):
        sqlstate = "42P01"

    with pytest.raises(tbm.PostgresOutcomeV3SchemaError):
        tbm.PostgresOutcomeV3Repository._raise_database_error(
            UndefinedError(),
            "ignored",
        )
    with pytest.raises(tbm.PostgresOutcomeV3PersistenceError):
        tbm.PostgresOutcomeV3Repository._raise_database_error(
            RuntimeError("database failure"),
            "bounded failure",
        )

    postgres_outcome_module._expected_function_bodies.cache_clear()
    monkeypatch.setattr(
        postgres_outcome_module,
        "read_packaged_resource",
        lambda _name: b"",
    )
    with pytest.raises(tbm.PostgresOutcomeV3SchemaError, match="incomplete"):
        postgres_outcome_module._expected_function_bodies()
    postgres_outcome_module._expected_function_bodies.cache_clear()
    monkeypatch.setattr(
        postgres_outcome_module,
        "read_packaged_resource",
        lambda _name: b"\xff",
    )
    with pytest.raises(tbm.PostgresOutcomeV3SchemaError, match="could not read"):
        postgres_outcome_module._expected_function_bodies()
    postgres_outcome_module._expected_function_bodies.cache_clear()


def test_postgres_outcome_owned_connection_context_manager(
    postgres_cluster: PostgresCluster,
):
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    repository = tbm.PostgresOutcomeV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )
    with repository as opened:
        assert opened.gate_sessions is repository.gate_sessions
    with pytest.raises(tbm.PostgresOutcomeV3PersistenceError):
        repository.__enter__()
    repository.__exit__(None, None, None)


def test_postgres_outcome_function_drift_repeat_install_and_active_v1(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    repeated = postgres_cluster.run_script(INSTALL)
    assert repeated.returncode != 0
    assert 'schema "trace_backed_memory_v3_outcome" already exists' in (
        repeated.stderr
    )

    assert_sql_succeeds(
        postgres_cluster,
        """
        CREATE OR REPLACE FUNCTION
            trace_backed_memory_v3_outcome.reject_immutable_change()
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
        repository = tbm.PostgresOutcomeV3Repository(connection)
        with pytest.raises(tbm.PostgresOutcomeV3SchemaError):
            repository.get_outcome("run_outcome_sha256_" + "f" * 64)

    assert_sql_succeeds(
        postgres_cluster,
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 1 WHERE singleton",
    )
    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert "requires active schema version 2" in rollback.stderr
