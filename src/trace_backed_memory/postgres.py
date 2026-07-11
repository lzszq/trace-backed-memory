from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any

from .store import TraceBackedMemoryStore


POSTGRES_SCHEMA_VERSION = 1


_SELECT_TRACES = """
SELECT trace_id, run_id, commit_sha, repo, tenant, branch, dirty,
       prompt_version, prompt_family, tool_schema_version, model, eval_suite,
       input_hash, output_hash, retrieved_context, tool_calls, tool_outputs,
       eval_result, latency_ms, cost_usd, error, trace_uri, created_at
FROM public.traces
ORDER BY trace_id
"""

_SELECT_FAILURE_CASES = """
SELECT case_id, source_trace_id, commit_sha, failure_type, symptom, root_cause,
       fix, fix_commit_sha, regression_passed, reviewed_by, review_notes,
       reviewed_at, status, created_at
FROM public.failure_cases
ORDER BY case_id
"""

_SELECT_LESSONS = """
SELECT lesson_id, source_case_id, lesson_text, memory_type, scope_json,
       confidence, sensitive, eval_leaking, status, created_at
FROM public.lessons
ORDER BY lesson_id
"""

_SELECT_PROJECT_POLICIES = """
SELECT policy_id, policy_text, scope_json, confidence, sensitive, eval_leaking,
       status, created_at
FROM public.project_policies
ORDER BY policy_id
"""

_SELECT_USAGE_LOGS = """
SELECT decision_id, run_id, mode, candidate_memory_ids, used_memory_ids,
       blocked_memory_ids, reason, risk, recommended_injection, eval_result,
       memory_caused_failure, trace_id, context, candidate_memory_statuses,
       system_blocked_reasons, created_at
FROM public.memory_usage_decisions
ORDER BY decision_id
"""


class PostgresAdapterError(RuntimeError):
    pass


class PostgresDependencyError(PostgresAdapterError):
    pass


class PostgresSchemaError(PostgresAdapterError):
    pass


class PostgresConflictError(PostgresAdapterError):
    pass


class PostgresPersistenceError(PostgresAdapterError):
    pass


@dataclass(frozen=True)
class PostgresSyncCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class PostgresSyncResult:
    traces: PostgresSyncCounts
    failure_cases: PostgresSyncCounts
    lessons: PostgresSyncCounts
    project_policies: PostgresSyncCounts
    usage_logs: PostgresSyncCounts


