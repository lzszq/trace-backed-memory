from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import trace_backed_memory.sqlite as sqlite_adapter

from trace_backed_memory import (
    FailureCase,
    Lesson,
    MemoryContext,
    MemoryDecision,
    ProjectPolicy,
    SQLiteAdapterError,
    SQLiteConflictError,
    SQLiteMemoryRepository,
    SQLiteSchemaError,
    SQLiteSyncCounts,
    Trace,
    TraceBackedMemoryStore,
    packaged_resources,
    read_packaged_resource,
)


ROOT = Path(__file__).resolve().parents[1]


def _complete_store() -> TraceBackedMemoryStore:
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_sqlite",
            run_id="run_sqlite",
            commit_sha="commit_sqlite",
            repo="repo_sqlite",
            tenant="tenant_sqlite",
            branch="main",
            eval_suite="suite_sqlite",
            input_hash="sha256:sqlite-input",
            retrieved_context=[{"source": "docs", "rank": 1}],
            tool_calls=[
                {"name": "search_docs", "arguments": {"query": "sqlite"}}
            ],
            tool_outputs=[{"error": "query was empty"}],
            eval_result="fail",
            latency_ms=125,
            cost_usd=0.125,
            error="search failed",
            created_at="2026-07-23T01:00:00Z",
        )
    )
    store.add_failure_case(
        FailureCase(
            case_id="case_sqlite",
            source_trace_id="trace_sqlite",
            commit_sha="commit_sqlite",
            failure_type="invalid_tool_argument",
            symptom="search_docs received an empty query",
            root_cause="the prompt omitted the query contract",
            fix="require a non-empty query",
            fix_commit_sha="fix_sqlite",
            regression_passed=True,
            reviewed_by="sqlite-reviewer",
            review_notes="verified with the SQLite regression suite",
            reviewed_at="2026-07-23T02:00:00Z",
            status="verified",
            created_at="2026-07-23T01:10:00Z",
        )
    )
    store.add_lesson(
        Lesson(
            lesson_id="lesson_sqlite",
            source_case_id="case_sqlite",
            lesson_text="Always provide a non-empty search query.",
            memory_type="procedural",
            scope={"repo": "repo_sqlite", "tenant": "tenant_sqlite"},
            confidence=0.9,
            status="active",
            created_at="2026-07-23T02:10:00Z",
        )
    )
    store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_sqlite",
            policy_text="Validate tool arguments before execution.",
            scope={"repo": "repo_sqlite", "tenant": "tenant_sqlite"},
            confidence=0.8,
            status="active",
            created_at="2026-07-23T02:20:00Z",
        )
    )
    store.log_decision(
        "run_sqlite",
        MemoryContext(
            mode="repair",
            repo="repo_sqlite",
            tenant="tenant_sqlite",
            commit_sha="commit_sqlite",
            eval_suite="suite_sqlite",
            input_hash="sha256:sqlite-input",
        ),
        ["lesson_sqlite", "policy_sqlite"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_sqlite", "policy_sqlite"],
            blocked_memory_ids=[],
            reason="Both records apply to the current repair.",
            risk="low",
            recommended_injection="short_summary",
        ),
        eval_result="fail",
    )
    return store


def _pending_store() -> TraceBackedMemoryStore:
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_pending_sqlite",
            run_id="run_pending_sqlite",
            commit_sha="commit_pending_sqlite",
            repo="repo_sqlite",
            tenant="tenant_sqlite",
            eval_result="unknown",
            created_at="2026-07-23T03:00:00Z",
        )
    )
    store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_pending_sqlite",
            policy_text="Validate every tool argument.",
            scope={"repo": "repo_sqlite", "tenant": "tenant_sqlite"},
            created_at="2026-07-23T03:10:00Z",
        )
    )
    store.log_decision(
        "run_pending_sqlite",
        MemoryContext(
            mode="repair",
            repo="repo_sqlite",
            tenant="tenant_sqlite",
            commit_sha="commit_pending_sqlite",
        ),
        ["policy_pending_sqlite"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["policy_pending_sqlite"],
            blocked_memory_ids=[],
            reason="The policy applies.",
            risk="low",
            recommended_injection="short_summary",
        ),
    )
    return store


def _apply_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        (ROOT / "schemas" / "sqlite.sql").read_text(encoding="utf-8")
    )


