from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import is_dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest


def _complete_store(
    *,
    decision_reason: str = "directly relevant",
    decision_eval_result: str | None = "pass",
    extra_trace: bool = False,
    offset_timestamps: bool = False,
    benchmark_identity: bool = False,
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
        eval_suite="suite_sync" if benchmark_identity else None,
        input_hash="input_hash_sync" if benchmark_identity else None,
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
        eval_result=decision_eval_result,
    )
    return store


def _draft_case_store(*, suffix: str = "lifecycle"):
    from trace_backed_memory import FailureCase, Trace, TraceBackedMemoryStore

    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id=f"trace_{suffix}",
            run_id=f"run_{suffix}",
            commit_sha=f"commit_{suffix}",
            repo=f"repo_{suffix}",
            tenant=f"tenant_{suffix}",
            created_at="2025-02-01T08:00:00Z",
        )
    )
    store.add_failure_case(
        FailureCase(
            case_id=f"case_{suffix}",
            source_trace_id=f"trace_{suffix}",
            commit_sha=f"commit_{suffix}",
            failure_type="unclassified",
            symptom="request failed",
            created_at="2025-02-01T09:00:00Z",
        )
    )
    return store


def _pending_trace_store(*, suffix: str = "completion"):
    from trace_backed_memory import Trace, TraceBackedMemoryStore

    store = TraceBackedMemoryStore()
    store.record_trace(
        Trace(
            trace_id=f"trace_{suffix}",
            run_id=f"run_{suffix}",
            commit_sha=f"commit_{suffix}",
            repo="repo_completion",
            tenant="tenant_completion",
            branch="main",
            prompt_version="planner_v3",
            prompt_family="planner",
            tool_schema_version="search_docs_v2",
            model="model_completion",
            eval_suite="suite_completion",
            input_hash="sha256:completion-input",
            retrieved_context=[{"source": "docs", "rank": 1}],
            tool_calls=[{"name": "search_docs", "arguments": {"query": "completion"}}],
            eval_result="unknown",
            created_at="2025-07-13T08:00:00Z",
        )
    )
    return store


def _pending_memory_run_store():
    from trace_backed_memory import MemoryContext, MemoryDecision

    store = _complete_store()
    source = store.traces["trace_sync"]
    current = store.record_trace(
        replace(
            source,
            trace_id="trace_atomic_run",
            run_id="run_atomic_run",
            output_hash=None,
            tool_outputs=[],
            eval_result="unknown",
            latency_ms=None,
            cost_usd=None,
            error=None,
            trace_uri=None,
            created_at="2025-07-13T09:00:00Z",
        )
    )
    store.log_decision(
        current.run_id,
        MemoryContext(
            mode="repair",
            repo=current.repo or "repo_sync",
            tenant=current.tenant,
            commit_sha=current.commit_sha,
        ),
        ["lesson_sync"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_sync"],
            blocked_memory_ids=[],
            reason="atomic run pending",
            risk="low",
            recommended_injection="short_summary",
        ),
    )
    return store


