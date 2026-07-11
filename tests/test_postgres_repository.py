from __future__ import annotations

import subprocess
import sys
from dataclasses import is_dataclass
from decimal import Decimal
from pathlib import Path

import pytest


def test_repository_rejects_missing_or_unknown_schema(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSchemaError

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        with pytest.raises(PostgresSchemaError, match="schema metadata"):
            repository.load()

        postgres_cluster.load_schema()
        loaded = repository.load()
        assert loaded.to_snapshot() == {
            "snapshot_version": 2,
            "traces": [],
            "failure_cases": [],
            "lessons": [],
            "project_policies": [],
            "usage_logs": [],
        }
        connection.execute(
            "UPDATE public.trace_backed_memory_schema SET schema_version = 2"
        )
        connection.commit()
        with pytest.raises(PostgresSchemaError, match="expected 1, found 2"):
            repository.load()


def test_repository_loads_a_complete_normalized_snapshot(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import PostgresMemoryRepository

    postgres_cluster.load_schema()
    large_integral_cost = Decimal("1" + "0" * 400)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        connection.execute(
            "INSERT INTO public.traces ("
            "trace_id, run_id, commit_sha, repo, dirty, eval_result, "
            "retrieved_context, tool_calls, tool_outputs, cost_usd, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "trace_db",
                "run_db",
                "commit_db",
                "repo_db",
                True,
                "fail",
                Jsonb([{"source": "database"}]),
                Jsonb([{"name": "query"}]),
                Jsonb([{"result": "failure"}]),
                large_integral_cost,
                "2025-01-02T03:04:05+05:30",
            ),
        )
        connection.execute(
            "INSERT INTO public.traces ("
            "trace_id, run_id, commit_sha, repo, eval_result, "
            "retrieved_context, tool_calls, tool_outputs, cost_usd, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "trace_fractional",
                "run_fractional",
                "commit_fractional",
                "repo_fractional",
                "pass",
                Jsonb([]),
                Jsonb([]),
                Jsonb([]),
                Decimal("0.125"),
                "2025-01-02T03:04:05+05:30",
            ),
        )
        connection.execute(
            "INSERT INTO public.failure_cases ("
            "case_id, source_trace_id, commit_sha, failure_type, symptom, fix, "
            "fix_commit_sha, regression_passed, status, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "case_db",
                "trace_db",
                "commit_db",
                "tool_error",
                "query failed",
                "fix query",
                "fix_commit_db",
                True,
                "verified",
                "2025-01-02T03:04:05+05:30",
            ),
        )
        connection.execute(
            "INSERT INTO public.lessons ("
            "lesson_id, source_case_id, lesson_text, memory_type, scope_json, "
            "confidence, status, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "lesson_db",
                "case_db",
                "validate the query before execution",
                "procedural",
                Jsonb({"repo": "repo_db"}),
                Decimal("0.875"),
                "active",
                "2025-01-02T03:04:05+05:30",
            ),
        )
        connection.execute(
            "INSERT INTO public.project_policies ("
            "policy_id, policy_text, scope_json, confidence, status, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s)",
            (
                "policy_db",
                "require validated queries",
                Jsonb({"repo": "repo_db"}),
                Decimal("0.625"),
                "active",
                "2025-01-02T03:04:05+05:30",
            ),
        )
        connection.execute(
            "INSERT INTO public.memory_usage_decisions ("
            "decision_id, run_id, trace_id, mode, candidate_memory_ids, "
            "used_memory_ids, blocked_memory_ids, risk, reason, "
            "recommended_injection, eval_result, memory_caused_failure, context, "
            "candidate_memory_statuses, system_blocked_reasons, created_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "decision_db",
                "run_db",
                "trace_db",
                "debug",
                Jsonb(["lesson_db", "policy_db"]),
                Jsonb(["lesson_db"]),
                Jsonb(["policy_db"]),
                "low",
                "lesson applies",
                "short_summary",
                "pass",
                False,
                Jsonb({"mode": "debug", "repo": "repo_db", "commit_sha": "commit_db"}),
                Jsonb({"lesson_db": "active", "policy_db": "active"}),
                Jsonb({"policy_db": "policy not applicable"}),
                "2025-01-02T03:04:05+05:30",
            ),
        )

        loaded = PostgresMemoryRepository(connection).load()
        snapshot = loaded.to_snapshot()

    assert snapshot["snapshot_version"] == 2
    assert snapshot["traces"][0]["trace_id"] == "trace_db"
    assert snapshot["traces"][0]["cost_usd"] == int(large_integral_cost)
    assert snapshot["traces"][0]["created_at"] == "2025-01-01T21:34:05Z"
    assert snapshot["traces"][1]["cost_usd"] == 0.125
    assert snapshot["failure_cases"][0]["case_id"] == "case_db"
    assert snapshot["lessons"][0]["lesson_id"] == "lesson_db"
    assert snapshot["lessons"][0]["confidence"] == 0.875
    assert snapshot["project_policies"][0]["policy_id"] == "policy_db"
    assert snapshot["project_policies"][0]["confidence"] == 0.625
    assert snapshot["usage_logs"][0]["decision_id"] == "decision_db"
    assert snapshot == TraceBackedMemoryStore.from_snapshot(snapshot).to_snapshot()