def test_sqlite_schema_resource_and_public_docs_are_published():
    canonical = (ROOT / "schemas" / "sqlite.sql").read_bytes()

    assert read_packaged_resource("schemas/sqlite.sql") == canonical
    assert "schemas/sqlite.sql" in {
        resource.name for resource in packaged_resources()
    }

    for relative_path in (
        "README.md",
        "README.zh-CN.md",
        "docs/architecture.md",
        "docs/architecture.zh-CN.md",
        "docs/product.en.md",
        "docs/product.md",
        "docs/usage-policy.md",
        "docs/usage-policy.zh-CN.md",
    ):
        document = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "SQLiteMemoryRepository" in document
        assert "schemas/sqlite.sql" in document


def test_sqlite_repository_initializes_and_loads_an_empty_store(tmp_path: Path):
    database = tmp_path / "memory.sqlite3"

    with SQLiteMemoryRepository.connect(database, initialize=True) as repository:
        loaded = repository.load()

    assert loaded.to_snapshot() == {
        "snapshot_version": 2,
        "traces": [],
        "failure_cases": [],
        "lessons": [],
        "project_policies": [],
        "usage_logs": [],
    }


def test_sqlite_repository_round_trips_and_reports_idempotent_counts(
    tmp_path: Path,
):
    source = _complete_store()
    expected = source.to_snapshot()

    with SQLiteMemoryRepository.connect(
        tmp_path / "memory.sqlite3", initialize=True
    ) as repository:
        first = repository.sync(source)
        loaded = repository.load()
        second = repository.sync(source)

    assert first.traces == SQLiteSyncCounts(inserted=1)
    assert first.failure_cases == SQLiteSyncCounts(inserted=1)
    assert first.lessons == SQLiteSyncCounts(inserted=1)
    assert first.project_policies == SQLiteSyncCounts(inserted=1)
    assert first.usage_logs == SQLiteSyncCounts(inserted=1)
    assert loaded.to_snapshot() == expected
    assert second.traces == SQLiteSyncCounts(unchanged=1)
    assert second.failure_cases == SQLiteSyncCounts(unchanged=1)
    assert second.lessons == SQLiteSyncCounts(unchanged=1)
    assert second.project_policies == SQLiteSyncCounts(unchanged=1)
    assert second.usage_logs == SQLiteSyncCounts(unchanged=1)


def test_sqlite_repository_sync_is_additive(tmp_path: Path):
    source = _complete_store()

    with SQLiteMemoryRepository.connect(
        tmp_path / "memory.sqlite3", initialize=True
    ) as repository:
        repository.sync(source)
        result = repository.sync(TraceBackedMemoryStore())
        loaded = repository.load()

    assert result.traces == SQLiteSyncCounts()
    assert result.failure_cases == SQLiteSyncCounts()
    assert result.lessons == SQLiteSyncCounts()
    assert result.project_policies == SQLiteSyncCounts()
    assert result.usage_logs == SQLiteSyncCounts()
    assert loaded.to_snapshot() == source.to_snapshot()


def test_sqlite_repository_syncs_forward_lifecycle_updates(tmp_path: Path):
    source = _complete_store()

    with SQLiteMemoryRepository.connect(
        tmp_path / "memory.sqlite3", initialize=True
    ) as repository:
        repository.sync(source)
        source.obsolete_failure_case("case_sqlite")
        source.obsolete_project_policy("policy_sqlite")

        result = repository.sync(source)
        loaded = repository.load()

    assert result.failure_cases == SQLiteSyncCounts(updated=1)
    assert result.lessons == SQLiteSyncCounts(unchanged=1)
    assert result.project_policies == SQLiteSyncCounts(updated=1)
    assert loaded.to_snapshot() == source.to_snapshot()


def test_sqlite_repository_syncs_trace_and_usage_completion_atomically(
    tmp_path: Path,
):
    source = _pending_store()
    decision_id = source.usage_logs[0].decision_id

    with SQLiteMemoryRepository.connect(
        tmp_path / "memory.sqlite3", initialize=True
    ) as repository:
        repository.sync(source)
        source.complete_memory_run(
            trace_id="trace_pending_sqlite",
            decision_id=decision_id,
            eval_result="pass",
            output_hash="sha256:sqlite-output",
            tool_outputs=[{"result": "ok"}],
            latency_ms=50,
            cost_usd=0.05,
        )

        result = repository.sync(source)
        loaded = repository.load()

    assert result.traces == SQLiteSyncCounts(updated=1)
    assert result.usage_logs == SQLiteSyncCounts(updated=1)
    assert loaded.to_snapshot() == source.to_snapshot()


