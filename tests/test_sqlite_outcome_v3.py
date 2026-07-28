from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import sqlite3
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_outcome_v3 as sqlite_outcome_module
from trace_backed_memory.gate_completion_v3 import (
    GateCompletionRequest,
    GateCompletionResult,
    GateCompletionV3Error,
    GateSessionCompletionService,
)
from trace_backed_memory.gate_session_v3 import GateSessionContractError
from trace_backed_memory.outcome_v3 import OutcomeContractError, build_run_outcome
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_gate_session_v3 import (
    SQLiteGateSessionConflictError,
    SQLiteGateSessionPersistenceError,
    SQLiteGateSessionRepository,
)
from trace_backed_memory.sqlite_outcome_v3 import (
    SQLITE_OUTCOME_V3_SCHEMA_VERSION,
    SQLiteOutcomeV3ConflictError,
    SQLiteOutcomeV3NotFoundError,
    SQLiteOutcomeV3PersistenceError,
    SQLiteOutcomeV3Repository,
    SQLiteOutcomeV3SchemaError,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64
TIMES = tuple(
    f"2026-07-29T00:{minute:02d}:00Z" for minute in range(20)
)


class SequenceClock:
    def __init__(self, values: tuple[str, ...] = TIMES) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return next(self._values)


def _request(**overrides: object) -> GateCompletionRequest:
    values: dict[str, object] = {
        "session_id": "gate_session_001",
        "expected_version": 6,
        "result": "pass",
        "evaluator_id": "evaluation_service",
        "evaluator_version": "1.2.0",
        "output_sha256": DIGEST_A,
        "evidence_artifact_sha256s": (DIGEST_B,),
        "latency_ms": 250,
        "cost_usd": 0.25,
    }
    values.update(overrides)
    return GateCompletionRequest(**values)  # type: ignore[arg-type]


def _executing(
    repository: SQLiteOutcomeV3Repository,
    *,
    suffix: str = "001",
) -> None:
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


def _repository(
    database: str | Path = ":memory:",
    *,
    clock: SequenceClock | None = None,
    **kwargs: object,
) -> SQLiteOutcomeV3Repository:
    return SQLiteOutcomeV3Repository.connect(
        database,
        initialize=True,
        clock=clock or SequenceClock(),
        **kwargs,
    )


def test_completion_service_atomically_persists_outcome_and_session():
    clock = SequenceClock()
    with _repository(clock=clock) as repository:
        _executing(repository)
        result = GateSessionCompletionService(repository).complete(_request())

        assert result.inserted is True
        assert result.session.status == "completed"
        assert result.session.version == 7
        assert result.session.updated_at == TIMES[6]
        assert result.session.run_outcome_id == result.outcome.run_outcome_id
        assert result.outcome.measured_at == TIMES[6]
        assert result.outcome.session_id == result.session.session_id
        assert repository.get_session(result.session.session_id) == result.session
        assert (
            repository.get_outcome(result.outcome.run_outcome_id)
            == result.outcome
        )
        assert len(repository.gate_sessions.history(result.session.session_id)) == 7


def test_exact_completion_replay_is_idempotent_without_reading_clock_again():
    clock = SequenceClock()
    with _repository(clock=clock) as repository:
        _executing(repository)
        first = repository.complete_session(_request())
        calls_after_first = clock.calls

        replay = repository.complete_session(_request())

        assert replay == replace(first, inserted=False)
        assert clock.calls == calls_after_first
        assert len(repository.gate_sessions.history("gate_session_001")) == 7


def test_completed_session_rejects_a_different_outcome():
    with _repository() as repository:
        _executing(repository)
        repository.complete_session(_request())

        with pytest.raises(
            SQLiteOutcomeV3ConflictError,
            match="another outcome",
        ):
            repository.complete_session(
                _request(output_sha256=DIGEST_B)
            )


def test_completion_requires_executing_session_and_exact_version():
    with _repository() as repository:
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
        with pytest.raises(
            SQLiteOutcomeV3ConflictError,
            match="executing",
        ):
            repository.complete_session(
                _request(expected_version=created.version)
            )

    with _repository() as repository:
        _executing(repository)
        with pytest.raises(
            GateSessionContractError,
            match="expected_version",
        ):
            repository.complete_session(_request(expected_version=5))
        assert repository.get_session("gate_session_001").status == "executing"
        with pytest.raises(SQLiteOutcomeV3NotFoundError):
            repository.get_outcome(
                "run_outcome_sha256_" + "f" * 64
            )


def test_shared_gate_authority_cannot_bypass_atomic_completion():
    with _repository() as repository:
        _executing(repository)

        with pytest.raises(
            SQLiteGateSessionConflictError,
            match="RunOutcome authority",
        ) as blocked:
            repository.gate_sessions.transition(
                "gate_session_001",
                "completed",
                expected_version=6,
                run_outcome_id="run_outcome_sha256_" + "f" * 64,
            )

        assert blocked.value.code == (
            "TBM_SQLITE_GATE_SESSION_COMPLETION_AUTHORITY"
        )
        assert repository.get_session("gate_session_001").status == "executing"


def test_orphaned_completed_session_fails_as_persistence_corruption():
    with _repository() as repository:
        _executing(repository)
        lower_level = SQLiteGateSessionRepository(
            repository._connection,
            clock=lambda: TIMES[6],
        )
        lower_level.transition(
            "gate_session_001",
            "completed",
            expected_version=6,
            run_outcome_id="run_outcome_sha256_" + "f" * 64,
        )
        lower_level.close()

        with pytest.raises(SQLiteOutcomeV3PersistenceError) as orphaned:
            repository.complete_session(_request())

        assert orphaned.value.code == "TBM_SQLITE_OUTCOME_ORPHANED_SESSION"


def test_cross_session_outcome_link_fails_as_persistence_corruption():
    with _repository() as repository:
        _executing(repository, suffix="001")
        _executing(repository, suffix="002")
        second = repository.complete_session(
            _request(
                session_id="gate_session_002",
                expected_version=6,
            )
        )
        lower_level = SQLiteGateSessionRepository(
            repository._connection,
            clock=lambda: TIMES[13],
        )
        lower_level.transition(
            "gate_session_001",
            "completed",
            expected_version=6,
            run_outcome_id=second.outcome.run_outcome_id,
        )
        lower_level.close()

        with pytest.raises(SQLiteOutcomeV3PersistenceError) as orphaned:
            repository.complete_session(_request())

        assert orphaned.value.code == "TBM_SQLITE_OUTCOME_ORPHANED_SESSION"
        assert isinstance(orphaned.value.__cause__, OutcomeContractError)


def test_gate_session_conflicts_are_mapped_to_outcome_errors():
    clock = SequenceClock(TIMES[:6] + ("2026-07-29T02:00:00Z",))
    with _repository(clock=clock) as repository:
        _executing(repository)

        with pytest.raises(SQLiteOutcomeV3ConflictError) as conflict:
            repository.complete_session(_request())

        assert conflict.value.code == "TBM_SQLITE_OUTCOME_SESSION_CONFLICT"
        assert isinstance(conflict.value.__cause__, SQLiteGateSessionConflictError)
        assert repository.get_session("gate_session_001").status == "executing"


def test_gate_session_persistence_failures_are_mapped(
    monkeypatch: pytest.MonkeyPatch,
):
    with _repository() as repository:
        dependency_error = SQLiteGateSessionPersistenceError(
            "TBM_TEST_DEPENDENCY",
            "corrupt dependent row",
        )

        def fail_select(_cursor, _session_id):
            raise dependency_error

        monkeypatch.setattr(
            repository._gate_sessions,
            "_select_current",
            fail_select,
        )
        with pytest.raises(SQLiteOutcomeV3PersistenceError) as completion:
            repository.complete_session(_request())
        assert completion.value.code == "TBM_SQLITE_OUTCOME_DEPENDENCY"
        assert completion.value.__cause__ is dependency_error

        with pytest.raises(SQLiteOutcomeV3PersistenceError) as readback:
            repository.get_session("gate_session_001")
        assert readback.value.code == "TBM_SQLITE_OUTCOME_DEPENDENCY"
        assert readback.value.__cause__ is dependency_error


def test_backward_clock_fails_without_partial_completion():
    clock = SequenceClock(TIMES[:6] + (TIMES[4],))
    with _repository(clock=clock) as repository:
        _executing(repository)
        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="backwards",
        ):
            repository.complete_session(_request())
        assert repository.get_session("gate_session_001").status == "executing"


