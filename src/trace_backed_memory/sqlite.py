from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal

from ._ingestion import (
    SNAPSHOT_FILE_MAX_BYTES,
    SNAPSHOT_MAX_RECORDS_PER_COLLECTION,
    SNAPSHOT_MAX_TOTAL_RECORDS,
    unique_json_object_pairs,
    validate_snapshot_record_count,
    validate_snapshot_total_record_count,
)
from ._timestamps import canonical_rfc3339
from .resources import PackagedResourceError, read_packaged_resource
from .store import TraceBackedMemoryStore


SQLITE_SCHEMA_VERSION = 1
_SQLITE_LOAD_MAX_RECORD_BYTES = SNAPSHOT_FILE_MAX_BYTES
_SQLITE_LOAD_MAX_TOTAL_PAYLOAD_BYTES = SNAPSHOT_FILE_MAX_BYTES
_SQLITE_PAYLOAD_MAX_NODES = 100_000
_SQLITE_PAYLOAD_MAX_DEPTH = 100
_MEASURED_EVAL_RESULTS = frozenset({"pass", "fail", "error"})
_TRACE_COMPLETION_FIELDS = frozenset(
    {
        "output_hash",
        "tool_outputs",
        "eval_result",
        "latency_ms",
        "cost_usd",
        "error",
        "trace_uri",
    }
)
_TRACE_OPTIONAL_COMPLETION_FIELDS = (
    "output_hash",
    "tool_outputs",
    "latency_ms",
    "cost_usd",
    "error",
    "trace_uri",
)
_USAGE_OUTCOME_FIELDS = frozenset({"eval_result", "memory_caused_failure"})
_MISSING_SCHEMA_MESSAGE = "SQLite schema is missing or incomplete"

_COLLECTION_SPECS = (
    ("traces", "traces", "trace_id"),
    ("failure_cases", "failure_cases", "case_id"),
    ("lessons", "lessons", "lesson_id"),
    ("project_policies", "project_policies", "policy_id"),
    ("usage_logs", "memory_usage_decisions", "decision_id"),
)
_SPEC_BY_COLLECTION = {
    collection: (table, id_field)
    for collection, table, id_field in _COLLECTION_SPECS
}
_MEMORY_COLLECTIONS = frozenset(
    {"failure_cases", "lessons", "project_policies"}
)
_TIMESTAMP_FIELDS = {
    "traces": ("created_at",),
    "failure_cases": ("reviewed_at", "created_at"),
    "lessons": ("created_at",),
    "project_policies": ("created_at",),
    "usage_logs": ("created_at",),
}


class SQLiteAdapterError(RuntimeError):
    pass


class SQLiteSchemaError(SQLiteAdapterError):
    pass


class SQLiteConflictError(SQLiteAdapterError):
    pass


class SQLitePersistenceError(SQLiteAdapterError):
    pass


@dataclass(frozen=True)
class SQLiteSyncCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class SQLiteSyncResult:
    traces: SQLiteSyncCounts
    failure_cases: SQLiteSyncCounts
    lessons: SQLiteSyncCounts
    project_policies: SQLiteSyncCounts
    usage_logs: SQLiteSyncCounts


def _validate_snapshot_record_counts(counts: Mapping[str, object]) -> None:
    if not isinstance(counts, Mapping):
        raise ValueError("SQLite snapshot counts must be a mapping")
    total_records = 0
    for collection_name, _table, _id_field in _COLLECTION_SPECS:
        if collection_name not in counts:
            raise ValueError(
                "SQLite snapshot counts are missing field "
                f"{collection_name!r}"
            )
        total_records += validate_snapshot_record_count(
            collection_name,
            counts[collection_name],
            max_records_per_collection=SNAPSHOT_MAX_RECORDS_PER_COLLECTION,
        )
    validate_snapshot_total_record_count(
        total_records,
        max_total_records=SNAPSHOT_MAX_TOTAL_RECORDS,
    )