def test_sqlite_repository_rolls_back_earlier_rows_on_immutable_conflict(
    tmp_path: Path,
):
    baseline = _complete_store()
    incoming = TraceBackedMemoryStore()
    incoming.record_trace(
        Trace(
            trace_id="aaa_inserted_before_conflict",
            run_id="run_new_sqlite",
            commit_sha="commit_new_sqlite",
            eval_result="unknown",
        )
    )
    incoming.record_trace(
        Trace(
            trace_id="trace_sqlite",
            run_id="run_sqlite",
            commit_sha="different_commit",
            eval_result="fail",
        )
    )

    with SQLiteMemoryRepository.connect(
        tmp_path / "memory.sqlite3", initialize=True
    ) as repository:
        repository.sync(baseline)

        with pytest.raises(SQLiteConflictError, match="immutable conflict"):
            repository.sync(incoming)

        assert repository.load().to_snapshot() == baseline.to_snapshot()


def test_sqlite_repository_rejects_missing_and_mismatched_schema(tmp_path: Path):
    missing = SQLiteMemoryRepository.connect(tmp_path / "missing.sqlite3")
    with missing, pytest.raises(SQLiteSchemaError, match="missing or incomplete"):
        missing.load()

    mismatched_path = tmp_path / "mismatched.sqlite3"
    mismatched = SQLiteMemoryRepository.connect(mismatched_path, initialize=True)
    mismatched.close()
    with sqlite3.connect(mismatched_path) as connection:
        connection.execute(
            "UPDATE trace_backed_memory_schema SET schema_version = 99"
        )
    mismatched = SQLiteMemoryRepository.connect(mismatched_path)
    with mismatched:
        with pytest.raises(SQLiteSchemaError, match="expected 1, found 99"):
            mismatched.load()

    incomplete_path = tmp_path / "incomplete.sqlite3"
    with SQLiteMemoryRepository.connect(
        incomplete_path, initialize=True
    ) as repository:
        repository._connection.execute("DROP TABLE project_policies")
        with pytest.raises(SQLiteSchemaError, match="missing or incomplete"):
            repository.sync(_pending_store())


def test_sqlite_repository_rejects_malformed_payload_and_remains_reusable(
    tmp_path: Path,
):
    database = tmp_path / "malformed.sqlite3"
    with SQLiteMemoryRepository.connect(database, initialize=True) as repository:
        repository._connection.execute(
            "INSERT INTO traces(trace_id, payload) VALUES (?, ?)",
            ("malformed_trace", "{}"),
        )
        repository._connection.commit()

        with pytest.raises(
            sqlite_adapter.SQLitePersistenceError,
            match="failed to load memory store",
        ):
            repository.load()

        repository._connection.execute(
            "DELETE FROM traces WHERE trace_id = ?", ("malformed_trace",)
        )
        repository._connection.commit()
        assert repository.load().to_snapshot()["traces"] == []


def test_sqlite_repository_rejects_excessively_nested_json_payload(
    tmp_path: Path,
):
    database = tmp_path / "deep-payload.sqlite3"
    payload = "[" * 5_000 + "0" + "]" * 5_000
    with SQLiteMemoryRepository.connect(database, initialize=True) as repository:
        repository._connection.execute(
            "INSERT INTO traces(trace_id, payload) VALUES (?, ?)",
            ("deep_trace", payload),
        )
        repository._connection.commit()

        with pytest.raises(
            sqlite_adapter.SQLitePersistenceError,
            match="failed to load memory store",
        ):
            repository.load()

        assert repository._connection.execute("SELECT 1").fetchone() == (1,)


def test_sqlite_repository_rejects_noncanonical_rfc3339_payload(
    tmp_path: Path,
):
    database = tmp_path / "timestamp-payload.sqlite3"
    record = _pending_store().to_snapshot()["traces"][0]
    record["created_at"] = "2026-07-23 03:00:00+00:00"
    with SQLiteMemoryRepository.connect(database, initialize=True) as repository:
        repository._connection.execute(
            "INSERT INTO traces(trace_id, payload) VALUES (?, ?)",
            (record["trace_id"], json.dumps(record)),
        )
        repository._connection.commit()

        with pytest.raises(
            sqlite_adapter.SQLitePersistenceError,
            match="failed to load memory store",
        ):
            repository.load()