def test_equal_clock_advances_one_second_and_unicode_round_trips():
    clock = SequenceClock(TIMES[:6] + (TIMES[5],))
    with _repository(clock=clock) as repository:
        _executing(repository)
        result = repository.complete_session(
            _request(
                result="error",
                evaluator_id="评估器",
                error_code="工具失败",
            )
        )
        assert result.outcome.measured_at == "2026-07-29T00:05:01Z"
        assert result.outcome.evaluator_id == "评估器"
        assert result.outcome.error_code == "工具失败"


def test_equal_clock_at_timestamp_limit_fails_stably():
    timestamp = "9999-12-31T23:59:59.999999Z"
    clock = SequenceClock((timestamp,))
    with _repository(clock=clock) as repository:
        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="supported range",
        ):
            repository._trusted_after(timestamp)


def test_completion_preserves_callers_transaction_with_savepoint():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(
            "schemas/sqlite-v3-gate-session.sql"
        ).decode("utf-8")
    )
    connection.executescript(
        read_packaged_resource("schemas/sqlite-v3-outcome.sql").decode(
            "utf-8"
        )
    )
    repository = SQLiteOutcomeV3Repository(
        connection,
        clock=SequenceClock(),
    )
    _executing(repository)

    connection.execute("BEGIN")
    completed = repository.complete_session(_request())
    assert connection.in_transaction is True
    assert repository.get_session(completed.session.session_id).status == "completed"
    connection.rollback()

    assert repository.get_session("gate_session_001").status == "executing"
    with pytest.raises(SQLiteOutcomeV3NotFoundError):
        repository.get_outcome(completed.outcome.run_outcome_id)
    repository.close()
    connection.close()


