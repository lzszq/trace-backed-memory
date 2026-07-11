from __future__ import annotations

import subprocess
import sys
from dataclasses import is_dataclass
from decimal import Decimal
from pathlib import Path

import pytest


def _complete_store(
    *,
    decision_reason: str = "directly relevant",
    extra_trace: bool = False,
    offset_timestamps: bool = False,
):
    from trace_backed_memory import (
        FailureCase,
        Lesson,
        MemoryContext,
        MemoryDecision,
        ProjectPolicy,
        Trace,
        TraceBackedMemoryStore,
    )

    if offset_timestamps:
        timestamps = {
            "trace": "2025-01-02T03:04:05+05:30",
            "reviewed": "2025-01-02T03:05:06+05:30",
            "case": "2025-01-02T03:06:07+05:30",
            "lesson": "2025-01-02T03:07:08+05:30",
            "policy": "2025-01-02T03:08:09+05:30",
        }
    else:
        timestamps = {
            "trace": "2025-01-01T21:34:05Z",
            "reviewed": "2025-01-01T21:35:06Z",
            "case": "2025-01-01T21:36:07Z",
            "lesson": "2025-01-01T21:37:08Z",
            "policy": None,
        }

    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_sync",
            run_id="run_sync",
            commit_sha="commit_sync",
            repo="repo_sync",
            tenant="tenant_sync",
            branch="feature/sync",
            dirty=True,
            prompt_version="prompt_sync_v2",
            prompt_family="repair_agent",
            tool_schema_version="tools_sync_v3",
            model="model_sync",
            eval_suite="suite_sync",
            input_hash="input_hash_sync",
            output_hash="output_hash_sync",
            retrieved_context=[
                {"rank": 1, "source": "docs_sync", "trusted": True}
            ],
            tool_calls=[{"name": "search_docs", "arguments": {"query": "sync"}}],
            tool_outputs=[{"documents": 3, "succeeded": False}],
            eval_result="fail",
            latency_ms=321,
            cost_usd=0.125,
            error="query validation failed",
            trace_uri="trace://sync/trace_sync",
            created_at=timestamps["trace"],
        )
    )
    if extra_trace:
        store.record_trace(
            Trace(
                trace_id="trace_sync_extra",
                run_id="run_sync_extra",
                commit_sha="commit_sync_extra",
                created_at=None,
            )
        )
    store.add_failure_case(
        FailureCase(
            case_id="case_sync",
            source_trace_id="trace_sync",
            commit_sha="commit_sync",
            failure_type="invalid_tool_argument",
            symptom="empty query",
            root_cause="query validation was skipped",
            fix="require a query",
            fix_commit_sha="fix_sync",
            regression_passed=True,
            reviewed_by="reviewer_sync",
            review_notes="verified against the regression suite",
            reviewed_at=timestamps["reviewed"],
            status="verified",
            created_at=timestamps["case"],
        )
    )
    store.add_lesson(
        Lesson(
            lesson_id="lesson_sync",
            source_case_id="case_sync",
            lesson_text="Use a non-empty query.",
            memory_type="procedural",
            scope={"repo": "repo_sync", "tenant": "tenant_sync"},
            confidence=0.875,
            sensitive=False,
            eval_leaking=False,
            status="active",
            created_at=timestamps["lesson"],
        )
    )
    store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_sync",
            policy_text="Validate tool arguments before execution.",
            scope={"repo": "repo_sync", "tenant": "tenant_sync"},
            confidence=0.625,
            sensitive=True,
            eval_leaking=False,
            status="active",
            created_at=timestamps["policy"],
        )
    )
    context = MemoryContext(
        mode="repair",
        repo="repo_sync",
        tenant="tenant_sync",
        commit_sha="commit_sync",
    )
    store.log_decision(
        "run_sync",
        context,
        ["lesson_sync", "policy_sync"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_sync", "policy_sync"],
            blocked_memory_ids=[],
            reason=decision_reason,
            risk="low",
            recommended_injection="short_summary",
        ),
        eval_result="pass",
    )
    return store


