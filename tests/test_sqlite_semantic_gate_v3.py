from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3Repository,
)
from trace_backed_memory.sqlite_semantic_gate_v3 import (
    SQLiteSemanticGateV3ConflictError,
    SQLiteSemanticGateV3Error,
    SQLiteSemanticGateV3NotFoundError,
    SQLiteSemanticGateV3PersistenceError,
    SQLiteSemanticGateV3Repository,
    SQLiteSemanticGateV3SchemaError,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_SCHEMA = ROOT / "schemas" / "sqlite-v3-gate-evidence.sql"
SEMANTIC_SCHEMA = ROOT / "schemas" / "sqlite-v3-semantic-gate.sql"


def _records() -> tuple[
    tbm.RetrievalSnapshot,
    tbm.SystemGateEvaluation,
    tbm.SemanticGateAttempt,
]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    attempt = tbm.loads_semantic_gate_attempt(
        (
            ROOT / "examples" / "semantic_gate_attempt_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation, attempt


def _next_attempt(
    parent: tbm.SemanticGateAttempt,
    *,
    request_id: str = "provider_request_002",
) -> tbm.SemanticGateAttempt:
    values = {
        key: value
        for key, value in parent.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        sequence=parent.sequence + 1,
        previous_attempt_id=parent.attempt_id,
        provider_request_id=request_id,
        decision_id=f"decision_{parent.sequence + 1:03d}",
        started_at="2026-07-27T08:03:00Z",
        finished_at="2026-07-27T08:03:01Z",
    )
    return tbm.build_semantic_gate_attempt(**values)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(GATE_SCHEMA.read_text(encoding="utf-8"))
    connection.executescript(SEMANTIC_SCHEMA.read_text(encoding="utf-8"))
    return connection


def _repository() -> SQLiteSemanticGateV3Repository:
    snapshot, evaluation, _attempt = _records()
    connection = _connection()
    SQLiteGateEvidenceV3Repository(connection).store_bundle(
        snapshot,
        evaluation,
    )
    return SQLiteSemanticGateV3Repository(
        connection,
        owns_connection=True,
    )


def _seed_database(path: Path) -> None:
    snapshot, evaluation, _attempt = _records()
    with SQLiteSemanticGateV3Repository.connect(
        path,
        initialize=True,
    ):
        pass
    with SQLiteGateEvidenceV3Repository.connect(path) as evidence:
        evidence.store_bundle(snapshot, evaluation)


def test_sqlite_semantic_gate_stores_chain_and_exact_replay() -> None:
    _snapshot, evaluation, first = _records()
    second = _next_attempt(first)
    repository = _repository()
    try:
        first_result = repository.store_attempt(first)
        replay = repository.store_attempt(first)
        second_result = repository.store_attempt(second)

        assert first_result.inserted is True
        assert replay.inserted is False
        assert second_result.inserted is True
        assert repository.load_attempt(first.attempt_id) == first
        assert repository.load_attempt(second.attempt_id) == second
        assert repository.load_chain(evaluation.evaluation_id) == (
            first,
            second,
        )
    finally:
        repository.close()


def test_sqlite_semantic_gate_rejects_forked_sequence() -> None:
    _snapshot, evaluation, first = _records()
    accepted = _next_attempt(first, request_id="provider_request_accepted")
    fork = _next_attempt(first, request_id="provider_request_fork")
    repository = _repository()
    try:
        repository.store_attempt(first)
        repository.store_attempt(accepted)
        with pytest.raises(
            SQLiteSemanticGateV3ConflictError,
            match="extend",
        ):
            repository.store_attempt(fork)
        assert repository.load_chain(evaluation.evaluation_id) == (
            first,
            accepted,
        )
    finally:
        repository.close()


def test_sqlite_semantic_gate_concurrent_exact_replay(tmp_path: Path) -> None:
    database = tmp_path / "semantic-exact.sqlite3"
    _seed_database(database)
    _snapshot, _evaluation, attempt = _records()

    def append() -> bool:
        with SQLiteSemanticGateV3Repository.connect(
            database,
            timeout=5,
        ) as repository:
            return repository.store_attempt(attempt).inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: append(), range(2)))
    assert sorted(results) == [False, True]


def test_sqlite_semantic_gate_concurrent_fork_has_one_head(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-fork.sqlite3"
    _seed_database(database)
    _snapshot, evaluation, first = _records()
    with SQLiteSemanticGateV3Repository.connect(database) as repository:
        repository.store_attempt(first)
    attempts = (
        _next_attempt(first, request_id="provider_request_a"),
        _next_attempt(first, request_id="provider_request_b"),
    )
    barrier = Barrier(2)

    def append(attempt: tbm.SemanticGateAttempt) -> str:
        barrier.wait()
        try:
            with SQLiteSemanticGateV3Repository.connect(
                database,
                timeout=5,
            ) as repository:
                repository.store_attempt(attempt)
            return "stored"
        except SQLiteSemanticGateV3ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, attempts))
    assert sorted(results) == ["conflict", "stored"]
    with SQLiteSemanticGateV3Repository.connect(database) as repository:
        chain = repository.load_chain(evaluation.evaluation_id)
        assert len(chain) == 2
        assert chain[0] == first
        assert chain[1] in attempts