def _load_psycopg() -> tuple[Any, Any, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise PostgresDependencyError(
            "PostgreSQL support requires: pip install 'trace-backed-memory[postgres]'"
        ) from exc
    return psycopg, dict_row, Jsonb


def _rfc3339(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be an aware datetime or None")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _numeric_cost(value: object) -> int | float | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("cost_usd must be a finite Decimal or None")
    if value == value.to_integral_value():
        return int(value)
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("cost_usd must be a finite JSON number or None")
    return converted


def _numeric_confidence(value: object, field_name: str) -> float:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{field_name} must be a finite float")
    return converted


def _json_value(value: object, field_name: str) -> object:
    if not isinstance(value, (list, dict)):
        raise ValueError(f"{field_name} must be a JSON list or object")
    return deepcopy(value)


class PostgresMemoryRepository:
    def __init__(self, connection: object, *, owns_connection: bool = False) -> None:
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: object) -> "PostgresMemoryRepository":
        psycopg, dict_row, _Jsonb = _load_psycopg()
        connection = psycopg.connect(conninfo, row_factory=dict_row, **kwargs)
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresAdapterError("PostgreSQL repository is closed")

    def _lock_schema(self, cursor: object, *, write: bool) -> None:
        lock = "UPDATE" if write else "SHARE"
        cursor.execute(
            f"SELECT schema_version FROM public.trace_backed_memory_schema "
            f"WHERE singleton FOR {lock}"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise PostgresSchemaError("PostgreSQL schema metadata must contain exactly one row")
        version = rows[0]["schema_version"]
        if version != POSTGRES_SCHEMA_VERSION:
            raise PostgresSchemaError(
                f"PostgreSQL schema version mismatch: expected 1, found {version}"
            )

    def _load_traces(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_TRACES)
        return [
            {
                "trace_id": row["trace_id"],
                "run_id": row["run_id"],
                "commit_sha": row["commit_sha"],
                "repo": row["repo"],
                "tenant": row["tenant"],
                "branch": row["branch"],
                "dirty": row["dirty"],
                "prompt_version": row["prompt_version"],
                "prompt_family": row["prompt_family"],
                "tool_schema_version": row["tool_schema_version"],
                "model": row["model"],
                "eval_suite": row["eval_suite"],
                "input_hash": row["input_hash"],
                "output_hash": row["output_hash"],
                "retrieved_context": _json_value(
                    row["retrieved_context"], "traces.retrieved_context"
                ),
                "tool_calls": _json_value(row["tool_calls"], "traces.tool_calls"),
                "tool_outputs": _json_value(row["tool_outputs"], "traces.tool_outputs"),
                "eval_result": row["eval_result"],
                "latency_ms": row["latency_ms"],
                "cost_usd": _numeric_cost(row["cost_usd"]),
                "error": row["error"],
                "trace_uri": row["trace_uri"],
                "created_at": _rfc3339(row["created_at"], "traces.created_at"),
            }
            for row in cursor.fetchall()
        ]

    def _load_failure_cases(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_FAILURE_CASES)
        return [
            {
                "case_id": row["case_id"],
                "source_trace_id": row["source_trace_id"],
                "commit_sha": row["commit_sha"],
                "failure_type": row["failure_type"],
                "symptom": row["symptom"],
                "root_cause": row["root_cause"],
                "fix": row["fix"],
                "fix_commit_sha": row["fix_commit_sha"],
                "regression_passed": row["regression_passed"],
                "reviewed_by": row["reviewed_by"],
                "review_notes": row["review_notes"],
                "reviewed_at": _rfc3339(row["reviewed_at"], "failure_cases.reviewed_at"),
                "status": row["status"],
                "created_at": _rfc3339(row["created_at"], "failure_cases.created_at"),
            }
            for row in cursor.fetchall()
        ]

    def _load_lessons(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_LESSONS)
        return [
            {
                "lesson_id": row["lesson_id"],
                "source_case_id": row["source_case_id"],
                "lesson_text": row["lesson_text"],
                "memory_type": row["memory_type"],
                "scope": _json_value(row["scope_json"], "lessons.scope_json"),
                "confidence": _numeric_confidence(row["confidence"], "lessons.confidence"),
                "sensitive": row["sensitive"],
                "eval_leaking": row["eval_leaking"],
                "status": row["status"],
                "created_at": _rfc3339(row["created_at"], "lessons.created_at"),
            }
            for row in cursor.fetchall()
        ]

    def _load_project_policies(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_PROJECT_POLICIES)
        return [
            {
                "policy_id": row["policy_id"],
                "policy_text": row["policy_text"],
                "scope": _json_value(row["scope_json"], "project_policies.scope_json"),
                "confidence": _numeric_confidence(
                    row["confidence"], "project_policies.confidence"
                ),
                "sensitive": row["sensitive"],
                "eval_leaking": row["eval_leaking"],
                "status": row["status"],
                "created_at": _rfc3339(
                    row["created_at"], "project_policies.created_at"
                ),
            }
            for row in cursor.fetchall()
        ]

    def _load_usage_logs(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_USAGE_LOGS)
        return [
            {
                "decision_id": row["decision_id"],
                "run_id": row["run_id"],
                "mode": row["mode"],
                "candidate_memory_ids": _json_value(
                    row["candidate_memory_ids"], "memory_usage_decisions.candidate_memory_ids"
                ),
                "used_memory_ids": _json_value(
                    row["used_memory_ids"], "memory_usage_decisions.used_memory_ids"
                ),
                "blocked_memory_ids": _json_value(
                    row["blocked_memory_ids"], "memory_usage_decisions.blocked_memory_ids"
                ),
                "reason": row["reason"],
                "risk": row["risk"],
                "recommended_injection": row["recommended_injection"],
                "eval_result": row["eval_result"],
                "memory_caused_failure": row["memory_caused_failure"],
                "trace_id": row["trace_id"],
                "context": _json_value(row["context"], "memory_usage_decisions.context"),
                "candidate_memory_statuses": _json_value(
                    row["candidate_memory_statuses"],
                    "memory_usage_decisions.candidate_memory_statuses",
                ),
                "system_blocked_reasons": _json_value(
                    row["system_blocked_reasons"],
                    "memory_usage_decisions.system_blocked_reasons",
                ),
                "created_at": _rfc3339(
                    row["created_at"], "memory_usage_decisions.created_at"
                ),
            }
            for row in cursor.fetchall()
        ]

    def load(self) -> TraceBackedMemoryStore:
        self._require_open()
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, write=False)
                    snapshot = {
                        "snapshot_version": 2,
                        "traces": self._load_traces(cursor),
                        "failure_cases": self._load_failure_cases(cursor),
                        "lessons": self._load_lessons(cursor),
                        "project_policies": self._load_project_policies(cursor),
                        "usage_logs": self._load_usage_logs(cursor),
                    }
            return TraceBackedMemoryStore.from_snapshot(snapshot)
        except PostgresSchemaError:
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42P01":
                raise PostgresSchemaError(
                    "PostgreSQL schema metadata is missing"
                ) from None
            if isinstance(exc, (psycopg.Error, TypeError, ValueError, OverflowError)):
                raise PostgresPersistenceError("failed to load memory store from PostgreSQL") from exc
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> "PostgresMemoryRepository":
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