def test_repository_sync_round_trips_and_is_idempotent(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _complete_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)

        first = repository.sync(store)
        assert first.traces == PostgresSyncCounts(inserted=1)
        assert first.failure_cases == PostgresSyncCounts(inserted=1)
        assert first.lessons == PostgresSyncCounts(inserted=1)
        assert first.project_policies == PostgresSyncCounts(inserted=1)
        assert first.usage_logs == PostgresSyncCounts(inserted=1)
        assert connection.execute(
            "SELECT sensitive, eval_leaking FROM public.project_policies "
            "WHERE policy_id = %s",
            ("policy_sync",),
        ).fetchone() == (True, False)
        assert connection.execute(
            "SELECT candidate_memory_ids, used_memory_ids "
            "FROM public.memory_usage_decisions WHERE decision_id = %s",
            ("decision_000001",),
        ).fetchone() == (["lesson_sync", "policy_sync"], ["lesson_sync"])

        loaded = repository.load().to_snapshot()
        assert loaded == store.to_snapshot()
        assert loaded["project_policies"][0]["sensitive"] is True
        assert loaded["project_policies"][0]["eval_leaking"] is False
        assert loaded["usage_logs"][0]["candidate_memory_ids"] == [
            "lesson_sync",
            "policy_sync",
        ]
        assert loaded["usage_logs"][0]["used_memory_ids"] == ["lesson_sync"]

        second = repository.sync(store)
        assert second.traces == PostgresSyncCounts(unchanged=1)
        assert second.failure_cases == PostgresSyncCounts(unchanged=1)
        assert second.lessons == PostgresSyncCounts(unchanged=1)
        assert second.project_policies == PostgresSyncCounts(unchanged=1)
        assert second.usage_logs == PostgresSyncCounts(unchanged=1)


def test_repository_sync_reports_empty_and_multi_record_counts(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        empty = repository.sync(TraceBackedMemoryStore())
        assert empty.traces == PostgresSyncCounts()
        assert empty.failure_cases == PostgresSyncCounts()
        assert empty.lessons == PostgresSyncCounts()
        assert empty.project_policies == PostgresSyncCounts()
        assert empty.usage_logs == PostgresSyncCounts()

        result = repository.sync(_complete_store(extra_trace=True))
        assert result.traces == PostgresSyncCounts(inserted=2)
        assert result.failure_cases == PostgresSyncCounts(inserted=1)
        assert result.lessons == PostgresSyncCounts(inserted=1)
        assert result.project_policies == PostgresSyncCounts(inserted=1)
        assert result.usage_logs == PostgresSyncCounts(inserted=1)


def test_repository_sync_rolls_back_earlier_inserts_on_late_conflict(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresConflictError, PostgresMemoryRepository

    postgres_cluster.load_schema()
    baseline = _complete_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        conflicting = _complete_store(
            decision_reason="conflicting reason",
            extra_trace=True,
        )
        with pytest.raises(
            PostgresConflictError,
            match="memory_usage_decisions.*decision_000001",
        ):
            repository.sync(conflicting)

        assert repository.load().to_snapshot() == baseline.to_snapshot()


def test_repository_sync_conflicts_when_immutable_json_types_differ(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    from trace_backed_memory import Trace, TraceBackedMemoryStore
    from trace_backed_memory.postgres import PostgresConflictError, PostgresMemoryRepository

    postgres_cluster.load_schema()
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_json_types",
            run_id="run_json_types",
            commit_sha="commit_json_types",
            tool_outputs=[{"succeeded": False}],
        )
    )
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        connection.execute(
            "UPDATE public.traces SET tool_outputs = %s WHERE trace_id = %s",
            (Jsonb([{"succeeded": 0}]), "trace_json_types"),
        )
        connection.commit()

        with pytest.raises(PostgresConflictError, match="traces.*trace_json_types"):
            repository.sync(store)


def test_repository_sync_keeps_numeric_normalization_type_aware(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")

    from trace_backed_memory import Trace, TraceBackedMemoryStore
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_numeric_normalization",
            run_id="run_numeric_normalization",
            commit_sha="commit_numeric_normalization",
            cost_usd=1.0,
            tool_outputs=[{"succeeded": False, "attempts": 0}],
        )
    )
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)

        repository.sync(store)
        loaded = repository.load().traces["trace_numeric_normalization"]
        assert loaded.cost_usd == 1
        assert type(loaded.cost_usd) is int
        assert repository.sync(store).traces == PostgresSyncCounts(unchanged=1)