def test_sqlite_semantic_gate_preserves_caller_transaction() -> None:
    _snapshot, _evaluation, attempt = _records()
    repository = _repository()
    connection = repository._connection
    try:
        connection.execute("BEGIN")
        repository.store_attempt(attempt)
        assert connection.in_transaction is True
        connection.rollback()
        with pytest.raises(SQLiteSemanticGateV3NotFoundError):
            repository.load_attempt(attempt.attempt_id)
    finally:
        repository.close()


def test_sqlite_semantic_gate_detects_schema_drift() -> None:
    _snapshot, _evaluation, attempt = _records()
    connection = _connection()
    SQLiteGateEvidenceV3Repository(connection).store_bundle(
        *_records()[:2],
    )
    connection.execute("DROP TRIGGER v3_semantic_gate_attempts_extend_head")
    repository = SQLiteSemanticGateV3Repository(
        connection,
        owns_connection=True,
    )
    try:
        with pytest.raises(SQLiteSemanticGateV3SchemaError):
            repository.store_attempt(attempt)
    finally:
        repository.close()


def test_sqlite_semantic_gate_detects_restored_descriptor_tamper() -> None:
    _snapshot, _evaluation, attempt = _records()
    repository = _repository()
    connection = repository._connection
    try:
        repository.store_attempt(attempt)
        connection.execute(
            "DROP TRIGGER v3_semantic_gate_attempts_immutable_update"
        )
        connection.execute(
            "UPDATE v3_semantic_gate_attempts SET descriptor = '{}' "
            "WHERE attempt_id = ?",
            (attempt.attempt_id,),
        )
        connection.executescript(SEMANTIC_SCHEMA.read_text(encoding="utf-8"))
        with pytest.raises(
            SQLiteSemanticGateV3PersistenceError,
            match="descriptor",
        ):
            repository.load_attempt(attempt.attempt_id)
    finally:
        repository.close()


def test_sqlite_semantic_gate_direct_writes_are_immutable() -> None:
    _snapshot, _evaluation, attempt = _records()
    repository = _repository()
    connection = repository._connection
    try:
        repository.store_attempt(attempt)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE v3_semantic_gate_attempts SET status = 'failed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM v3_semantic_gate_attempt_heads")
    finally:
        repository.close()


def test_sqlite_semantic_gate_replace_cannot_bypass_immutability() -> None:
    _snapshot, _evaluation, attempt = _records()
    repository = _repository()
    connection = repository._connection
    try:
        repository.store_attempt(attempt)
        head = connection.execute(
            "SELECT system_gate_evaluation_id, session_id, "
            "retrieval_snapshot_id, current_sequence, current_attempt_id "
            "FROM v3_semantic_gate_attempt_heads"
        ).fetchone()
        stored_attempt = connection.execute(
            "SELECT attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor "
            "FROM v3_semantic_gate_attempts"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "INSERT OR REPLACE INTO v3_semantic_gate_attempt_heads "
                "VALUES (?, ?, ?, ?, ?)",
                head,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="immutable|does not extend",
        ):
            connection.execute(
                "INSERT OR REPLACE INTO v3_semantic_gate_attempts "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stored_attempt,
            )
    finally:
        repository.close()


def test_sqlite_semantic_gate_replace_guards_survive_recursive_off() -> None:
    _snapshot, _evaluation, attempt = _records()
    repository = _repository()
    connection = repository._connection
    try:
        repository.store_attempt(attempt)
        head = list(
            connection.execute(
                "SELECT system_gate_evaluation_id, session_id, "
                "retrieval_snapshot_id, current_sequence, current_attempt_id "
                "FROM v3_semantic_gate_attempt_heads"
            ).fetchone()
        )
        head[3] = 2
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "INSERT OR REPLACE INTO v3_semantic_gate_attempt_heads "
                "VALUES (?, ?, ?, ?, ?)",
                head,
            )
        stored_attempt = connection.execute(
            "SELECT attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor "
            "FROM v3_semantic_gate_attempts"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "INSERT OR REPLACE INTO v3_semantic_gate_attempts "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stored_attempt,
            )
        connection.execute("PRAGMA recursive_triggers = ON")
        assert repository.load_attempt(attempt.attempt_id) == attempt
    finally:
        repository.close()