def test_invalid_outcome_insert_rolls_back_completed_session(
    monkeypatch: pytest.MonkeyPatch,
):
    with _repository() as repository:
        _executing(repository)
        original = SQLiteOutcomeV3Repository._outcome_row

        def invalid_row(outcome):
            row = list(original(outcome))
            row[-1] = " " + row[-1]
            return tuple(row)

        monkeypatch.setattr(
            SQLiteOutcomeV3Repository,
            "_outcome_row",
            staticmethod(invalid_row),
        )
        with pytest.raises(SQLiteOutcomeV3PersistenceError):
            repository.complete_session(_request())

        assert repository.get_session("gate_session_001").status == "executing"
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM v3_run_outcomes"
        ).fetchone()
        assert count == (0,)


def test_readback_mismatch_rolls_back_completed_session(
    monkeypatch: pytest.MonkeyPatch,
):
    with _repository() as repository:
        _executing(repository)
        original = SQLiteOutcomeV3Repository._select_outcome

        def changed(cursor, run_outcome_id):
            stored = original(cursor, run_outcome_id)
            return build_run_outcome(
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

        monkeypatch.setattr(
            SQLiteOutcomeV3Repository,
            "_select_outcome",
            staticmethod(changed),
        )
        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="read-back",
        ):
            repository.complete_session(_request())
        assert repository.get_session("gate_session_001").status == "executing"


def test_sql_guards_descriptor_and_immutable_rows():
    with _repository() as repository:
        _executing(repository)
        result = repository.complete_session(_request())
        connection = repository._connection

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE v3_run_outcomes SET result = 'fail' "
                "WHERE run_outcome_id = ?",
                (result.outcome.run_outcome_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM v3_run_outcomes WHERE run_outcome_id = ?",
                (result.outcome.run_outcome_id,),
            )