def test_sqlite_repository_round_trips_gate_request_id(tmp_path: Path):
    store = _pending_store()
    trace = store.traces["trace_pending_sqlite"]
    context = MemoryContext(
        mode="repair",
        repo="repo_sqlite",
        tenant="tenant_sqlite",
        commit_sha="commit_pending_sqlite",
    )
    request = store.prepare_memory(
        context,
        task="repair",
        trace_id=trace.trace_id,
    )
    store.finalize_memory(
        request,
        {
            "use_memory": True,
            "allowed_memory_ids": ["policy_pending_sqlite"],
            "blocked_memory_ids": [],
            "reason": "The policy applies.",
            "risk": "low",
            "recommended_injection": "short_summary",
        },
        trace_id=trace.trace_id,
    )

    with SQLiteMemoryRepository.connect(
        tmp_path / "request-id.sqlite3",
        initialize=True,
    ) as repository:
        repository.sync(store)
        loaded = repository.load()

    assert loaded.usage_logs[-1].request_id == request.request_id


def test_sqlite_repository_loads_legacy_usage_payload_without_request_id(
    tmp_path: Path,
):
    database = tmp_path / "legacy-request-id.sqlite3"
    with SQLiteMemoryRepository.connect(database, initialize=True) as repository:
        repository.sync(_complete_store())
        decision_id, payload = repository._connection.execute(
            "SELECT decision_id, payload FROM memory_usage_decisions"
        ).fetchone()
        legacy_payload = json.loads(payload)
        legacy_payload.pop("request_id")
        repository._connection.execute(
            "UPDATE memory_usage_decisions SET payload = ? WHERE decision_id = ?",
            (json.dumps(legacy_payload), decision_id),
        )
        repository._connection.commit()

        loaded = repository.load()
        result = repository.sync(loaded)

    assert loaded.usage_logs[0].request_id is None
    assert result.usage_logs.unchanged == 1


def test_sqlite_repository_enforces_payload_budget_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "payload.sqlite3"
    with SQLiteMemoryRepository.connect(database, initialize=True) as repository:
        repository.sync(_pending_store())
        monkeypatch.setattr(sqlite_adapter, "_SQLITE_LOAD_MAX_RECORD_BYTES", 10)
        monkeypatch.setattr(
            sqlite_adapter,
            "_SQLITE_LOAD_MAX_TOTAL_PAYLOAD_BYTES",
            10,
        )

        with pytest.raises(
            sqlite_adapter.SQLitePersistenceError,
            match="failed to load memory store",
        ):
            repository.load()

        assert repository._connection.execute("SELECT 1").fetchone() == (1,)


def test_sqlite_repository_connection_ownership(tmp_path: Path):
    owned = SQLiteMemoryRepository.connect(
        tmp_path / "owned.sqlite3", initialize=True
    )
    owned.close()
    with pytest.raises(SQLiteAdapterError, match="repository is closed"):
        owned.load()

    connection = sqlite3.connect(tmp_path / "borrowed.sqlite3")
    _apply_schema(connection)
    borrowed = SQLiteMemoryRepository(connection)
    borrowed.close()
    assert connection.execute("SELECT 1").fetchone() == (1,)
    connection.close()


def test_sqlite_connect_closes_connection_when_initialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    connection = sqlite3.connect(tmp_path / "initialization.sqlite3")
    monkeypatch.setattr(
        sqlite_adapter.sqlite3,
        "connect",
        lambda *args, **kwargs: connection,
    )
    monkeypatch.setattr(
        sqlite_adapter,
        "read_packaged_resource",
        lambda _name: b"\xff",
    )

    with pytest.raises(
        sqlite_adapter.SQLitePersistenceError,
        match="failed to connect to SQLite",
    ):
        SQLiteMemoryRepository.connect("ignored", initialize=True)

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_sqlite_repository_uses_savepoint_inside_caller_transaction(
    tmp_path: Path,
):
    connection = sqlite3.connect(
        tmp_path / "caller.sqlite3",
        isolation_level=None,
    )
    _apply_schema(connection)
    repository = SQLiteMemoryRepository(connection)

    connection.execute("BEGIN")
    repository.sync(_complete_store())
    connection.execute("ROLLBACK")

    assert repository.load().to_snapshot()["traces"] == []
    repository.close()
    connection.close()