def _validate_snapshot_payload_sizes(sizes: Mapping[str, object]) -> None:
    if not isinstance(sizes, Mapping):
        raise ValueError("SQLite snapshot payload sizes must be a mapping")
    validated: dict[str, int] = {}
    for field_name in ("max_record_bytes", "total_bytes"):
        if field_name not in sizes:
            raise ValueError(
                "SQLite snapshot payload sizes are missing field "
                f"{field_name!r}"
            )
        value = sizes[field_name]
        if type(value) is not int or value < 0:
            raise ValueError(
                f"SQLite snapshot payload field {field_name!r} must be a "
                "non-negative integer"
            )
        validated[field_name] = value

    max_record_bytes = validated["max_record_bytes"]
    total_bytes = validated["total_bytes"]
    if max_record_bytes > total_bytes:
        raise ValueError(
            "SQLite snapshot maximum record payload cannot exceed total payload"
        )
    if max_record_bytes > _SQLITE_LOAD_MAX_RECORD_BYTES:
        raise ValueError(
            "SQLite snapshot record payload contains "
            f"{max_record_bytes} bytes; maximum is "
            f"{_SQLITE_LOAD_MAX_RECORD_BYTES}"
        )
    if total_bytes > _SQLITE_LOAD_MAX_TOTAL_PAYLOAD_BYTES:
        raise ValueError(
            f"SQLite snapshot payload contains {total_bytes} bytes; maximum "
            f"is {_SQLITE_LOAD_MAX_TOTAL_PAYLOAD_BYTES}"
        )