def test_direct_noncanonical_descriptor_is_rejected():
    clock = SequenceClock()
    with _repository(clock=clock) as repository:
        _executing(repository)
        measured_at = TIMES[6]
        outcome = build_run_outcome(
            session_id="gate_session_001",
            trace_id="trace_001",
            run_id="run_001",
            usage_decision_id="usage_001",
            result="pass",
            evaluator_id="evaluation_service",
            evaluator_version="1.2.0",
            evidence_artifact_sha256s=(DIGEST_B,),
            measured_at=measured_at,
            output_sha256=DIGEST_A,
        )
        lower_level = SQLiteGateSessionRepository(
            repository._connection,
            clock=lambda: measured_at,
        )
        lower_level.transition(
            "gate_session_001",
            "completed",
            expected_version=6,
            run_outcome_id=outcome.run_outcome_id,
        )
        lower_level.close()
        row = list(repository._outcome_row(outcome))
        row[-1] = row[-1].replace(
            '{"contract_version"',
            '{ "contract_version"',
            1,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid RunOutcome",
        ):
            repository._connection.execute(
                "INSERT INTO v3_run_outcomes ("
                "run_outcome_id, session_id, trace_id, run_id, "
                "usage_decision_id, result, evaluator_id, "
                "evaluator_version, output_sha256, tool_outputs_sha256, "
                "evidence_artifact_sha256s_json, latency_ms, "
                "cost_usd_json, error_code, measured_at, descriptor"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row),
            )


def test_schema_drift_and_missing_schema_fail_closed():
    with _repository() as repository:
        _executing(repository)
        repository._connection.execute(
            "CREATE INDEX unexpected_outcome_index "
            "ON v3_run_outcomes(result)"
        )
        with pytest.raises(SQLiteOutcomeV3SchemaError, match="unexpected"):
            repository.complete_session(_request())

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteOutcomeV3Repository(connection)
    with pytest.raises(SQLiteOutcomeV3SchemaError):
        repository.get_outcome("run_outcome_sha256_" + "f" * 64)
    repository.close()
    connection.close()

    with _repository() as repository:
        repository._connection.execute(
            "DROP TRIGGER v3_run_outcomes_immutable_update"
        )
        with pytest.raises(SQLiteOutcomeV3SchemaError):
            repository.get_outcome(
                "run_outcome_sha256_" + "f" * 64
            )


def test_gate_session_dependency_drift_is_mapped_to_outcome_schema_error():
    with _repository() as repository:
        repository._connection.execute(
            "DROP TRIGGER gate_session_revisions_immutable_update"
        )
        with pytest.raises(
            SQLiteOutcomeV3SchemaError,
            match="dependency",
        ):
            repository.get_outcome(
                "run_outcome_sha256_" + "f" * 64
            )


def test_schema_metadata_and_required_pragmas_fail_closed():
    with _repository() as repository:
        repository._connection.execute(
            "DELETE FROM trace_backed_memory_v3_outcome_schema"
        )
        with pytest.raises(SQLiteOutcomeV3SchemaError, match="metadata"):
            repository.get_outcome("run_outcome_sha256_" + "f" * 64)

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN")
    with pytest.raises(
        SQLiteOutcomeV3PersistenceError,
        match="foreign keys",
    ):
        SQLiteOutcomeV3Repository(connection)
    connection.close()


def test_closed_repository_and_missing_outcome_have_stable_errors():
    repository = _repository()
    with pytest.raises(SQLiteOutcomeV3NotFoundError) as missing:
        repository.get_outcome("run_outcome_sha256_" + "f" * 64)
    assert missing.value.code == "TBM_SQLITE_OUTCOME_NOT_FOUND"
    repository.close()
    with pytest.raises(SQLiteOutcomeV3PersistenceError) as closed:
        repository.get_outcome("run_outcome_sha256_" + "f" * 64)
    assert closed.value.code == "TBM_SQLITE_OUTCOME_CLOSED"
    repository.close()


def test_invalid_repository_arguments_and_lookups_are_rejected():
    with pytest.raises(ValueError, match="connection"):
        SQLiteOutcomeV3Repository(object())  # type: ignore[arg-type]
    connection = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="clock"):
        SQLiteOutcomeV3Repository(
            connection,
            clock=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="allow_direct_completion"):
        SQLiteGateSessionRepository(
            connection,
            allow_direct_completion=1,  # type: ignore[arg-type]
        )
    connection.close()
    with pytest.raises(ValueError, match="initialize"):
        SQLiteOutcomeV3Repository.connect(
            ":memory:",
            initialize=1,  # type: ignore[arg-type]
        )
    with pytest.raises(SQLiteOutcomeV3PersistenceError):
        SQLiteOutcomeV3Repository.connect(object())  # type: ignore[arg-type]

    with _repository() as repository:
        with pytest.raises(TypeError, match="GateCompletionRequest"):
            repository.complete_session(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="session_id"):
            repository.get_session(1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="run_outcome_id"):
            repository.get_outcome(1)  # type: ignore[arg-type]
        with pytest.raises(SQLiteOutcomeV3NotFoundError) as missing:
            repository.get_session("missing")
        assert missing.value.code == "TBM_SQLITE_OUTCOME_SESSION_NOT_FOUND"
        with pytest.raises(SQLiteOutcomeV3NotFoundError):
            repository.complete_session(
                _request(session_id="missing")
            )

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = OFF")
    connection.execute("BEGIN")
    with pytest.raises(
        SQLiteOutcomeV3PersistenceError,
        match="recursive triggers",
    ):
        SQLiteOutcomeV3Repository(connection)
    connection.close()