def test_repository_sync_is_idempotent_when_jsonb_normalizes_large_float(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")

    from trace_backed_memory import Trace, TraceBackedMemoryStore
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id="trace_json_large_float",
            run_id="run_json_large_float",
            commit_sha="commit_json_large_float",
            tool_outputs=[{"metrics": {"magnitude": 1e20}}],
        )
    )
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)

        repository.sync(store)
        magnitude = repository.load().traces["trace_json_large_float"].tool_outputs[
            0
        ]["metrics"]["magnitude"]
        assert magnitude == 100000000000000000000
        assert type(magnitude) is int
        assert repository.sync(store).traces == PostgresSyncCounts(unchanged=1)


def test_repository_sync_is_idempotent_for_offset_timestamp(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")

    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _complete_store(offset_timestamps=True)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)

        repository.sync(store)
        loaded = repository.load().to_snapshot()
        assert loaded["traces"][0]["created_at"] == "2025-01-01T21:34:05Z"
        assert loaded["failure_cases"][0]["reviewed_at"] == "2025-01-01T21:35:06Z"
        assert loaded["failure_cases"][0]["created_at"] == "2025-01-01T21:36:07Z"
        assert loaded["lessons"][0]["created_at"] == "2025-01-01T21:37:08Z"
        assert loaded["project_policies"][0]["created_at"] == (
            "2025-01-01T21:38:09Z"
        )

        second = repository.sync(store)
        assert second.traces == PostgresSyncCounts(unchanged=1)
        assert second.failure_cases == PostgresSyncCounts(unchanged=1)
        assert second.lessons == PostgresSyncCounts(unchanged=1)
        assert second.project_policies == PostgresSyncCounts(unchanged=1)
        assert second.usage_logs == PostgresSyncCounts(unchanged=1)


@pytest.mark.parametrize(
    ("table", "record_id", "update_sql", "replacement"),
    [
        (
            "traces",
            "trace_sync",
            "UPDATE public.traces SET model = %s WHERE trace_id = %s",
            "conflicting_model",
        ),
        (
            "failure_cases",
            "case_sync",
            "UPDATE public.failure_cases SET symptom = %s WHERE case_id = %s",
            "conflicting symptom",
        ),
        (
            "lessons",
            "lesson_sync",
            "UPDATE public.lessons SET lesson_text = %s WHERE lesson_id = %s",
            "Conflicting lesson text.",
        ),
        (
            "project_policies",
            "policy_sync",
            "UPDATE public.project_policies SET policy_text = %s WHERE policy_id = %s",
            "Conflicting project policy.",
        ),
        (
            "memory_usage_decisions",
            "decision_000001",
            "UPDATE public.memory_usage_decisions SET reason = %s WHERE decision_id = %s",
            "conflicting usage reason",
        ),
    ],
)
def test_repository_sync_conflicts_for_differences_in_every_table(
    postgres_cluster,
    table,
    record_id,
    update_sql,
    replacement,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresConflictError, PostgresMemoryRepository

    postgres_cluster.load_schema()
    store = _complete_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        connection.execute(update_sql, (replacement, record_id))
        connection.commit()

        with pytest.raises(PostgresConflictError, match=f"{table}.*{record_id}"):
            repository.sync(store)


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