def test_sqlite_repository_savepoint_conflict_preserves_outer_transaction(
    tmp_path: Path,
):
    connection = sqlite3.connect(
        tmp_path / "caller-conflict.sqlite3",
        isolation_level=None,
    )
    _apply_schema(connection)
    connection.execute("CREATE TABLE caller_state(value TEXT NOT NULL)")
    repository = SQLiteMemoryRepository(connection)
    repository.sync(_complete_store())

    conflicting = TraceBackedMemoryStore()
    conflicting.record_trace(
        Trace(
            trace_id="trace_sqlite",
            run_id="run_sqlite",
            commit_sha="different_commit",
            eval_result="fail",
        )
    )
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state(value) VALUES ('kept')")

    with pytest.raises(SQLiteConflictError, match="immutable conflict"):
        repository.sync(conflicting)

    assert connection.in_transaction
    assert connection.execute("SELECT value FROM caller_state").fetchall() == [
        ("kept",)
    ]
    connection.execute("ROLLBACK")
    assert repository.load().to_snapshot() == _complete_store().to_snapshot()
    repository.close()
    connection.close()


def test_sqlite_repository_release_failure_rolls_back_nested_writes(
    tmp_path: Path,
):
    class FailingReleaseConnection(sqlite3.Connection):
        fail_next_release = False

        def execute(self, sql, parameters=(), /):
            if (
                self.fail_next_release
                and sql.upper().startswith("RELEASE SAVEPOINT")
            ):
                self.fail_next_release = False
                raise sqlite3.OperationalError("simulated release failure")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(
        tmp_path / "release-failure.sqlite3",
        isolation_level=None,
        factory=FailingReleaseConnection,
    )
    _apply_schema(connection)
    connection.execute("CREATE TABLE caller_state(value TEXT NOT NULL)")
    repository = SQLiteMemoryRepository(connection)
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state(value) VALUES ('kept')")
    connection.fail_next_release = True

    with pytest.raises(
        sqlite_adapter.SQLitePersistenceError,
        match="failed to sync memory store",
    ):
        repository.sync(_pending_store())

    assert connection.in_transaction
    assert connection.execute("SELECT value FROM caller_state").fetchall() == [
        ("kept",)
    ]
    assert connection.execute("SELECT count(*) FROM traces").fetchone() == (0,)
    connection.execute("INSERT INTO caller_state(value) VALUES ('still-usable')")
    connection.execute("ROLLBACK")
    assert repository.load().to_snapshot()["traces"] == []
    repository.close()
    connection.close()


def test_sqlite_repository_cleanup_release_failure_preserves_primary_conflict(
    tmp_path: Path,
):
    class FailingReleaseConnection(sqlite3.Connection):
        fail_next_release = False

        def execute(self, sql, parameters=(), /):
            if (
                self.fail_next_release
                and sql.upper().startswith("RELEASE SAVEPOINT")
            ):
                self.fail_next_release = False
                raise sqlite3.OperationalError("simulated release failure")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(
        tmp_path / "cleanup-release-failure.sqlite3",
        isolation_level=None,
        factory=FailingReleaseConnection,
    )
    _apply_schema(connection)
    connection.execute("CREATE TABLE caller_state(value TEXT NOT NULL)")
    repository = SQLiteMemoryRepository(connection)
    repository.sync(_complete_store())
    conflicting = TraceBackedMemoryStore()
    conflicting.record_trace(
        Trace(
            trace_id="trace_sqlite",
            run_id="run_sqlite",
            commit_sha="different_commit",
            eval_result="fail",
        )
    )
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state(value) VALUES ('kept')")
    connection.fail_next_release = True

    with pytest.raises(SQLiteConflictError, match="immutable conflict"):
        repository.sync(conflicting)

    assert connection.in_transaction
    assert connection.execute("SELECT value FROM caller_state").fetchall() == [
        ("kept",)
    ]
    connection.execute("INSERT INTO caller_state(value) VALUES ('still-usable')")
    connection.execute("ROLLBACK")
    assert repository.load().to_snapshot() == _complete_store().to_snapshot()
    repository.close()
    connection.close()