def test_default_clock_and_externally_closed_connection_are_defensive():
    assert sqlite_outcome_module._service_timestamp().endswith("Z")
    repository = _repository()
    repository._connection.close()
    with pytest.raises(
        SQLiteOutcomeV3PersistenceError,
        match="closed",
    ):
        repository.gate_sessions


def test_invalid_clock_is_rejected_without_partial_completion():
    clock = SequenceClock(TIMES[:6] + ("not-a-time",))
    with _repository(clock=clock) as repository:
        _executing(repository)
        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="invalid timestamp",
        ):
            repository.complete_session(_request())
        assert repository.get_session("gate_session_001").status == "executing"


def test_defensive_row_validation_rejects_shape_descriptor_and_columns():
    with _repository() as repository:
        _executing(repository)
        result = repository.complete_session(_request())
        row = repository._outcome_row(result.outcome)

        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="invalid shape",
        ):
            repository._outcome_from_row(row[:-1])
        invalid_descriptor = list(row)
        invalid_descriptor[-1] = "{}"
        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="contract validation",
        ):
            repository._outcome_from_row(tuple(invalid_descriptor))
        mismatched_column = list(row)
        mismatched_column[6] = "another_evaluator"
        with pytest.raises(
            SQLiteOutcomeV3PersistenceError,
            match="columns",
        ):
            repository._outcome_from_row(tuple(mismatched_column))


def test_private_schema_helpers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(SQLiteOutcomeV3SchemaError):
        sqlite_outcome_module._normalized_schema_sql(None)
    with pytest.raises(SQLiteOutcomeV3SchemaError):
        SQLiteOutcomeV3Repository._raise_database_error(
            sqlite3.OperationalError("no such table: v3_run_outcomes"),
            "ignored",
        )
    with pytest.raises(SQLiteOutcomeV3PersistenceError):
        SQLiteOutcomeV3Repository._raise_database_error(
            sqlite3.OperationalError("database is busy"),
            "bounded failure",
        )

    sqlite_outcome_module._canonical_schema_definitions.cache_clear()
    monkeypatch.setattr(
        sqlite_outcome_module,
        "read_packaged_resource",
        lambda _name: (_ for _ in ()).throw(OSError("missing")),
    )
    with pytest.raises(
        SQLiteOutcomeV3SchemaError,
        match="canonical",
    ):
        sqlite_outcome_module._canonical_schema_definitions()
    sqlite_outcome_module._canonical_schema_definitions.cache_clear()


def test_concurrent_exact_completion_has_one_insert(tmp_path: Path):
    database = tmp_path / "outcome.sqlite3"
    setup = _repository(database, clock=SequenceClock())
    _executing(setup)
    setup.close()
    first = SQLiteOutcomeV3Repository.connect(
        database,
        clock=lambda: TIMES[6],
        timeout=5,
        check_same_thread=False,
    )
    second = SQLiteOutcomeV3Repository.connect(
        database,
        clock=lambda: TIMES[6],
        timeout=5,
        check_same_thread=False,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda repository: repository.complete_session(_request()),
                (first, second),
            )
        )

    assert sorted(result.inserted for result in results) == [False, True]
    assert results[0].session == results[1].session
    assert results[0].outcome == results[1].outcome
    first.close()
    second.close()