def test_sqlite_semantic_gate_requires_gate_evidence_schema() -> None:
    connection = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        connection.executescript(SEMANTIC_SCHEMA.read_text(encoding="utf-8"))
    assert connection.in_transaction is True
    connection.rollback()
    assert connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE name = 'trace_backed_memory_v3_semantic_gate_schema'"
    ).fetchone() is None
    connection.close()


@pytest.mark.parametrize(
    ("pragma", "message"),
    (
        ("foreign_keys", "requires foreign keys"),
        ("recursive_triggers", "requires recursive triggers"),
    ),
)
def test_sqlite_semantic_gate_rejects_disabled_required_pragma(
    pragma: str,
    message: str,
) -> None:
    _snapshot, _evaluation, attempt = _records()
    repository = _repository()
    connection = repository._connection
    try:
        connection.execute(f"PRAGMA {pragma} = OFF")
        with pytest.raises(SQLiteSemanticGateV3SchemaError, match=message):
            repository.store_attempt(attempt)
    finally:
        repository.close()


def test_sqlite_semantic_gate_failed_nested_append_preserves_outer_work() -> None:
    _snapshot, evaluation, first = _records()
    accepted = _next_attempt(first, request_id="provider_request_accepted")
    fork = _next_attempt(first, request_id="provider_request_fork")
    repository = _repository()
    connection = repository._connection
    try:
        repository.store_attempt(first)
        connection.execute("CREATE TABLE caller_work (value TEXT NOT NULL)")
        connection.commit()
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO caller_work (value) VALUES ('preserved')"
        )
        repository.store_attempt(accepted)
        with pytest.raises(
            SQLiteSemanticGateV3ConflictError,
            match="extend",
        ):
            repository.store_attempt(fork)
        assert connection.in_transaction is True
        assert connection.execute(
            "SELECT value FROM caller_work"
        ).fetchall() == [("preserved",)]
        assert repository.load_chain(evaluation.evaluation_id) == (
            first,
            accepted,
        )
        connection.rollback()
        assert repository.load_chain(evaluation.evaluation_id) == (first,)
    finally:
        repository.close()


def test_sqlite_semantic_gate_rejects_invalid_inputs_and_bounds() -> None:
    _snapshot, evaluation, first = _records()
    oversized = _next_attempt(first)
    values = {
        key: value
        for key, value in oversized.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        sequence=101,
        previous_attempt_id=first.attempt_id,
    )
    oversized = tbm.build_semantic_gate_attempt(**values)
    repository = _repository()
    try:
        with pytest.raises(ValueError, match="exactly SemanticGateAttempt"):
            repository.store_attempt(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="ledger bound"):
            repository.store_attempt(oversized)
        with pytest.raises(ValueError, match="attempt_id"):
            repository.load_attempt("invalid")
        with pytest.raises(ValueError, match="evaluation_id"):
            repository.load_chain("invalid")
        with pytest.raises(SQLiteSemanticGateV3NotFoundError):
            repository.load_chain(evaluation.evaluation_id)
    finally:
        repository.close()
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        SQLiteSemanticGateV3Repository(object())  # type: ignore[arg-type]


def test_sqlite_semantic_gate_public_exports_and_resource_copy() -> None:
    assert (
        tbm.SQLiteSemanticGateV3Repository
        is SQLiteSemanticGateV3Repository
    )
    assert tbm.SQLITE_SEMANTIC_GATE_V3_SCHEMA_VERSION == 1
    assert tbm.read_packaged_resource(
        "schemas/sqlite-v3-semantic-gate.sql"
    ) == SEMANTIC_SCHEMA.read_bytes()
    assert "SQLiteSemanticGateV3Repository" in tbm.__all__


def test_sqlite_semantic_gate_close_is_idempotent() -> None:
    repository = SQLiteSemanticGateV3Repository.connect(initialize=True)
    repository.close()
    repository.close()
    with pytest.raises(SQLiteSemanticGateV3Error, match="closed"):
        repository.load_attempt("semantic_attempt_sha256_" + "0" * 64)