def _canonical_rfc3339(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return canonical_rfc3339(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 string or None") from exc


def _synchronized(method):
    @wraps(method)
    def synchronized(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return synchronized


def _canonical_numeric_cost(value: object) -> int | float | None:
    if value is None:
        return None
    if type(value) is int:
        numeric = Decimal(value)
    elif type(value) is float and math.isfinite(value):
        numeric = Decimal(str(value))
    else:
        raise ValueError("cost_usd must be a finite JSON number or None")
    if numeric == numeric.to_integral_value():
        return int(numeric)
    converted = float(numeric)
    if not math.isfinite(converted):
        raise ValueError("cost_usd must be a finite JSON number or None")
    return converted


def _canonical_numeric_confidence(value: object, field_name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite JSON number")
    return float(value)


def _canonical_record(
    collection: str,
    record: Mapping[str, object],
) -> dict[str, object]:
    if type(record) is not dict:
        raise ValueError(f"SQLite {collection} payload must be a JSON object")
    canonical = dict(record)
    if collection == "usage_logs":
        canonical.setdefault("request_id", None)
    try:
        for field_name in _TIMESTAMP_FIELDS[collection]:
            canonical[field_name] = _canonical_rfc3339(
                canonical[field_name], f"{collection}.{field_name}"
            )
        if collection == "traces":
            canonical["cost_usd"] = _canonical_numeric_cost(
                canonical["cost_usd"]
            )
        elif collection == "lessons":
            canonical["confidence"] = _canonical_numeric_confidence(
                canonical["confidence"], "lessons.confidence"
            )
        elif collection == "project_policies":
            canonical["confidence"] = _canonical_numeric_confidence(
                canonical["confidence"], "project_policies.confidence"
            )
    except KeyError as exc:
        raise ValueError(
            f"SQLite {collection} payload is missing field {exc.args[0]!r}"
        ) from exc
    return canonical


def _canonical_values_equal(left: object, right: object) -> bool:
    left_is_number = type(left) in (int, float)
    right_is_number = type(right) in (int, float)
    if left_is_number or right_is_number:
        if not (left_is_number and right_is_number):
            return False
        if (type(left) is float and not math.isfinite(left)) or (
            type(right) is float and not math.isfinite(right)
        ):
            return False
        return Decimal(str(left)) == Decimal(str(right))
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _canonical_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _canonical_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _trace_completion_slot_is_empty(field_name: str, value: object) -> bool:
    if field_name == "tool_outputs":
        return value == []
    return value is None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"SQLite payload contains invalid JSON constant: {value}")


def _validate_payload_budget(collection: str, value: object) -> None:
    node_count = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if node_count > _SQLITE_PAYLOAD_MAX_NODES:
            raise ValueError(
                f"SQLite {collection} payload contains more than "
                f"{_SQLITE_PAYLOAD_MAX_NODES} nodes"
            )
        if type(current) not in {dict, list}:
            continue
        if current and depth >= _SQLITE_PAYLOAD_MAX_DEPTH:
            raise ValueError(
                f"SQLite {collection} payload exceeds maximum nesting depth "
                f"{_SQLITE_PAYLOAD_MAX_DEPTH}"
            )
        children = (
            current.values() if type(current) is dict else current
        )
        if len(current) > _SQLITE_PAYLOAD_MAX_NODES - node_count:
            raise ValueError(
                f"SQLite {collection} payload contains more than "
                f"{_SQLITE_PAYLOAD_MAX_NODES} nodes"
            )
        stack.extend((child, depth + 1) for child in children)


def _encode_payload(collection: str, record: Mapping[str, object]) -> str:
    canonical = _canonical_record(collection, record)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_payload(collection: str, payload: object) -> dict[str, object]:
    if type(payload) is not str:
        raise ValueError(f"SQLite {collection} payload must be text")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=lambda pairs: unique_json_object_pairs(
                pairs,
                description=f"SQLite {collection} payload",
            ),
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"SQLite {collection} payload must be valid JSON") from exc
    _validate_payload_budget(collection, decoded)
    return _canonical_record(collection, decoded)


def _merge_trace(
    stored: dict[str, object],
    incoming: dict[str, object],
) -> Literal["updated", "unchanged"]:
    if stored.keys() != incoming.keys() or any(
        field_name not in _TRACE_COMPLETION_FIELDS
        and not _canonical_values_equal(stored[field_name], incoming[field_name])
        for field_name in incoming
    ):
        raise SQLiteConflictError("trace immutable fields differ")
    if _canonical_values_equal(stored, incoming):
        return "unchanged"
    if (
        stored["eval_result"] == "unknown"
        and incoming["eval_result"] in _MEASURED_EVAL_RESULTS
    ):
        for field_name in _TRACE_OPTIONAL_COMPLETION_FIELDS:
            if (
                not _trace_completion_slot_is_empty(
                    field_name, stored[field_name]
                )
                and not _canonical_values_equal(
                    stored[field_name], incoming[field_name]
                )
            ):
                raise SQLiteConflictError("trace completion evidence differs")
        return "updated"
    raise SQLiteConflictError("trace completion transition is not allowed")


def _merge_failure_case(
    stored: dict[str, object],
    incoming: dict[str, object],
) -> Literal["updated", "unchanged"]:
    if stored.keys() != incoming.keys() or any(
        not _canonical_values_equal(stored[field_name], incoming[field_name])
        for field_name in ("case_id", "source_trace_id", "commit_sha", "created_at")
    ):
        raise SQLiteConflictError("failure-case immutable fields differ")
    if _canonical_values_equal(stored, incoming):
        return "unchanged"
    stored_status = stored["status"]
    incoming_status = incoming["status"]
    if (stored_status == "verified" and incoming_status == "draft") or (
        stored_status == "obsolete" and incoming_status != "obsolete"
    ):
        raise SQLiteConflictError("failure-case status transition is not allowed")
    return "updated"


def _merge_status_record(
    stored: dict[str, object],
    incoming: dict[str, object],
) -> Literal["updated", "unchanged"]:
    if stored.keys() != incoming.keys() or any(
        field_name != "status"
        and not _canonical_values_equal(stored[field_name], incoming[field_name])
        for field_name in incoming
    ):
        raise SQLiteConflictError("runtime-memory immutable fields differ")
    if stored["status"] == incoming["status"]:
        return "unchanged"
    if stored["status"] == "obsolete":
        raise SQLiteConflictError("obsolete runtime memory cannot be reactivated")
    return "updated"


def _merge_usage_log(
    stored: dict[str, object],
    incoming: dict[str, object],
) -> Literal["updated", "unchanged"]:
    if stored.keys() != incoming.keys() or any(
        field_name not in _USAGE_OUTCOME_FIELDS
        and not _canonical_values_equal(stored[field_name], incoming[field_name])
        for field_name in incoming
    ):
        raise SQLiteConflictError("usage-log immutable fields differ")
    if _canonical_values_equal(stored, incoming):
        return "unchanged"
    if (
        stored["eval_result"] not in _MEASURED_EVAL_RESULTS
        and incoming["eval_result"] in _MEASURED_EVAL_RESULTS
    ):
        return "updated"
    raise SQLiteConflictError("usage-log outcome transition is not allowed")


def _merge_record(
    collection: str,
    stored: dict[str, object],
    incoming: dict[str, object],
) -> Literal["updated", "unchanged"]:
    if collection == "traces":
        return _merge_trace(stored, incoming)
    if collection == "failure_cases":
        return _merge_failure_case(stored, incoming)
    if collection in {"lessons", "project_policies"}:
        return _merge_status_record(stored, incoming)
    return _merge_usage_log(stored, incoming)


def _sync_counts(
    results: list[Literal["inserted", "updated", "unchanged"]],
) -> SQLiteSyncCounts:
    return SQLiteSyncCounts(
        inserted=results.count("inserted"),
        updated=results.count("updated"),
        unchanged=results.count("unchanged"),
    )


def _is_schema_error(error: sqlite3.DatabaseError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "no such table",
            "no such column",
            "malformed database schema",
        )
    )


class SQLiteMemoryRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        owns_connection: bool = False,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a sqlite3.Connection")
        self._connection = connection
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False
        self._savepoint_number = 0

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        **kwargs: object,
    ) -> "SQLiteMemoryRepository":
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource("schemas/sqlite.sql").decode("utf-8")
                )
        except (
            OSError,
            UnicodeError,
            sqlite3.Error,
            PackagedResourceError,
            TypeError,
            ValueError,
        ) as exc:
            if "connection" in locals():
                connection.close()
            raise SQLitePersistenceError("failed to connect to SQLite") from exc
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteAdapterError("SQLite repository is closed")
        try:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            raise SQLiteAdapterError("SQLite repository is closed") from exc

    def _rollback_connection_or_close(
        self,
        primary_error: BaseException,
        *,
        context: str,
    ) -> None:
        for attempt in range(2):
            if not self._connection.in_transaction:
                return
            try:
                self._connection.rollback()
            except BaseException as rollback_error:
                prefix = (
                    "failed to roll back"
                    if attempt == 0
                    else "retry failed while rolling back"
                )
                primary_error.add_note(
                    f"{prefix} {context}: {rollback_error}"
                )
                continue
            if not self._connection.in_transaction:
                return
            primary_error.add_note(
                f"rollback attempt left {context} active"
            )

        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to roll back SQLite savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after savepoint "
                            "cleanup failed"
                        ),
                    )
                    return

                try:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    try:
                        self._connection.execute(
                            f"RELEASE SAVEPOINT {savepoint}"
                        )
                    except BaseException as retry_error:
                        primary_error.add_note(
                            "failed to release SQLite savepoint "
                            f"{savepoint}: {cleanup_error}; retry failed: "
                            f"{retry_error}"
                        )

            try:
                yield
            except BaseException as error:
                rollback_savepoint(error)
                raise
            else:
                try:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as error:
                    rollback_savepoint(error)
                    raise
            return

        def rollback_top_level(primary_error: BaseException) -> None:
            self._rollback_connection_or_close(
                primary_error,
                context="the top-level SQLite transaction",
            )

        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            rollback_top_level(error)
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                rollback_top_level(error)
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(
            "SELECT schema_version FROM trace_backed_memory_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise SQLiteSchemaError(
                "SQLite schema metadata must contain exactly one row"
            )
        version = rows[0][0]
        if version != SQLITE_SCHEMA_VERSION:
            raise SQLiteSchemaError(
                "SQLite schema version mismatch: expected "
                f"{SQLITE_SCHEMA_VERSION}, found {version}"
            )

    def _snapshot_record_counts(
        self, cursor: sqlite3.Cursor
    ) -> dict[str, object]:
        counts: dict[str, object] = {}
        for collection, table, _id_field in _COLLECTION_SPECS:
            cursor.execute(f"SELECT count(*) FROM {table}")
            row = cursor.fetchone()
            if row is None or len(row) != 1:
                raise ValueError(
                    f"SQLite {collection} count query must return one row"
                )
            counts[collection] = row[0]
        return counts

    def _snapshot_payload_sizes(
        self, cursor: sqlite3.Cursor
    ) -> dict[str, object]:
        max_record_bytes = 0
        total_bytes = 0
        for collection, table, _id_field in _COLLECTION_SPECS:
            cursor.execute(
                "SELECT COALESCE(MAX(length(CAST(payload AS BLOB))), 0), "
                "COALESCE(SUM(length(CAST(payload AS BLOB))), 0) "
                f"FROM {table}"
            )
            row = cursor.fetchone()
            if row is None or len(row) != 2:
                raise ValueError(
                    f"SQLite {collection} payload query must return one row"
                )
            collection_max, collection_total = row
            if type(collection_max) is not int or type(collection_total) is not int:
                raise ValueError(
                    f"SQLite {collection} payload query must return integers"
                )
            max_record_bytes = max(max_record_bytes, collection_max)
            total_bytes += collection_total
        return {
            "max_record_bytes": max_record_bytes,
            "total_bytes": total_bytes,
        }

    def _load_snapshot(self, cursor: sqlite3.Cursor) -> dict[str, object]:
        _validate_snapshot_record_counts(self._snapshot_record_counts(cursor))
        _validate_snapshot_payload_sizes(self._snapshot_payload_sizes(cursor))
        snapshot: dict[str, object] = {"snapshot_version": 2}
        for collection, table, id_field in _COLLECTION_SPECS:
            cursor.execute(f"SELECT {id_field}, payload FROM {table} ORDER BY {id_field}")
            records: list[dict[str, object]] = []
            for record_id, payload in cursor.fetchall():
                record = _decode_payload(collection, payload)
                if record.get(id_field) != record_id:
                    raise ValueError(
                        f"SQLite {collection} row ID does not match payload"
                    )
                records.append(record)
            snapshot[collection] = records
        return snapshot

    def _ensure_memory_id_available(
        self,
        cursor: sqlite3.Cursor,
        *,
        collection: str,
        record_id: str,
    ) -> None:
        if collection not in _MEMORY_COLLECTIONS:
            return
        for other_collection in _MEMORY_COLLECTIONS.difference({collection}):
            other_table, other_id_field = _SPEC_BY_COLLECTION[other_collection]
            cursor.execute(
                f"SELECT 1 FROM {other_table} WHERE {other_id_field} = ?",
                (record_id,),
            )
            if cursor.fetchone() is not None:
                raise SQLiteConflictError("runtime memory ID already exists")

    def _sync_record(
        self,
        cursor: sqlite3.Cursor,
        *,
        collection: str,
        incoming: dict[str, object],
    ) -> Literal["inserted", "updated", "unchanged"]:
        table, id_field = _SPEC_BY_COLLECTION[collection]
        canonical_incoming = _canonical_record(collection, incoming)
        record_id = canonical_incoming[id_field]
        if not isinstance(record_id, str):
            raise ValueError(f"SQLite {collection}.{id_field} must be a string")

        cursor.execute(
            f"SELECT payload FROM {table} WHERE {id_field} = ?",
            (record_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            self._ensure_memory_id_available(
                cursor,
                collection=collection,
                record_id=record_id,
            )
            cursor.execute(
                f"INSERT INTO {table}({id_field}, payload) VALUES (?, ?)",
                (record_id, _encode_payload(collection, canonical_incoming)),
            )
            return "inserted"
        if len(rows) != 1:
            raise SQLiteConflictError("primary-key selector returned multiple rows")

        stored = _decode_payload(collection, rows[0][0])
        action = _merge_record(collection, stored, canonical_incoming)
        if action == "updated":
            cursor.execute(
                f"UPDATE {table} SET payload = ? WHERE {id_field} = ?",
                (_encode_payload(collection, canonical_incoming), record_id),
            )
            if cursor.rowcount != 1:
                raise SQLiteConflictError("row changed during synchronization")
        return action

    def _cascade_obsolete_lessons(
        self,
        cursor: sqlite3.Cursor,
        *,
        case_id: str,
    ) -> None:
        cursor.execute("SELECT lesson_id, payload FROM lessons ORDER BY lesson_id")
        for lesson_id, payload in cursor.fetchall():
            lesson = _decode_payload("lessons", payload)
            if (
                lesson["source_case_id"] == case_id
                and lesson["status"] == "active"
            ):
                lesson["status"] = "obsolete"
                cursor.execute(
                    "UPDATE lessons SET payload = ? WHERE lesson_id = ?",
                    (_encode_payload("lessons", lesson), lesson_id),
                )
                if cursor.rowcount != 1:
                    raise SQLiteConflictError(
                        "lesson changed during failure-case cascade"
                    )

    def _sync_collection(
        self,
        cursor: sqlite3.Cursor,
        *,
        collection: str,
        records: object,
    ) -> list[Literal["inserted", "updated", "unchanged"]]:
        if not isinstance(records, list):
            raise ValueError(f"snapshot field {collection!r} must be a list")
        _table, id_field = _SPEC_BY_COLLECTION[collection]
        results: list[Literal["inserted", "updated", "unchanged"]] = []
        for record in sorted(records, key=lambda item: item[id_field]):
            if type(record) is not dict:
                raise ValueError(f"snapshot field {collection!r} must contain objects")
            record_id = record.get(id_field)
            try:
                result = self._sync_record(
                    cursor,
                    collection=collection,
                    incoming=record,
                )
                if (
                    collection == "failure_cases"
                    and record["status"] == "obsolete"
                ):
                    self._cascade_obsolete_lessons(
                        cursor,
                        case_id=record_id,
                    )
            except SQLiteConflictError as exc:
                raise SQLiteConflictError(
                    f"failed to sync {_SPEC_BY_COLLECTION[collection][0]} row "
                    f"{record_id}: immutable conflict"
                ) from exc
            except sqlite3.DatabaseError as exc:
                if _is_schema_error(exc):
                    raise SQLiteSchemaError(_MISSING_SCHEMA_MESSAGE) from exc
                raise SQLitePersistenceError(
                    f"failed to sync {_SPEC_BY_COLLECTION[collection][0]} row "
                    f"{record_id}"
                ) from exc
            results.append(result)
        return results

    @_synchronized
    def sync(self, store: TraceBackedMemoryStore) -> SQLiteSyncResult:
        self._require_open()
        snapshot = store.to_snapshot()
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    results = {
                        collection: self._sync_collection(
                            cursor,
                            collection=collection,
                            records=snapshot[collection],
                        )
                        for collection, _table, _id_field in _COLLECTION_SPECS
                    }
                    TraceBackedMemoryStore.from_snapshot(
                        self._load_snapshot(cursor)
                    )
            return SQLiteSyncResult(
                traces=_sync_counts(results["traces"]),
                failure_cases=_sync_counts(results["failure_cases"]),
                lessons=_sync_counts(results["lessons"]),
                project_policies=_sync_counts(results["project_policies"]),
                usage_logs=_sync_counts(results["usage_logs"]),
            )
        except (SQLiteConflictError, SQLiteSchemaError):
            raise
        except sqlite3.DatabaseError as exc:
            if _is_schema_error(exc):
                raise SQLiteSchemaError(_MISSING_SCHEMA_MESSAGE) from exc
            raise SQLitePersistenceError(
                "failed to sync memory store to SQLite"
            ) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise SQLitePersistenceError(
                "failed to sync memory store to SQLite"
            ) from exc

    @_synchronized
    def load(self) -> TraceBackedMemoryStore:
        self._require_open()
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    snapshot = self._load_snapshot(cursor)
                    loaded = TraceBackedMemoryStore.from_snapshot(snapshot)
            return loaded
        except SQLiteSchemaError:
            raise
        except sqlite3.DatabaseError as exc:
            if _is_schema_error(exc):
                raise SQLiteSchemaError(_MISSING_SCHEMA_MESSAGE) from exc
            raise SQLitePersistenceError(
                "failed to load memory store from SQLite"
            ) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise SQLitePersistenceError(
                "failed to load memory store from SQLite"
            ) from exc

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    @_synchronized
    def __enter__(self) -> "SQLiteMemoryRepository":
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
