from __future__ import annotations

import sys
from dataclasses import is_dataclass

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


def test_package_import_does_not_load_psycopg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delitem(sys.modules, "psycopg", raising=False)
    monkeypatch.delitem(sys.modules, "trace_backed_memory", raising=False)

    __import__("trace_backed_memory")

    assert "psycopg" not in sys.modules


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