def test_sqlite_repository_savepoint_rollback_failure_aborts_outer_transaction(
    tmp_path: Path,
):
    class FailingSavepointCleanupConnection(sqlite3.Connection):
        fail_next_release = False
        fail_next_savepoint_rollback = False

        def execute(self, sql, parameters=(), /):
            normalized = sql.upper()
            if self.fail_next_release and normalized.startswith("RELEASE SAVEPOINT"):
                self.fail_next_release = False
                raise sqlite3.OperationalError("simulated release failure")
            if (
                self.fail_next_savepoint_rollback
                and normalized.startswith("ROLLBACK TO SAVEPOINT")
            ):
                self.fail_next_savepoint_rollback = False
                raise sqlite3.OperationalError("simulated savepoint rollback failure")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(
        tmp_path / "savepoint-rollback-failure.sqlite3",
        isolation_level=None,
        factory=FailingSavepointCleanupConnection,
    )
    _apply_schema(connection)
    connection.execute("CREATE TABLE caller_state(value TEXT NOT NULL)")
    repository = SQLiteMemoryRepository(connection)
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state(value) VALUES ('must-roll-back')")
    connection.fail_next_release = True
    connection.fail_next_savepoint_rollback = True

    with pytest.raises(
        sqlite_adapter.SQLitePersistenceError,
        match="failed to sync memory store",
    ):
        repository.sync(_pending_store())

    assert not connection.in_transaction
    assert connection.execute("SELECT value FROM caller_state").fetchall() == []
    assert connection.execute("SELECT count(*) FROM traces").fetchone() == (0,)
    repository.close()
    connection.close()


def test_sqlite_repository_closes_borrowed_connection_when_nested_rollback_cannot_recover(
    tmp_path: Path,
):
    class UnrecoverableRollbackConnection(sqlite3.Connection):
        fail_next_release = False
        fail_next_savepoint_rollback = False
        rollback_failures_remaining = 0

        def execute(self, sql, parameters=(), /):
            normalized = sql.upper()
            if self.fail_next_release and normalized.startswith("RELEASE SAVEPOINT"):
                self.fail_next_release = False
                raise sqlite3.OperationalError("simulated release failure")
            if (
                self.fail_next_savepoint_rollback
                and normalized.startswith("ROLLBACK TO SAVEPOINT")
            ):
                self.fail_next_savepoint_rollback = False
                raise sqlite3.OperationalError(
                    "simulated savepoint rollback failure"
                )
            return super().execute(sql, parameters)

        def rollback(self) -> None:
            if self.rollback_failures_remaining:
                self.rollback_failures_remaining -= 1
                raise sqlite3.OperationalError("simulated outer rollback failure")
            super().rollback()

    connection = sqlite3.connect(
        tmp_path / "unrecoverable-nested-rollback.sqlite3",
        isolation_level=None,
        factory=UnrecoverableRollbackConnection,
    )
    _apply_schema(connection)
    repository = SQLiteMemoryRepository(connection)
    connection.execute("BEGIN")
    connection.fail_next_release = True
    connection.fail_next_savepoint_rollback = True
    connection.rollback_failures_remaining = 2

    with pytest.raises(
        sqlite_adapter.SQLitePersistenceError,
        match="failed to sync memory store",
    ) as exc_info:
        repository.sync(_pending_store())

    notes = getattr(exc_info.value.__cause__, "__notes__", ())
    assert any("retry failed" in note for note in notes)
    assert repository._closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.commit()


def test_sqlite_repository_rolls_back_after_commit_failure(tmp_path: Path):
    class FailingCommitConnection(sqlite3.Connection):
        fail_next_commit = False

        def commit(self) -> None:
            if self.fail_next_commit:
                self.fail_next_commit = False
                raise sqlite3.OperationalError("simulated commit failure")
            super().commit()

    connection = sqlite3.connect(
        tmp_path / "commit-failure.sqlite3",
        factory=FailingCommitConnection,
    )
    _apply_schema(connection)
    repository = SQLiteMemoryRepository(connection)
    connection.fail_next_commit = True

    with pytest.raises(
        sqlite_adapter.SQLitePersistenceError,
        match="failed to sync memory store",
    ):
        repository.sync(_pending_store())

    assert not connection.in_transaction
    assert repository.load().to_snapshot()["traces"] == []
    repository.close()
    connection.close()