def _assert_sync_conflict_preserves_state(
    postgres_cluster,
    baseline,
    conflicting,
    *,
    table: str,
    record_id: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresConflictError, PostgresMemoryRepository

    postgres_cluster.load_schema()
    expected = baseline.to_snapshot()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        with pytest.raises(PostgresConflictError, match=f"{table}.*{record_id}"):
            repository.sync(conflicting)

        assert repository.load().to_snapshot() == expected


def test_repository_sync_updates_failure_case_lifecycle_and_cascades_lessons(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import Lesson
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _draft_case_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        store.review_failure_case(
            "case_lifecycle",
            reviewed_by="reviewer_lifecycle",
            root_cause="empty arguments bypassed validation",
            failure_type="invalid_tool_argument",
            symptom="tool rejected an empty query",
            review_notes="reproduced from the source trace",
            reviewed_at="2025-02-01T10:00:00Z",
        )
        store.verify_failure_case(
            "case_lifecycle",
            fix="validate arguments before tool execution",
            fix_commit_sha="fix_lifecycle",
            regression_passed=True,
        )
        store.add_lesson(
            Lesson(
                lesson_id="lesson_lifecycle",
                source_case_id="case_lifecycle",
                lesson_text="Validate arguments before executing tools.",
                memory_type="procedural",
                scope={"repo": "repo_lifecycle", "tenant": "tenant_lifecycle"},
                confidence=0.9,
                created_at="2025-02-01T11:00:00Z",
            )
        )

        reviewed = repository.sync(store)
        assert reviewed.failure_cases == PostgresSyncCounts(updated=1)
        loaded_case = repository.load().failure_cases["case_lifecycle"]
        assert (
            loaded_case.failure_type,
            loaded_case.symptom,
            loaded_case.root_cause,
            loaded_case.reviewed_by,
            loaded_case.review_notes,
            loaded_case.reviewed_at,
            loaded_case.fix,
            loaded_case.fix_commit_sha,
            loaded_case.regression_passed,
            loaded_case.status,
        ) == (
            "invalid_tool_argument",
            "tool rejected an empty query",
            "empty arguments bypassed validation",
            "reviewer_lifecycle",
            "reproduced from the source trace",
            "2025-02-01T10:00:00Z",
            "validate arguments before tool execution",
            "fix_lifecycle",
            True,
            "verified",
        )

        store.obsolete_failure_case("case_lifecycle")
        obsoleted = repository.sync(store)
        assert obsoleted.failure_cases == PostgresSyncCounts(updated=1)
        assert obsoleted.lessons == PostgresSyncCounts(unchanged=1)
        assert connection.execute(
            "SELECT status FROM public.lessons WHERE lesson_id = %s",
            ("lesson_lifecycle",),
        ).fetchone() == ("obsolete",)
        loaded = repository.load()
        assert loaded.failure_cases["case_lifecycle"].status == "obsolete"
        assert loaded.lessons["lesson_lifecycle"].status == "obsolete"


def test_repository_sync_updates_only_lesson_and_policy_statuses(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _complete_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        store.obsolete_lesson("lesson_sync")
        store.obsolete_project_policy("policy_sync")
        result = repository.sync(store)

        assert result.lessons == PostgresSyncCounts(updated=1)
        assert result.project_policies == PostgresSyncCounts(updated=1)
        loaded = repository.load()
        assert loaded.lessons["lesson_sync"].status == "obsolete"
        assert loaded.project_policies["policy_sync"].status == "obsolete"


def test_runtime_sql_has_no_unqualified_now_calls():
    from trace_backed_memory import postgres

    runtime_sql = "\n".join(
        value
        for name, value in vars(postgres).items()
        if name.startswith("_") and name.isupper() and isinstance(value, str)
    )

    assert re.search(r"(?<![.\w])now\s*\(", runtime_sql, re.IGNORECASE) is None


def test_repository_lifecycle_timestamps_resist_hostile_search_path(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository

    postgres_cluster.load_schema()
    store = _complete_store()
    hijacked_timestamp = "2001-02-03 04:05:06+00"
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        connection.execute("CREATE SCHEMA attacker")
        connection.execute("GRANT USAGE, CREATE ON SCHEMA attacker TO PUBLIC")
        connection.execute(
            "CREATE FUNCTION attacker.now() RETURNS timestamptz "
            "LANGUAGE sql IMMUTABLE AS "
            "$$ SELECT TIMESTAMPTZ '2001-02-03 04:05:06+00' $$"
        )
        connection.commit()
        connection.execute("SET search_path = attacker, public, pg_catalog")
        assert connection.execute(
            "SELECT now() = %s::timestamptz", (hijacked_timestamp,)
        ).fetchone() == (True,)

        store.obsolete_lesson("lesson_sync")
        store.obsolete_project_policy("policy_sync")
        repository.sync(store)

        lifecycle_rows = connection.execute(
            "SELECT status, updated_at = %s::timestamptz "
            "FROM public.lessons WHERE lesson_id = %s "
            "UNION ALL "
            "SELECT status, updated_at = %s::timestamptz "
            "FROM public.project_policies WHERE policy_id = %s",
            (
                hijacked_timestamp,
                "lesson_sync",
                hijacked_timestamp,
                "policy_sync",
            ),
        ).fetchall()

    assert lifecycle_rows == [("obsolete", False), ("obsolete", False)]


@pytest.mark.parametrize(
    ("immutable_field", "alternate_commit_sha"),
    [
        ("source_trace_id", "commit_sync"),
        ("commit_sha", "commit_alternate_provenance"),
    ],
)
def test_repository_sync_rejects_failure_case_immutable_provenance_changes(
    postgres_cluster,
    immutable_field,
    alternate_commit_sha,
):
    from trace_backed_memory import TraceBackedMemoryStore

    baseline = _complete_store()
    snapshot = baseline.to_snapshot()
    alternate_trace_id = "trace_alternate_provenance"
    snapshot["traces"].append(
        {
            **snapshot["traces"][0],
            "trace_id": alternate_trace_id,
            "run_id": "run_alternate_provenance",
            "commit_sha": alternate_commit_sha,
        }
    )
    snapshot["failure_cases"][0]["source_trace_id"] = alternate_trace_id
    if immutable_field == "commit_sha":
        snapshot["failure_cases"][0]["commit_sha"] = alternate_commit_sha
    conflicting = TraceBackedMemoryStore.from_snapshot(snapshot)

    _assert_sync_conflict_preserves_state(
        postgres_cluster,
        baseline,
        conflicting,
        table="failure_cases",
        record_id="case_sync",
    )


def test_sync_failure_case_row_conflicts_on_isolated_commit_sha(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
        _sync_failure_case_row,
    )

    postgres_cluster.load_schema()
    baseline = _draft_case_store(suffix="commit_sha_isolation")
    incoming_snapshot = baseline.to_snapshot()
    alternate_commit_sha = "commit_commit_sha_isolation_alternate"
    incoming_snapshot["traces"][0]["commit_sha"] = alternate_commit_sha
    incoming_snapshot["failure_cases"][0]["commit_sha"] = alternate_commit_sha
    incoming = TraceBackedMemoryStore.from_snapshot(incoming_snapshot)
    incoming_case = incoming.to_snapshot()["failure_cases"][0]
    case_id = "case_commit_sha_isolation"
    trace_id = "trace_commit_sha_isolation"
    assert incoming.failure_cases[case_id].source_trace_id == trace_id
    assert incoming.failure_cases[case_id].commit_sha == alternate_commit_sha
    assert incoming.traces[trace_id].commit_sha == alternate_commit_sha

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)
        expected = repository.load().to_snapshot()
        stored_case = expected["failure_cases"][0]
        assert {
            field: value
            for field, value in stored_case.items()
            if field != "commit_sha"
        } == {
            field: value
            for field, value in incoming_case.items()
            if field != "commit_sha"
        }
        assert stored_case["commit_sha"] != incoming_case["commit_sha"]

        with pytest.raises(
            PostgresConflictError,
            match=(
                "^PostgreSQL conflict for failure_cases row "
                "case_commit_sha_isolation$"
            ),
        ):
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        "INSERT INTO public.traces "
                        "(trace_id, run_id, commit_sha) VALUES (%s, %s, %s)",
                        (
                            "trace_commit_sha_rollback_marker",
                            "run_commit_sha_rollback_marker",
                            "commit_sha_rollback_marker",
                        ),
                    )
                    _sync_failure_case_row(
                        cursor,
                        record_id=case_id,
                        incoming=incoming_case,
                    )

        assert repository.load().to_snapshot() == expected


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("lesson_text", "Use a validated non-empty query."),
        (
            "scope",
            {"repo": "repo_sync", "tenant": "tenant_sync", "branch": "main"},
        ),
        ("confidence", 0.5),
        ("source_case_id", "case_alternate"),
        ("memory_type", "semantic"),
        ("sensitive", True),
        ("eval_leaking", True),
        ("created_at", "2026-01-02T03:04:05Z"),
    ],
)
def test_repository_sync_conflicts_for_lesson_non_status_changes(
    postgres_cluster,
    field_name,
    replacement,
):
    from trace_backed_memory import TraceBackedMemoryStore

    baseline = _complete_store()
    snapshot = baseline.to_snapshot()
    if field_name == "source_case_id":
        snapshot["traces"].append(
            {
                **snapshot["traces"][0],
                "trace_id": "trace_alternate",
                "run_id": "run_alternate",
                "commit_sha": "commit_alternate",
            }
        )
        snapshot["failure_cases"].append(
            {
                **snapshot["failure_cases"][0],
                "case_id": "case_alternate",
                "source_trace_id": "trace_alternate",
                "commit_sha": "commit_alternate",
            }
        )
    snapshot["lessons"][0][field_name] = replacement
    conflicting = TraceBackedMemoryStore.from_snapshot(snapshot)

    _assert_sync_conflict_preserves_state(
        postgres_cluster,
        baseline,
        conflicting,
        table="lessons",
        record_id="lesson_sync",
    )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("policy_text", "Require schema-valid tool arguments."),
        (
            "scope",
            {"repo": "repo_sync", "tenant": "tenant_sync", "branch": "main"},
        ),
        ("confidence", 0.5),
        ("sensitive", False),
        ("eval_leaking", True),
        ("created_at", "2026-01-02T03:04:05Z"),
    ],
)
def test_repository_sync_conflicts_for_policy_non_status_changes(
    postgres_cluster,
    field_name,
    replacement,
):
    from trace_backed_memory import TraceBackedMemoryStore

    baseline = _complete_store()
    snapshot = baseline.to_snapshot()
    snapshot["project_policies"][0][field_name] = replacement
    conflicting = TraceBackedMemoryStore.from_snapshot(snapshot)

    _assert_sync_conflict_preserves_state(
        postgres_cluster,
        baseline,
        conflicting,
        table="project_policies",
        record_id="policy_sync",
    )


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


