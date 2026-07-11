from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import TraceBackedMemoryStore


POSTGRES_SCHEMA_VERSION = 1


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

    def load(self) -> TraceBackedMemoryStore:
        self._require_open()
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.cursor(row_factory=dict_row) as cursor:
                self._lock_schema(cursor, write=False)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42P01":
                self._connection.rollback()
                raise PostgresSchemaError(
                    "PostgreSQL schema metadata is missing"
                ) from None
            raise
        return TraceBackedMemoryStore()

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
