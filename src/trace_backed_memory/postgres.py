from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any, Literal

from .store import TraceBackedMemoryStore


POSTGRES_SCHEMA_VERSION = 1


_LOCK_SCHEMA_FOR_SHARE = """
SELECT schema_version
FROM public.trace_backed_memory_schema
WHERE singleton
FOR SHARE
"""

_LOCK_SCHEMA_FOR_UPDATE = """
SELECT schema_version
FROM public.trace_backed_memory_schema
WHERE singleton
FOR UPDATE
"""

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

_SELECT_TRACE_BY_ID = """
SELECT trace_id, run_id, commit_sha, repo, tenant, branch, dirty,
       prompt_version, prompt_family, tool_schema_version, model, eval_suite,
       input_hash, output_hash, retrieved_context, tool_calls, tool_outputs,
       eval_result, latency_ms, cost_usd, error, trace_uri, created_at
FROM public.traces
WHERE trace_id = %s
"""

_SELECT_FAILURE_CASE_BY_ID = """
SELECT case_id, source_trace_id, commit_sha, failure_type, symptom, root_cause,
       fix, fix_commit_sha, regression_passed, reviewed_by, review_notes,
       reviewed_at, status, created_at
FROM public.failure_cases
WHERE case_id = %s
"""

_SELECT_LESSON_BY_ID = """
SELECT lesson_id, source_case_id, lesson_text, memory_type, scope_json,
       confidence, sensitive, eval_leaking, status, created_at
FROM public.lessons
WHERE lesson_id = %s
"""

_SELECT_PROJECT_POLICY_BY_ID = """
SELECT policy_id, policy_text, scope_json, confidence, sensitive, eval_leaking,
       status, created_at
FROM public.project_policies
WHERE policy_id = %s
"""

_SELECT_USAGE_LOG_BY_ID = """
SELECT decision_id, run_id, mode, candidate_memory_ids, used_memory_ids,
       blocked_memory_ids, reason, risk, recommended_injection, eval_result,
       memory_caused_failure, trace_id, context, candidate_memory_statuses,
       system_blocked_reasons, created_at
FROM public.memory_usage_decisions
WHERE decision_id = %s
"""

_INSERT_TRACE = """
INSERT INTO public.traces (
    trace_id, run_id, commit_sha, repo, tenant, branch, dirty,
    prompt_version, prompt_family, tool_schema_version, model, eval_suite,
    input_hash, output_hash, retrieved_context, tool_calls, tool_outputs,
    eval_result, latency_ms, cost_usd, error, trace_uri, created_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
"""

_INSERT_FAILURE_CASE = """
INSERT INTO public.failure_cases (
    case_id, source_trace_id, commit_sha, failure_type, symptom, root_cause,
    fix, fix_commit_sha, regression_passed, reviewed_by, review_notes,
    reviewed_at, status, created_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_LESSON = """