@pytest.mark.parametrize(
    "invalid_state",
    ["malformed_json_evidence", "unknown_runtime_id", "provenance_mismatch"],
)
def test_repository_load_rejects_invalid_domain_state(postgres_cluster, invalid_state):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresPersistenceError,
    )

    postgres_cluster.load_schema()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        with connection.transaction():
            connection.execute("SET LOCAL session_replication_role = replica")
            if invalid_state == "malformed_json_evidence":
                malformed_evidence: dict[str, object] = {}
                for _ in range(101):
                    malformed_evidence = {"nested": malformed_evidence}
                connection.execute(
                    "INSERT INTO public.traces ("
                    "trace_id, run_id, commit_sha, retrieved_context"
                    ") VALUES (%s, %s, %s, %s)",
                    (
                        "trace_invalid_json",
                        "run_invalid_json",
                        "commit_invalid_json",
                        Jsonb([malformed_evidence]),
                    ),
                )
            elif invalid_state == "unknown_runtime_id":
                connection.execute(
                    "INSERT INTO public.traces (trace_id, run_id, commit_sha, repo) "
                    "VALUES (%s, %s, %s, %s)",
                    ("trace_unknown", "run_unknown", "commit_unknown", "repo_unknown"),
                )
                connection.execute(
                    "INSERT INTO public.memory_usage_decisions ("
                    "decision_id, run_id, trace_id, mode, candidate_memory_ids, "
                    "used_memory_ids, blocked_memory_ids, risk, reason, "
                    "recommended_injection, context, candidate_memory_statuses, "
                    "system_blocked_reasons"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        "decision_unknown",
                        "run_unknown",
                        "trace_unknown",
                        "debug",
                        Jsonb(["memory_unknown"]),
                        Jsonb([]),
                        Jsonb(["memory_unknown"]),
                        "low",
                        "blocked unknown memory",
                        "none",
                        Jsonb(
                            {
                                "mode": "debug",
                                "repo": "repo_unknown",
                                "commit_sha": "commit_unknown",
                            }
                        ),
                        Jsonb({"memory_unknown": "active"}),
                        Jsonb({"memory_unknown": "unknown memory"}),
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO public.traces (trace_id, run_id, commit_sha) "
                    "VALUES (%s, %s, %s)",
                    ("trace_provenance", "run_provenance", "commit_source"),
                )
                connection.execute(
                    "INSERT INTO public.failure_cases ("
                    "case_id, source_trace_id, commit_sha, failure_type, symptom"
                    ") VALUES (%s, %s, %s, %s, %s)",
                    (
                        "case_provenance",
                        "trace_provenance",
                        "commit_mismatch",
                        "tool_error",
                        "mismatched provenance",
                    ),
                )

        with pytest.raises(PostgresPersistenceError) as error:
            PostgresMemoryRepository(connection).load()

    assert isinstance(error.value.__cause__, ValueError)


def test_postgres_adapter_types_are_publicly_exported():
    from trace_backed_memory import (
        PostgresAdapterError,
        PostgresConflictError,
        PostgresDependencyError,
        PostgresMemoryRepository,
        PostgresPersistenceError,
        PostgresSchemaError,
        PostgresSyncCounts,
        PostgresSyncResult,
    )

    assert issubclass(PostgresDependencyError, PostgresAdapterError)
    assert issubclass(PostgresSchemaError, PostgresAdapterError)
    assert issubclass(PostgresConflictError, PostgresAdapterError)
    assert issubclass(PostgresPersistenceError, PostgresAdapterError)
    assert PostgresMemoryRepository is not None
    assert is_dataclass(PostgresSyncCounts)
    assert is_dataclass(PostgresSyncResult)
    assert PostgresSyncCounts() == PostgresSyncCounts(0, 0, 0)


def test_package_import_does_not_load_psycopg():
    source_path = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(source_path)!r})\n"
        "import trace_backed_memory\n"
        "assert 'psycopg' not in sys.modules\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_missing_postgres_extra_has_stable_error(monkeypatch: pytest.MonkeyPatch):
    from trace_backed_memory import postgres
    from trace_backed_memory.postgres import PostgresDependencyError

    class BlockPsycopgImports:
        def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
            if fullname == "psycopg" or fullname.startswith("psycopg."):
                raise ModuleNotFoundError("No module named 'psycopg'", name=fullname)
            return None

    for module_name in list(sys.modules):
        if module_name == "psycopg" or module_name.startswith("psycopg."):
            monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.setattr(sys, "meta_path", [BlockPsycopgImports(), *sys.meta_path])

    with pytest.raises(
        PostgresDependencyError,
        match="PostgreSQL support requires: pip install 'trace-backed-memory\\[postgres\\]'",
    ):
        postgres._load_psycopg()


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[str] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str) -> None:
        self.executions.append(query)

    def fetchall(self) -> list[dict[str, int]]:
        return [{"schema_version": 1}]


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0
        self.cursor_kwargs: dict[str, object] | None = None

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def cursor(self, **kwargs: object) -> _FakeCursor:
        self.cursor_kwargs = kwargs
        return _FakeCursor()


def test_repository_requires_a_connection():
    from trace_backed_memory.postgres import PostgresMemoryRepository

    with pytest.raises(ValueError, match="connection is required"):
        PostgresMemoryRepository(None)


def test_borrowed_connection_remains_open_after_repository_close():
    from trace_backed_memory.postgres import PostgresMemoryRepository

    connection = _FakeConnection()
    repository = PostgresMemoryRepository(connection)

    repository.close()
    repository.close()

    assert connection.close_calls == 0
    assert connection.closed is False


def test_owned_connection_closes_exactly_once():
    from trace_backed_memory.postgres import PostgresMemoryRepository

    connection = _FakeConnection()
    repository = PostgresMemoryRepository(connection, owns_connection=True)

    repository.close()
    repository.close()

    assert connection.close_calls == 1
    assert connection.closed is True


def test_operations_after_close_fail():
    from trace_backed_memory.postgres import PostgresAdapterError, PostgresMemoryRepository

    repository = PostgresMemoryRepository(_FakeConnection())
    repository.close()

    with pytest.raises(PostgresAdapterError, match="repository is closed"):
        repository.load()