def test_repository_sync_completes_pending_trace_and_is_idempotent(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _pending_trace_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        created_at_before = connection.execute(
            "SELECT created_at FROM public.traces WHERE trace_id = %s",
            ("trace_completion",),
        ).fetchone()[0]

        completed = store.complete_trace(
            "trace_completion",
            eval_result="error",
            output_hash="sha256:completion-output",
            tool_outputs=[{"documents": 4}],
            latency_ms=75,
            cost_usd=0.125,
            error="executor failed",
            trace_uri="trace://completion",
        )
        updated = repository.sync(store)

        assert updated.traces == PostgresSyncCounts(updated=1)
        assert updated.failure_cases == PostgresSyncCounts()
        assert updated.lessons == PostgresSyncCounts()
        assert updated.project_policies == PostgresSyncCounts()
        assert updated.usage_logs == PostgresSyncCounts()
        assert connection.execute(
            "SELECT output_hash, tool_outputs, eval_result, latency_ms, "
            "cost_usd, error, trace_uri, created_at FROM public.traces "
            "WHERE trace_id = %s",
            ("trace_completion",),
        ).fetchone() == (
            "sha256:completion-output",
            [{"documents": 4}],
            "error",
            75,
            Decimal("0.125"),
            "executor failed",
            "trace://completion",
            created_at_before,
        )
        assert repository.load().traces[completed.trace_id] == completed

        repeated = repository.sync(store)
        assert repeated.traces == PostgresSyncCounts(unchanged=1)


def test_repository_trace_completion_preserves_but_cannot_rewrite_prefilled_evidence(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
        PostgresSyncCounts,
    )

    postgres_cluster.load_schema()
    baseline_snapshot = _pending_trace_store(suffix="prefilled").to_snapshot()
    baseline_snapshot["traces"][0]["output_hash"] = "sha256:prefilled"
    baseline = TraceBackedMemoryStore.from_snapshot(baseline_snapshot)
    conflicting_snapshot = baseline.to_snapshot()
    conflicting_snapshot["traces"][0]["eval_result"] = "pass"
    conflicting_snapshot["traces"][0]["output_hash"] = "sha256:other"
    conflicting = TraceBackedMemoryStore.from_snapshot(conflicting_snapshot)
    valid = TraceBackedMemoryStore.from_snapshot(baseline.to_snapshot())
    valid.complete_trace("trace_prefilled", eval_result="pass")

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        with pytest.raises(
            PostgresConflictError,
            match="traces.*trace_prefilled.*immutable conflict",
        ):
            repository.sync(conflicting)
        assert repository.load().traces["trace_prefilled"].eval_result == "unknown"

        updated = repository.sync(valid)
        assert updated.traces == PostgresSyncCounts(updated=1)
        loaded = repository.load().traces["trace_prefilled"]
        assert loaded.eval_result == "pass"
        assert loaded.output_hash == "sha256:prefilled"


def test_repository_sync_rejects_stale_or_conflicting_trace_completion(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
        PostgresSyncCounts,
    )

    postgres_cluster.load_schema()
    store = _pending_trace_store()
    stale = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    replay = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    conflicting = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        store.complete_trace(
            "trace_completion",
            eval_result="pass",
            output_hash="sha256:completion-output",
        )
        repository.sync(store)

        replay.complete_trace(
            "trace_completion",
            eval_result="pass",
            output_hash="sha256:completion-output",
        )
        assert repository.sync(replay).traces == PostgresSyncCounts(unchanged=1)

        with pytest.raises(
            PostgresConflictError,
            match="traces.*trace_completion.*immutable conflict",
        ):
            repository.sync(stale)

        conflicting.complete_trace(
            "trace_completion",
            eval_result="error",
            error="different result",
        )
        with pytest.raises(
            PostgresConflictError,
            match="traces.*trace_completion.*immutable conflict",
        ):
            repository.sync(conflicting)

        loaded = repository.load().traces["trace_completion"]
        assert loaded.eval_result == "pass"
        assert loaded.output_hash == "sha256:completion-output"


def test_repository_rolls_back_trace_completion_on_later_trace_conflict(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
    )

    postgres_cluster.load_schema()
    baseline = _pending_trace_store(suffix="completion_a")
    second = _pending_trace_store(suffix="completion_b")
    baseline.record_trace(second.traces["trace_completion_b"])
    incoming = TraceBackedMemoryStore.from_snapshot(baseline.to_snapshot())
    incoming.complete_trace("trace_completion_a", eval_result="pass")
    conflicting_snapshot = incoming.to_snapshot()
    conflicting_snapshot["traces"][1]["branch"] = "other"
    conflicting = TraceBackedMemoryStore.from_snapshot(conflicting_snapshot)

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        with pytest.raises(
            PostgresConflictError,
            match="traces.*trace_completion_b.*immutable conflict",
        ):
            repository.sync(conflicting)

        loaded = repository.load()
        assert loaded.traces["trace_completion_a"].eval_result == "unknown"
        assert loaded.traces["trace_completion_b"].branch == "main"


def test_repository_sync_updates_deferred_usage_outcome_and_is_idempotent(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _complete_store(decision_eval_result=None)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        sealed = store.record_decision_outcome(
            "decision_000001",
            "fail",
            memory_caused_failure=True,
        )
        updated = repository.sync(store)

        assert updated.traces == PostgresSyncCounts(unchanged=1)
        assert updated.failure_cases == PostgresSyncCounts(unchanged=1)
        assert updated.lessons == PostgresSyncCounts(unchanged=1)
        assert updated.project_policies == PostgresSyncCounts(unchanged=1)
        assert updated.usage_logs == PostgresSyncCounts(updated=1)
        assert connection.execute(
            "SELECT eval_result, memory_caused_failure "
            "FROM public.memory_usage_decisions WHERE decision_id = %s",
            ("decision_000001",),
        ).fetchone() == ("fail", True)
        assert repository.load().usage_logs[0] == sealed

        repeated = repository.sync(store)
        assert repeated.usage_logs == PostgresSyncCounts(unchanged=1)


def test_repository_sync_persists_atomic_memory_run_completion(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _pending_memory_run_store()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        completion = store.complete_memory_run(
            trace_id="trace_atomic_run",
            decision_id="decision_000002",
            eval_result="pass",
            output_hash="sha256:atomic-output",
            tool_outputs=[{"documents": 4}],
            latency_ms=80,
        )
        updated = repository.sync(store)

        assert updated.traces == PostgresSyncCounts(updated=1, unchanged=1)
        assert updated.failure_cases == PostgresSyncCounts(unchanged=1)
        assert updated.lessons == PostgresSyncCounts(unchanged=1)
        assert updated.project_policies == PostgresSyncCounts(unchanged=1)
        assert updated.usage_logs == PostgresSyncCounts(updated=1, unchanged=1)

        loaded = repository.load()
        assert loaded.traces["trace_atomic_run"] == completion.trace
        assert next(
            log
            for log in loaded.usage_logs
            if log.decision_id == "decision_000002"
        ) == completion.usage_log

        repeated = repository.sync(store)
        assert repeated.traces == PostgresSyncCounts(unchanged=2)
        assert repeated.usage_logs == PostgresSyncCounts(unchanged=2)


def test_repository_rolls_back_trace_update_on_atomic_run_usage_conflict(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
    )

    postgres_cluster.load_schema()
    baseline = _pending_memory_run_store()
    incoming = TraceBackedMemoryStore.from_snapshot(baseline.to_snapshot())
    incoming.complete_memory_run(
        trace_id="trace_atomic_run",
        decision_id="decision_000002",
        eval_result="pass",
        output_hash="sha256:atomic-output",
    )
    conflicting_snapshot = incoming.to_snapshot()
    conflicting_log = next(
        log
        for log in conflicting_snapshot["usage_logs"]
        if log["decision_id"] == "decision_000002"
    )
    conflicting_log["reason"] = "conflicting atomic decision"
    conflicting = TraceBackedMemoryStore.from_snapshot(conflicting_snapshot)

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        with pytest.raises(
            PostgresConflictError,
            match="memory_usage_decisions.*decision_000002.*immutable conflict",
        ):
            repository.sync(conflicting)

        loaded = repository.load()
        assert loaded.traces["trace_atomic_run"].eval_result == "unknown"
        assert next(
            log
            for log in loaded.usage_logs
            if log.decision_id == "decision_000002"
        ).eval_result is None


def test_repository_load_reproduces_derived_memory_run_audits(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository

    postgres_cluster.load_schema()
    store = _pending_memory_run_store()
    store.complete_trace("trace_atomic_run", eval_result="error")
    expected = store.memory_run_audits()

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        restored = repository.load()

    assert [audit.status for audit in expected] == ["conflict", "trace_only"]
    assert restored.memory_run_audits() == expected


def test_repository_load_reproduces_derived_memory_run_metrics(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository

    postgres_cluster.load_schema()
    store = _pending_memory_run_store()
    store.complete_trace("trace_atomic_run", eval_result="error")
    expected = store.memory_run_metrics()

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        restored = repository.load()

    assert expected.decision_count == 2
    assert expected.trace_only_count == 1
    assert expected.conflict_count == 1
    assert expected.recoverable_count == 1
    assert restored.memory_run_metrics() == expected


def test_repository_sync_persists_atomic_batch_memory_run_recovery(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import MemoryContext, MemoryDecision
    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresSyncCounts,
    )

    postgres_cluster.load_schema()
    store = _pending_memory_run_store()
    first = store.traces["trace_atomic_run"]
    second = store.record_trace(
        replace(
            first,
            trace_id="trace_atomic_run_second",
            run_id="run_atomic_run_second",
            created_at="2025-07-13T10:00:00Z",
        )
    )
    store.log_decision(
        second.run_id,
        MemoryContext(
            mode="repair",
            repo=second.repo or "repo_sync",
            tenant=second.tenant,
            commit_sha=second.commit_sha,
        ),
        ["lesson_sync"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_sync"],
            blocked_memory_ids=[],
            reason="second atomic batch run pending",
            risk="low",
            recommended_injection="short_summary",
        ),
    )
    store.record_decision_outcome(
        "decision_000002",
        "error",
        memory_caused_failure=True,
    )
    store.record_decision_outcome("decision_000003", "pass")

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        completions = store.recover_memory_runs(
            ("decision_000003", "decision_000002")
        )
        updated = repository.sync(store)

        assert [item.trace.eval_result for item in completions] == ["pass", "error"]
        assert completions[1].usage_log.memory_caused_failure is True
        assert updated.traces == PostgresSyncCounts(updated=2, unchanged=1)
        assert updated.failure_cases == PostgresSyncCounts(unchanged=1)
        assert updated.lessons == PostgresSyncCounts(unchanged=1)
        assert updated.project_policies == PostgresSyncCounts(unchanged=1)
        assert updated.usage_logs == PostgresSyncCounts(unchanged=3)

        restored = repository.load()
        assert [audit.status for audit in restored.memory_run_audits()] == [
            "conflict",
            "complete",
            "complete",
        ]
        assert restored.memory_run_metrics().complete_count == 2
        assert restored.memory_run_metrics().recoverable_count == 0

        repeated = repository.sync(store)
        assert repeated.traces == PostgresSyncCounts(unchanged=3)
        assert repeated.usage_logs == PostgresSyncCounts(unchanged=3)


def test_repository_sync_persists_atomic_batch_memory_run_completion(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import MemoryContext, MemoryDecision, MemoryRunResult
    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresSyncCounts,
    )

    postgres_cluster.load_schema()
    store = _pending_memory_run_store()
    first = store.traces["trace_atomic_run"]
    second = store.record_trace(
        replace(
            first,
            trace_id="trace_atomic_completion_second",
            run_id="run_atomic_completion_second",
            created_at="2025-07-13T11:00:00Z",
        )
    )
    store.log_decision(
        second.run_id,
        MemoryContext(
            mode="repair",
            repo=second.repo or "repo_sync",
            tenant=second.tenant,
            commit_sha=second.commit_sha,
        ),
        ["lesson_sync"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_sync"],
            blocked_memory_ids=[],
            reason="second atomic completion pending",
            risk="low",
            recommended_injection="short_summary",
        ),
    )

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        completions = store.complete_memory_runs(
            (
                MemoryRunResult(
                    decision_id="decision_000003",
                    eval_result="pass",
                    output_hash="sha256:batch-pass",
                    tool_outputs=({"documents": 4},),
                ),
                MemoryRunResult(
                    decision_id="decision_000002",
                    eval_result="error",
                    memory_caused_failure=True,
                    error="batch executor failed",
                ),
            )
        )
        updated = repository.sync(store)

        assert [item.trace.eval_result for item in completions] == ["pass", "error"]
        assert updated.traces == PostgresSyncCounts(updated=2, unchanged=1)
        assert updated.failure_cases == PostgresSyncCounts(unchanged=1)
        assert updated.lessons == PostgresSyncCounts(unchanged=1)
        assert updated.project_policies == PostgresSyncCounts(unchanged=1)
        assert updated.usage_logs == PostgresSyncCounts(updated=2, unchanged=1)

        restored = repository.load()
        assert [audit.status for audit in restored.memory_run_audits()] == [
            "conflict",
            "complete",
            "complete",
        ]
        assert restored.traces["trace_atomic_completion_second"].tool_outputs == [
            {"documents": 4}
        ]
        assert restored.memory_run_metrics().complete_count == 2

        repeated = repository.sync(store)
        assert repeated.traces == PostgresSyncCounts(unchanged=3)
        assert repeated.usage_logs == PostgresSyncCounts(unchanged=3)


def test_repository_sync_persists_recovered_decision_only_memory_run(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store = _pending_memory_run_store()
    store.record_decision_outcome(
        "decision_000002",
        "error",
        memory_caused_failure=True,
    )

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        recovered = store.recover_memory_run(
            "decision_000002",
            output_hash="sha256:recovered-output",
            error="executor failed",
        )
        updated = repository.sync(store)

        assert updated.traces == PostgresSyncCounts(updated=1, unchanged=1)
        assert updated.failure_cases == PostgresSyncCounts(unchanged=1)
        assert updated.lessons == PostgresSyncCounts(unchanged=1)
        assert updated.project_policies == PostgresSyncCounts(unchanged=1)
        assert updated.usage_logs == PostgresSyncCounts(unchanged=2)

        restored = repository.load()
        assert restored.traces["trace_atomic_run"] == recovered.trace
        assert next(
            log
            for log in restored.usage_logs
            if log.decision_id == "decision_000002"
        ) == recovered.usage_log
        assert [audit.status for audit in restored.memory_run_audits()] == [
            "conflict",
            "complete",
        ]

        repeated = repository.sync(store)
        assert repeated.traces == PostgresSyncCounts(unchanged=2)
        assert repeated.usage_logs == PostgresSyncCounts(unchanged=2)


def test_repository_sync_rejects_stale_or_conflicting_sealed_outcome(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
    )

    postgres_cluster.load_schema()
    store = _complete_store(decision_eval_result="unknown")
    stale = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    replay = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    conflicting = TraceBackedMemoryStore.from_snapshot(store.to_snapshot())
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)
        store.record_decision_outcome("decision_000001", "pass")
        repository.sync(store)

        replay.record_decision_outcome("decision_000001", "pass")
        replayed = repository.sync(replay)
        assert replayed.usage_logs.updated == 0
        assert replayed.usage_logs.unchanged == 1

        with pytest.raises(
            PostgresConflictError,
            match="memory_usage_decisions.*decision_000001.*immutable conflict",
        ):
            repository.sync(stale)

        conflicting.record_decision_outcome("decision_000001", "error")
        with pytest.raises(
            PostgresConflictError,
            match="memory_usage_decisions.*decision_000001.*immutable conflict",
        ):
            repository.sync(conflicting)

        loaded = repository.load().usage_logs[0]
        assert loaded.eval_result == "pass"
        assert loaded.memory_caused_failure is False


def test_repository_rolls_back_outcome_update_on_later_usage_conflict(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import (
        MemoryContext,
        MemoryDecision,
        TraceBackedMemoryStore,
    )
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
    )

    postgres_cluster.load_schema()
    baseline = _complete_store(decision_eval_result=None)
    baseline.log_decision(
        "run_sync",
        MemoryContext(
            mode="repair",
            repo="repo_sync",
            tenant="tenant_sync",
            commit_sha="commit_sync",
        ),
        ["lesson_sync"],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=["lesson_sync"],
            blocked_memory_ids=[],
            reason="second decision",
            risk="low",
            recommended_injection="short_summary",
        ),
    )
    incoming = TraceBackedMemoryStore.from_snapshot(baseline.to_snapshot())
    incoming.record_decision_outcome("decision_000001", "pass")
    conflicting_snapshot = incoming.to_snapshot()
    conflicting_snapshot["usage_logs"][1]["reason"] = "conflicting second decision"
    conflicting = TraceBackedMemoryStore.from_snapshot(conflicting_snapshot)

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        with pytest.raises(
            PostgresConflictError,
            match="memory_usage_decisions.*decision_000002.*immutable conflict",
        ):
            repository.sync(conflicting)

        loaded = repository.load()
        assert [log.eval_result for log in loaded.usage_logs] == [None, None]


def test_repository_round_trips_benchmark_identity_audit_without_new_columns(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository

    postgres_cluster.load_schema()
    store = _complete_store(benchmark_identity=True)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store)

        trace_input_hash = connection.execute(
            "SELECT input_hash FROM public.traces WHERE trace_id = %s",
            ("trace_sync",),
        ).fetchone()[0]
        context, blocked_reasons = connection.execute(
            "SELECT context, system_blocked_reasons "
            "FROM public.memory_usage_decisions WHERE decision_id = %s",
            ("decision_000001",),
        ).fetchone()
        restored = repository.load()

    block_reason = "memory originates from current benchmark example"
    assert trace_input_hash == "input_hash_sync"
    assert context["eval_suite"] == "suite_sync"
    assert context["input_hash"] == "input_hash_sync"
    assert blocked_reasons["lesson_sync"] == block_reason
    assert restored.traces["trace_sync"].input_hash == "input_hash_sync"
    assert restored.usage_logs[0].context["input_hash"] == "input_hash_sync"
    assert restored.usage_logs[0].system_blocked_reasons["lesson_sync"] == block_reason


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
            decision_reason=(
                'postgresql://sync_user:dsn-secret@db/sync '
                '{"api_key":"payload-secret"} params=("sql-secret",)'
            ),
            extra_trace=True,
        )
        with pytest.raises(
            PostgresConflictError,
            match="memory_usage_decisions.*decision_000001",
        ) as error:
            repository.sync(conflicting)

        message = str(error.value)
        assert "sync" in message
        assert "memory_usage_decisions" in message
        assert "decision_000001" in message
        assert "dsn-secret" not in message
        assert "payload-secret" not in message
        assert "sql-secret" not in message
        assert isinstance(error.value.__cause__, PostgresConflictError)
        loaded = repository.load().to_snapshot()
        assert loaded == baseline.to_snapshot()
        assert all(trace["trace_id"] != "trace_sync_extra" for trace in loaded["traces"])


def test_borrowed_sync_is_removed_by_callers_outer_rollback(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository

    class RollBackOuterTransaction(Exception):
        pass

    postgres_cluster.load_schema()
    store = _draft_case_store(suffix="outer_rollback")
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)

        with pytest.raises(RollBackOuterTransaction):
            with connection.transaction():
                repository.sync(store)
                assert (
                    connection.info.transaction_status
                    == psycopg.pq.TransactionStatus.INTRANS
                )
                assert connection.execute(
                    "SELECT count(*) FROM public.traces WHERE trace_id = %s",
                    ("trace_outer_rollback",),
                ).fetchone() == (1,)
                raise RollBackOuterTransaction

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as observer:
        assert observer.execute(
            "SELECT count(*) FROM public.traces WHERE trace_id = %s",
            ("trace_outer_rollback",),
        ).fetchone() == (0,)


def test_repository_failure_rolls_back_savepoint_and_preserves_outer_work(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import (
        PostgresConflictError,
        PostgresMemoryRepository,
    )

    postgres_cluster.load_schema()
    baseline = _complete_store()
    conflicting = _complete_store(
        decision_reason="savepoint conflict",
        extra_trace=True,
    )
    outer_trace_ids = ("trace_outer_before", "trace_outer_after")
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(baseline)

        with connection.transaction():
            connection.execute(
                "INSERT INTO public.traces (trace_id, run_id, commit_sha) "
                "VALUES (%s, %s, %s)",
                (outer_trace_ids[0], "run_outer_before", "commit_outer_before"),
            )
            with pytest.raises(PostgresConflictError):
                repository.sync(conflicting)

            assert (
                connection.info.transaction_status
                == psycopg.pq.TransactionStatus.INTRANS
            )
            assert connection.execute(
                "SELECT count(*) FROM public.traces WHERE trace_id = %s",
                (outer_trace_ids[0],),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT count(*) FROM public.traces WHERE trace_id = %s",
                ("trace_sync_extra",),
            ).fetchone() == (0,)
            connection.execute(
                "INSERT INTO public.traces (trace_id, run_id, commit_sha) "
                "VALUES (%s, %s, %s)",
                (outer_trace_ids[1], "run_outer_after", "commit_outer_after"),
            )

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as observer:
        assert observer.execute(
            "SELECT trace_id FROM public.traces WHERE trace_id = ANY(%s) "
            "ORDER BY trace_id",
            (list(outer_trace_ids),),
        ).fetchall() == [("trace_outer_after",), ("trace_outer_before",)]
        assert observer.execute(
            "SELECT count(*) FROM public.traces WHERE trace_id = %s",
            ("trace_sync_extra",),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("holder_operation", "contender_operation"),
    [("load", "sync"), ("sync", "load")],
)
def test_schema_share_and_update_locks_serialize_in_both_directions(
    postgres_cluster,
    holder_operation,
    contender_operation,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresPersistenceError,
    )

    def operate(repository, operation):
        if operation == "load":
            return repository.load()
        return repository.sync(TraceBackedMemoryStore())

    postgres_cluster.load_schema()
    with (
        psycopg.connect(**postgres_cluster.connection_kwargs()) as holder,
        psycopg.connect(**postgres_cluster.connection_kwargs()) as contender,
    ):
        holder_repository = PostgresMemoryRepository(holder)
        contender_repository = PostgresMemoryRepository(contender)
        contender.execute("SET lock_timeout = '100ms'")
        contender.commit()

        with holder.transaction():
            operate(holder_repository, holder_operation)
            with pytest.raises(PostgresPersistenceError) as error:
                operate(contender_repository, contender_operation)

            assert type(error.value.__cause__) is psycopg.errors.LockNotAvailable

        result = operate(contender_repository, contender_operation)

    if contender_operation == "load":
        assert result.to_snapshot()["traces"] == []
    else:
        assert result.traces.inserted == 0


def test_repository_sync_preserves_database_only_trace_case_chains(postgres_cluster):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository, PostgresSyncCounts

    postgres_cluster.load_schema()
    store_a = _draft_case_store(suffix="additive_a")
    store_b = _draft_case_store(suffix="additive_b")
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = PostgresMemoryRepository(connection)
        repository.sync(store_a)
        repository.sync(store_b)

        repeated_a = repository.sync(store_a)
        assert repeated_a.traces == PostgresSyncCounts(unchanged=1)
        assert repeated_a.failure_cases == PostgresSyncCounts(unchanged=1)
        assert connection.execute(
            "SELECT trace_id FROM public.traces ORDER BY trace_id"
        ).fetchall() == [("trace_additive_a",), ("trace_additive_b",)]
        assert connection.execute(
            "SELECT case_id FROM public.failure_cases ORDER BY case_id"
        ).fetchall() == [("case_additive_a",), ("case_additive_b",)]

        loaded = repository.load()
        assert set(loaded.traces) == {"trace_additive_a", "trace_additive_b"}
        assert set(loaded.failure_cases) == {"case_additive_a", "case_additive_b"}

    adapter_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "trace_backed_memory"
        / "postgres.py"
    ).read_text(encoding="utf-8")
    assert "DELETE" not in adapter_source.upper()
    assert "TRUNCATE" not in adapter_source.upper()


def test_repository_sync_wraps_driver_errors_with_sanitized_row_context(
    postgres_cluster,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import Trace, TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresPersistenceError,
    )

    postgres_cluster.load_schema()
    verified = _draft_case_store(suffix="driver_error")
    parameter_canaries = (
        "parameter-reviewer-secret",
        "parameter-payload-secret",
        "parameter-sql-secret",
        "parameter-fix-secret",
        "parameter-fix-commit-secret",
    )
    verified.review_failure_case(
        "case_driver_error",
        reviewed_by=parameter_canaries[0],
        root_cause=f'{{"api_key":"{parameter_canaries[1]}"}}',
        review_notes=f'params=("{parameter_canaries[2]}",)',
    )
    verified.verify_failure_case(
        "case_driver_error",
        fix=parameter_canaries[3],
        fix_commit_sha=parameter_canaries[4],
        regression_passed=True,
    )
    reverse_snapshot = verified.to_snapshot()
    reverse_snapshot["failure_cases"][0]["status"] = "draft"
    reverse = TraceBackedMemoryStore.from_snapshot(reverse_snapshot)
    reverse.record_trace(
        Trace(
            trace_id="trace_driver_error_extra",
            run_id="run_driver_error_extra",
            commit_sha="commit_driver_error_extra",
        )
    )
    reverse_case = reverse.failure_cases["case_driver_error"]
    assert reverse_case.reviewed_by == parameter_canaries[0]
    assert parameter_canaries[1] in reverse_case.root_cause
    assert parameter_canaries[2] in reverse_case.review_notes
    assert reverse_case.fix == parameter_canaries[3]
    assert reverse_case.fix_commit_sha == parameter_canaries[4]

    connection_canary = "dsn-application-name-secret"
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        application_name=connection_canary,
    ) as connection:
        assert connection_canary in connection.info.dsn
        assert connection.execute(
            "SELECT current_setting('application_name')"
        ).fetchone() == (connection_canary,)
        repository = PostgresMemoryRepository(connection)
        repository.sync(verified)

        with pytest.raises(PostgresPersistenceError) as error:
            repository.sync(reverse)

        message = str(error.value)
        assert "sync" in message
        assert "failure_cases" in message
        assert "case_driver_error" in message
        for canary in (*parameter_canaries, connection_canary):
            assert canary not in message
        assert isinstance(error.value.__cause__, psycopg.Error)
        loaded = repository.load()
        assert loaded.failure_cases["case_driver_error"].status == "verified"
        assert "trace_driver_error_extra" not in loaded.traces


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
            "UPDATE public.failure_cases SET created_at = %s WHERE case_id = %s",
            "2026-01-01T00:00:00Z",
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
        with pytest.raises(PostgresSchemaError, match="schema is missing or incomplete"):
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


@pytest.mark.parametrize("schema_state", ["missing", "incomplete"])
@pytest.mark.parametrize("operation", ["load", "sync"])
def test_missing_or_incomplete_schema_keeps_sanitized_driver_cause(
    postgres_cluster,
    schema_state,
    operation,
):
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory import Trace, TraceBackedMemoryStore
    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresSchemaError,
    )

    connection_canary = "schema-connection-canary"
    parameter_canary = "schema-parameter-canary"
    json_canary = "schema-json-canary"
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        application_name=connection_canary,
    ) as connection:
        assert connection_canary in connection.info.dsn
        if schema_state == "incomplete":
            connection.execute(
                "CREATE TABLE public.trace_backed_memory_schema ("
                "singleton boolean PRIMARY KEY CHECK (singleton), "
                "schema_version integer NOT NULL)"
            )
            connection.execute(
                "INSERT INTO public.trace_backed_memory_schema "
                "(singleton, schema_version) VALUES (true, 1)"
            )
            connection.commit()

        store = TraceBackedMemoryStore()
        store.record_trace(
            Trace(
                trace_id=parameter_canary,
                run_id="schema-run-canary",
                commit_sha="schema-commit-canary",
                retrieved_context=[{"secret": json_canary}],
            )
        )
        repository = PostgresMemoryRepository(connection)

        with pytest.raises(PostgresSchemaError) as error:
            if operation == "load":
                repository.load()
            else:
                repository.sync(store)

    assert str(error.value) == "PostgreSQL schema is missing or incomplete"
    assert type(error.value.__cause__) is psycopg.errors.UndefinedTable
    for canary in (connection_canary, parameter_canary, json_canary):
        assert canary not in str(error.value)


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