@dataclass
class InvalidAuthority:
    result: object

    def complete_session(self, request):
        return self.result

    def get_session(self, session_id):
        raise AssertionError("invalid receipt must fail before read-back")

    def get_outcome(self, run_outcome_id):
        raise AssertionError("invalid receipt must fail before read-back")


def test_completion_service_rejects_invalid_authority_receipt():
    service = GateSessionCompletionService(InvalidAuthority(result=object()))
    with pytest.raises(
        GateCompletionV3Error,
        match="invalid receipt",
    ):
        service.complete(_request())


@dataclass
class ReceiptAuthority:
    result: GateCompletionResult
    completion_error: Exception | None = None
    read_error: Exception | None = None
    retained_session: object | None = None
    retained_outcome: object | None = None

    def complete_session(self, request):
        if self.completion_error is not None:
            raise self.completion_error
        return self.result

    def get_session(self, session_id):
        if self.read_error is not None:
            raise self.read_error
        return self.retained_session or self.result.session

    def get_outcome(self, run_outcome_id):
        if self.read_error is not None:
            raise self.read_error
        return self.retained_outcome or self.result.outcome


def _completed_result() -> GateCompletionResult:
    with _repository() as repository:
        _executing(repository)
        return repository.complete_session(_request())


def test_completion_service_maps_authority_and_readback_failures():
    result = _completed_result()
    stable = GateCompletionV3Error("TBM_TEST", "stable")
    with pytest.raises(GateCompletionV3Error) as reraised:
        GateSessionCompletionService(
            ReceiptAuthority(result, completion_error=stable)
        ).complete(_request())
    assert reraised.value is stable

    with pytest.raises(GateCompletionV3Error) as failed:
        GateSessionCompletionService(
            ReceiptAuthority(result, completion_error=RuntimeError("boom"))
        ).complete(_request())
    assert failed.value.code == "TBM_GATE_COMPLETION_FAILED"

    with pytest.raises(GateCompletionV3Error) as unreadable:
        GateSessionCompletionService(
            ReceiptAuthority(result, read_error=RuntimeError("boom"))
        ).complete(_request())
    assert unreadable.value.code == "TBM_GATE_COMPLETION_READBACK_FAILED"

    changed = replace(result.outcome, run_outcome_id=result.outcome.run_outcome_id)
    with pytest.raises(GateCompletionV3Error) as mismatch:
        GateSessionCompletionService(
            ReceiptAuthority(
                result,
                retained_outcome=replace(
                    changed,
                    evaluator_version=changed.evaluator_version,
                ),
                retained_session=replace(
                    result.session,
                    version=result.session.version + 1,
                ),
            )
        ).complete(_request())
    assert mismatch.value.code == "TBM_GATE_COMPLETION_READBACK_INVALID"


def test_completion_service_rejects_wrong_type_and_inconsistent_linkage():
    service = GateSessionCompletionService(
        InvalidAuthority(result=object())
    )
    with pytest.raises(GateCompletionV3Error, match="exactly"):
        service.complete(object())  # type: ignore[arg-type]

    result = _completed_result()
    inconsistent = GateCompletionResult(
        session=replace(
            result.session,
            run_outcome_id="run_outcome_sha256_" + "f" * 64,
        ),
        outcome=result.outcome,
        inserted=False,
    )
    with pytest.raises(
        GateCompletionV3Error,
        match="inconsistent",
    ):
        GateSessionCompletionService(
            ReceiptAuthority(inconsistent)
        ).complete(_request())


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": ""},
        {"expected_version": 0},
        {"evidence_artifact_sha256s": ()},
        {"result": "unknown"},
        {"cost_usd": float("inf")},
    ],
)
def test_completion_request_is_strict(changes):
    with pytest.raises(GateCompletionV3Error):
        _request(**changes)


def test_resource_and_package_root_exports_are_published():
    assert SQLITE_OUTCOME_V3_SCHEMA_VERSION == 1
    assert (
        read_packaged_resource("schemas/sqlite-v3-outcome.sql")
        == (Path("schemas") / "sqlite-v3-outcome.sql").read_bytes()
    )
    assert tbm.GateSessionCompletionService is GateSessionCompletionService
    assert tbm.SQLiteOutcomeV3Repository is SQLiteOutcomeV3Repository
    assert tbm.SQLITE_OUTCOME_V3_SCHEMA_VERSION == 1