def test_sqlite_repository_top_level_rollback_failure_preserves_primary_error(
    tmp_path: Path,
):
    class FailingRollbackConnection(sqlite3.Connection):
        fail_next_rollback = False

        def rollback(self) -> None:
            if self.fail_next_rollback:
                self.fail_next_rollback = False
                raise sqlite3.OperationalError("simulated rollback failure")
            super().rollback()

    connection = sqlite3.connect(
        tmp_path / "rollback-failure.sqlite3",
        factory=FailingRollbackConnection,
    )
    _apply_schema(connection)
    repository = SQLiteMemoryRepository(connection)
    repository.sync(_complete_store())
    conflicting = TraceBackedMemoryStore()
    conflicting.record_trace(
        Trace(
            trace_id="trace_sqlite",
            run_id="run_sqlite",
            commit_sha="different_commit",
            eval_result="fail",
        )
    )
    connection.fail_next_rollback = True

    with pytest.raises(SQLiteConflictError, match="immutable conflict") as exc_info:
        repository.sync(conflicting)

    assert any(
        "simulated rollback failure" in note
        for note in getattr(exc_info.value, "__notes__", ())
    )
    assert not connection.in_transaction
    assert repository.load().to_snapshot() == _complete_store().to_snapshot()
    repository.close()
    connection.close()


def test_sqlite_repository_serializes_same_instance_sync_and_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = SQLiteMemoryRepository.connect(
        tmp_path / "same-instance.sqlite3",
        initialize=True,
        check_same_thread=False,
    )
    entered = threading.Event()
    release = threading.Event()
    original_sync_collection = repository._sync_collection

    def blocking_sync_collection(*args, **kwargs):
        if kwargs["collection"] == "traces" and not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return original_sync_collection(*args, **kwargs)

    monkeypatch.setattr(repository, "_sync_collection", blocking_sync_collection)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sync_future = pool.submit(repository.sync, _pending_store())
        assert entered.wait(timeout=5)
        load_future = pool.submit(repository.load)
        time.sleep(0.05)
        assert not load_future.done()
        release.set()
        sync_future.result(timeout=5)
        loaded = load_future.result(timeout=5)

    assert len(loaded.traces) == 1
    repository.close()


def test_sqlite_repository_close_waits_for_inflight_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = SQLiteMemoryRepository.connect(
        tmp_path / "close-wait.sqlite3",
        initialize=True,
        check_same_thread=False,
    )
    entered = threading.Event()
    release = threading.Event()
    original_sync_collection = repository._sync_collection

    def blocking_sync_collection(*args, **kwargs):
        if kwargs["collection"] == "traces" and not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return original_sync_collection(*args, **kwargs)

    monkeypatch.setattr(repository, "_sync_collection", blocking_sync_collection)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sync_future = pool.submit(repository.sync, _pending_store())
        assert entered.wait(timeout=5)
        close_future = pool.submit(repository.close)
        time.sleep(0.05)
        assert not close_future.done()
        release.set()
        sync_future.result(timeout=5)
        close_future.result(timeout=5)

    with pytest.raises(SQLiteAdapterError, match="closed"):
        repository.load()


def test_sqlite_timestamp_canonicalizer_rejects_submicrosecond_precision():
    assert sqlite_adapter._canonical_rfc3339(
        "2026-07-23T09:00:00.123456+08:00",
        "created_at",
    ) == "2026-07-23T01:00:00.123456Z"

    with pytest.raises(ValueError, match="created_at"):
        sqlite_adapter._canonical_rfc3339(
            "2026-07-23T01:00:00.1234567Z",
            "created_at",
        )


def test_sqlite_repository_serializes_top_level_writers(tmp_path: Path):
    database = tmp_path / "writer-lock.sqlite3"
    first_connection = sqlite3.connect(database, isolation_level=None)
    _apply_schema(first_connection)

    with SQLiteMemoryRepository.connect(database, timeout=0) as repository:
        first_connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(
                sqlite_adapter.SQLitePersistenceError,
                match="failed to sync memory store",
            ):
                repository.sync(_pending_store())
        finally:
            first_connection.execute("ROLLBACK")

        result = repository.sync(_pending_store())
        assert result.traces == SQLiteSyncCounts(inserted=1)

    first_connection.close()