def test_connect_wraps_driver_failure_without_exposing_conninfo():
    psycopg = pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import (
        PostgresMemoryRepository,
        PostgresPersistenceError,
    )

    canaries = (
        "conninfo-user-canary",
        "conninfo-password-canary",
        "conninfo-database-canary",
        "conninfo-application-canary",
    )
    conninfo = (
        "host=127.0.0.1 port=not-a-port "
        f"user={canaries[0]} password={canaries[1]} "
        f"dbname={canaries[2]} application_name={canaries[3]}"
    )

    with pytest.raises(PostgresPersistenceError) as error:
        PostgresMemoryRepository.connect(conninfo)

    assert str(error.value) == "failed to connect to PostgreSQL"
    assert type(error.value.__cause__) is psycopg.OperationalError
    for canary in canaries:
        assert canary not in str(error.value)


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


def test_owned_connect_context_closes_real_postgres_connection(postgres_cluster):
    pytest.importorskip("psycopg")
    from trace_backed_memory.postgres import PostgresMemoryRepository

    with PostgresMemoryRepository.connect(
        **postgres_cluster.connection_kwargs()
    ) as repository:
        connection = repository._connection
        assert connection.closed is False
        assert connection.execute("SELECT 1").fetchone() == {"?column?": 1}

    assert connection.closed is True


def test_operations_after_close_fail():
    from trace_backed_memory.postgres import PostgresAdapterError, PostgresMemoryRepository

    repository = PostgresMemoryRepository(_FakeConnection())
    repository.close()

    with pytest.raises(PostgresAdapterError, match="repository is closed"):
        repository.load()