INSERT INTO public.lessons (
    lesson_id, source_case_id, lesson_text, memory_type, scope_json,
    confidence, sensitive, eval_leaking, status, created_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_PROJECT_POLICY = """
INSERT INTO public.project_policies (
    policy_id, policy_text, scope_json, confidence, sensitive, eval_leaking,
    status, created_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_USAGE_LOG = """
INSERT INTO public.memory_usage_decisions (
    decision_id, run_id, mode, candidate_memory_ids, used_memory_ids,
    blocked_memory_ids, reason, risk, recommended_injection, eval_result,
    memory_caused_failure, trace_id, context, candidate_memory_statuses,
    system_blocked_reasons, created_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
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


def _decode_trace(row: dict[str, object]) -> dict[str, object]:
    return {
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


def _decode_failure_case(row: dict[str, object]) -> dict[str, object]:
    return {
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
        "reviewed_at": _rfc3339(
            row["reviewed_at"], "failure_cases.reviewed_at"
        ),
        "status": row["status"],
        "created_at": _rfc3339(row["created_at"], "failure_cases.created_at"),
    }


def _decode_lesson(row: dict[str, object]) -> dict[str, object]:
    return {
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


def _decode_project_policy(row: dict[str, object]) -> dict[str, object]:
    return {
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


def _decode_usage_log(row: dict[str, object]) -> dict[str, object]:
    return {
        "decision_id": row["decision_id"],
        "run_id": row["run_id"],
        "mode": row["mode"],
        "candidate_memory_ids": _json_value(
            row["candidate_memory_ids"],
            "memory_usage_decisions.candidate_memory_ids",
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


def _encode_trace(record: dict[str, object], Jsonb: Any) -> tuple[object, ...]:
    return (
        record["trace_id"],
        record["run_id"],
        record["commit_sha"],
        record["repo"],
        record["tenant"],
        record["branch"],
        record["dirty"],
        record["prompt_version"],
        record["prompt_family"],
        record["tool_schema_version"],
        record["model"],
        record["eval_suite"],
        record["input_hash"],
        record["output_hash"],
        Jsonb(deepcopy(record["retrieved_context"])),
        Jsonb(deepcopy(record["tool_calls"])),
        Jsonb(deepcopy(record["tool_outputs"])),
        record["eval_result"],
        record["latency_ms"],
        record["cost_usd"],
        record["error"],
        record["trace_uri"],
        record["created_at"],
    )


def _encode_failure_case(record: dict[str, object], Jsonb: Any) -> tuple[object, ...]:
    return (
        record["case_id"],
        record["source_trace_id"],
        record["commit_sha"],
        record["failure_type"],
        record["symptom"],
        record["root_cause"],
        record["fix"],
        record["fix_commit_sha"],
        record["regression_passed"],
        record["reviewed_by"],
        record["review_notes"],
        record["reviewed_at"],
        record["status"],
        record["created_at"],
    )


def _encode_lesson(record: dict[str, object], Jsonb: Any) -> tuple[object, ...]:
    return (
        record["lesson_id"],
        record["source_case_id"],
        record["lesson_text"],
        record["memory_type"],
        Jsonb(deepcopy(record["scope"])),
        record["confidence"],
        record["sensitive"],
        record["eval_leaking"],
        record["status"],
        record["created_at"],
    )


def _encode_project_policy(
    record: dict[str, object], Jsonb: Any
) -> tuple[object, ...]:
    return (
        record["policy_id"],
        record["policy_text"],
        Jsonb(deepcopy(record["scope"])),
        record["confidence"],
        record["sensitive"],
        record["eval_leaking"],
        record["status"],
        record["created_at"],
    )


def _encode_usage_log(record: dict[str, object], Jsonb: Any) -> tuple[object, ...]:
    return (
        record["decision_id"],
        record["run_id"],
        record["mode"],
        Jsonb(deepcopy(record["candidate_memory_ids"])),
        Jsonb(deepcopy(record["used_memory_ids"])),
        Jsonb(deepcopy(record["blocked_memory_ids"])),
        record["reason"],
        record["risk"],
        record["recommended_injection"],
        record["eval_result"],
        record["memory_caused_failure"],
        record["trace_id"],
        Jsonb(deepcopy(record["context"])),
        Jsonb(deepcopy(record["candidate_memory_statuses"])),
        Jsonb(deepcopy(record["system_blocked_reasons"])),
        record["created_at"],
    )


_ROW_CODECS = {
    "traces": (_decode_trace, _encode_trace),
    "failure_cases": (_decode_failure_case, _encode_failure_case),
    "lessons": (_decode_lesson, _encode_lesson),
    "project_policies": (_decode_project_policy, _encode_project_policy),
    "memory_usage_decisions": (_decode_usage_log, _encode_usage_log),
}

_TIMESTAMP_FIELDS = {
    "traces": ("created_at",),
    "failure_cases": ("reviewed_at", "created_at"),
    "lessons": ("created_at",),
    "project_policies": ("created_at",),
    "memory_usage_decisions": ("created_at",),
}


def _canonical_rfc3339(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an RFC 3339 string or None")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 string or None") from exc
    return _rfc3339(parsed, field_name)


def _canonical_numeric_cost(value: object) -> int | float | None:
    if value is None:
        return None
    if type(value) is int:
        return _numeric_cost(Decimal(value))
    if type(value) is float and math.isfinite(value):
        return _numeric_cost(Decimal(str(value)))
    raise ValueError("cost_usd must be a finite JSON number or None")


def _canonical_numeric_confidence(value: object, field_name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite JSON number")
    return float(value)


def _canonical_incoming_record(
    table: str, incoming: dict[str, object]
) -> dict[str, object]:
    canonical = dict(incoming)
    for field_name in _TIMESTAMP_FIELDS[table]:
        canonical[field_name] = _canonical_rfc3339(
            canonical[field_name], f"{table}.{field_name}"
        )
    if table == "traces":
        canonical["cost_usd"] = _canonical_numeric_cost(canonical["cost_usd"])
    elif table == "lessons":
        canonical["confidence"] = _canonical_numeric_confidence(
            canonical["confidence"], "lessons.confidence"
        )
    elif table == "project_policies":
        canonical["confidence"] = _canonical_numeric_confidence(
            canonical["confidence"], "project_policies.confidence"
        )
    return canonical


def _type_strict_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _type_strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _type_strict_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _sync_immutable_row(
    cursor: object,
    *,
    table: str,
    record_id: str,
    incoming: dict[str, object],
    select_sql: str,
    insert_sql: str,
) -> Literal["inserted", "unchanged"]:
    decoder, encoder = _ROW_CODECS[table]
    canonical_incoming = _canonical_incoming_record(table, incoming)
    cursor.execute(select_sql, (record_id,))
    rows = cursor.fetchall()
    if not rows:
        _psycopg, _dict_row, Jsonb = _load_psycopg()
        cursor.execute(insert_sql, encoder(canonical_incoming, Jsonb))
        return "inserted"
    if len(rows) != 1 or not _type_strict_equal(
        decoder(rows[0]), canonical_incoming
    ):
        raise PostgresConflictError(f"PostgreSQL conflict for {table} row {record_id}")
    return "unchanged"


def _sync_counts(results: list[Literal["inserted", "unchanged"]]) -> PostgresSyncCounts:
    return PostgresSyncCounts(
        inserted=results.count("inserted"),
        unchanged=results.count("unchanged"),
    )


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
        cursor.execute(_LOCK_SCHEMA_FOR_UPDATE if write else _LOCK_SCHEMA_FOR_SHARE)
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
        return [_decode_trace(row) for row in cursor.fetchall()]

    def _load_failure_cases(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_FAILURE_CASES)
        return [_decode_failure_case(row) for row in cursor.fetchall()]

    def _load_lessons(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_LESSONS)
        return [_decode_lesson(row) for row in cursor.fetchall()]

    def _load_project_policies(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_PROJECT_POLICIES)
        return [_decode_project_policy(row) for row in cursor.fetchall()]

    def _load_usage_logs(self, cursor: object) -> list[dict[str, object]]:
        cursor.execute(_SELECT_USAGE_LOGS)
        return [_decode_usage_log(row) for row in cursor.fetchall()]

    def sync(self, store: TraceBackedMemoryStore) -> PostgresSyncResult:
        self._require_open()
        snapshot = store.to_snapshot()
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, write=True)
                    traces = [
                        _sync_immutable_row(
                            cursor,
                            table="traces",
                            record_id=record["trace_id"],
                            incoming=record,
                            select_sql=_SELECT_TRACE_BY_ID,
                            insert_sql=_INSERT_TRACE,
                        )
                        for record in sorted(
                            snapshot["traces"], key=lambda item: item["trace_id"]
                        )
                    ]
                    failure_cases = [
                        _sync_immutable_row(
                            cursor,
                            table="failure_cases",
                            record_id=record["case_id"],
                            incoming=record,
                            select_sql=_SELECT_FAILURE_CASE_BY_ID,
                            insert_sql=_INSERT_FAILURE_CASE,
                        )
                        for record in sorted(
                            snapshot["failure_cases"], key=lambda item: item["case_id"]
                        )
                    ]
                    lessons = [
                        _sync_immutable_row(
                            cursor,
                            table="lessons",
                            record_id=record["lesson_id"],
                            incoming=record,
                            select_sql=_SELECT_LESSON_BY_ID,
                            insert_sql=_INSERT_LESSON,
                        )
                        for record in sorted(
                            snapshot["lessons"], key=lambda item: item["lesson_id"]
                        )
                    ]
                    project_policies = [
                        _sync_immutable_row(
                            cursor,
                            table="project_policies",
                            record_id=record["policy_id"],
                            incoming=record,
                            select_sql=_SELECT_PROJECT_POLICY_BY_ID,
                            insert_sql=_INSERT_PROJECT_POLICY,
                        )
                        for record in sorted(
                            snapshot["project_policies"],
                            key=lambda item: item["policy_id"],
                        )
                    ]
                    usage_logs = [
                        _sync_immutable_row(
                            cursor,
                            table="memory_usage_decisions",
                            record_id=record["decision_id"],
                            incoming=record,
                            select_sql=_SELECT_USAGE_LOG_BY_ID,
                            insert_sql=_INSERT_USAGE_LOG,
                        )
                        for record in sorted(
                            snapshot["usage_logs"], key=lambda item: item["decision_id"]
                        )
                    ]
            return PostgresSyncResult(
                traces=_sync_counts(traces),
                failure_cases=_sync_counts(failure_cases),
                lessons=_sync_counts(lessons),
                project_policies=_sync_counts(project_policies),
                usage_logs=_sync_counts(usage_logs),
            )
        except (PostgresConflictError, PostgresSchemaError):
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42P01":
                raise PostgresSchemaError(
                    "PostgreSQL schema metadata is missing"
                ) from None
            if isinstance(exc, (psycopg.Error, TypeError, ValueError, OverflowError)):
                raise PostgresPersistenceError(
                    "failed to sync memory store to PostgreSQL"
                ) from exc
            raise

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
