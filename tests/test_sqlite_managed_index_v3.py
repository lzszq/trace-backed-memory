from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_managed_index_v3 as sqlite_managed_index_v3
from tests.test_managed_index_v3 import _bundle


def test_sqlite_managed_index_publishes_replays_and_advances_head():
    first = _bundle()
    second = _bundle(retriever_version="v2")
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        created = repository.publish(
            first,
            expected_current_bundle_id=None,
        )
        replayed = repository.publish(
            first,
            expected_current_bundle_id=None,
        )
        advanced = repository.publish(
            second,
            expected_current_bundle_id=first.bundle_id,
        )

        assert (created.changed, created.previous_bundle_id) == (True, None)
        assert (replayed.changed, replayed.head_version) == (False, 1)
        assert (
            advanced.changed,
            advanced.previous_bundle_id,
            advanced.head_version,
        ) == (True, first.bundle_id, 2)
        assert repository.load(first.bundle_id) == first
        assert repository.load(second.bundle_id) == second
        assert (
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
            == second
        )


def test_sqlite_managed_index_conflict_is_atomic():
    first = _bundle()
    second = _bundle(retriever_version="v2")
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(first, expected_current_bundle_id=None)
        with pytest.raises(tbm.SQLiteManagedIndexV3ConflictError):
            repository.publish(
                second,
                expected_current_bundle_id="managed_index_bundle_sha256_" + "f" * 64,
            )
        with pytest.raises(KeyError):
            repository.load(second.bundle_id)
        assert (
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
            == first
        )


def test_sqlite_managed_index_uses_savepoints_for_caller_transactions():
    first = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        tbm.read_packaged_resource("schemas/sqlite-v3-managed-index.sql").decode(
            "utf-8"
        )
    )
    repository = tbm.SQLiteManagedIndexV3Repository(connection)
    try:
        connection.execute("CREATE TABLE caller_state(value TEXT NOT NULL)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_state VALUES ('kept')")
        repository.publish(first, expected_current_bundle_id=None)
        with pytest.raises(tbm.SQLiteManagedIndexV3ConflictError):
            repository.publish(
                _bundle(retriever_version="v2"),
                expected_current_bundle_id="managed_index_bundle_sha256_" + "f" * 64,
            )
        assert connection.execute("SELECT value FROM caller_state").fetchall() == [
            ("kept",)
        ]
        connection.rollback()
        with pytest.raises(KeyError):
            repository.load(first.bundle_id)
    finally:
        repository.close()
        connection.close()


def test_sqlite_managed_index_relations_are_immutable():
    bundle = _bundle()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        repository.publish(bundle, expected_current_bundle_id=None)
        connection = repository._connection
        for statement in (
            "DELETE FROM v3_managed_index_bundles",
            "UPDATE v3_managed_index_bundles SET retriever_version = 'tampered'",
            "DELETE FROM v3_managed_index_heads",
            "UPDATE v3_managed_index_heads SET head_version = head_version + 2",
            "DELETE FROM trace_backed_memory_v3_managed_index_schema",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        (
            "bundle_id",
            "managed_index_bundle_sha256_" + "a" * 63 + "Z",
        ),
        ("source_catalog_sha256", "sha256:" + "a" * 63 + "Z"),
    ),
)
def test_sqlite_managed_index_rejects_non_hex_hashes(column, value):
    bundle = _bundle()
    payload = tbm.dumps_managed_index_bundle(bundle).encode("utf-8")
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        values = {
            "bundle_id": bundle.bundle_id,
            "tenant_id": bundle.tenant_id,
            "repository_id": bundle.repository_id,
            "environment_id": bundle.environment_id,
            "retriever_id": bundle.retriever_id,
            "retriever_version": bundle.retriever_version,
            "source_catalog_sha256": bundle.source_catalog_sha256,
            "payload_utf8": payload,
        }
        values[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            repository._connection.execute(
                "INSERT INTO v3_managed_index_bundles ("
                "bundle_id, tenant_id, repository_id, environment_id, "
                "retriever_id, retriever_version, "
                "source_catalog_sha256, payload_utf8"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(values[name] for name in values),
            )


def test_sqlite_managed_index_rejects_blank_scope_identifiers():
    bundle = _bundle()
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        with pytest.raises(sqlite3.IntegrityError):
            repository._connection.execute(
                "INSERT INTO v3_managed_index_bundles ("
                "bundle_id, tenant_id, repository_id, environment_id, "
                "retriever_id, retriever_version, "
                "source_catalog_sha256, payload_utf8"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bundle.bundle_id,
                    "   ",
                    bundle.repository_id,
                    bundle.environment_id,
                    bundle.retriever_id,
                    bundle.retriever_version,
                    bundle.source_catalog_sha256,
                    tbm.dumps_managed_index_bundle(bundle).encode("utf-8"),
                ),
            )


@pytest.mark.parametrize(
    "drift",
    (
        "DROP INDEX v3_managed_index_bundles_scope",
        "DROP TRIGGER v3_managed_index_heads_cas",
        "ALTER TABLE v3_managed_index_bundles ADD COLUMN unexpected TEXT",
    ),
)
def test_sqlite_managed_index_fails_closed_on_schema_drift(drift):
    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    try:
        repository._connection.execute(drift)
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            repository.publish(
                _bundle(),
                expected_current_bundle_id=None,
            )
    finally:
        repository.close()


def test_sqlite_managed_index_rejects_arbitrarily_named_attached_trigger():
    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    try:
        repository._connection.execute(
            "CREATE TRIGGER rogue_trigger "
            "BEFORE INSERT ON v3_managed_index_bundles "
            "BEGIN SELECT RAISE(ABORT, 'rogue'); END"
        )
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            repository.publish(
                _bundle(),
                expected_current_bundle_id=None,
            )
    finally:
        repository.close()


def test_sqlite_managed_index_rejects_temp_trigger_on_managed_table():
    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    try:
        repository._connection.execute(
            "CREATE TEMP TRIGGER rogue_temp "
            "BEFORE INSERT ON v3_managed_index_bundles "
            "BEGIN SELECT RAISE(ABORT, 'rogue'); END"
        )
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            repository.publish(
                _bundle(),
                expected_current_bundle_id=None,
            )
    finally:
        repository.close()


def test_sqlite_managed_index_requires_integrity_pragmas():
    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    try:
        repository._connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
    finally:
        repository.close()


def test_sqlite_managed_index_serializes_concurrent_first_publish(
    tmp_path: Path,
):
    database = tmp_path / "managed-index.sqlite3"
    tbm.SQLiteManagedIndexV3Repository.connect(
        database,
        initialize=True,
    ).close()
    bundle = _bundle()

    def publish() -> tuple[bool, int]:
        with tbm.SQLiteManagedIndexV3Repository.connect(
            database,
            timeout=10,
        ) as repository:
            result = repository.publish(
                bundle,
                expected_current_bundle_id=None,
            )
            return result.changed, result.head_version

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _item: publish(), range(2)))

    assert sorted(results) == [(False, 1), (True, 1)]


def test_sqlite_managed_index_schema_is_packaged_byte_exact():
    canonical = (
        Path(__file__).parents[1] / "schemas" / "sqlite-v3-managed-index.sql"
    ).read_bytes()
    assert (
        tbm.read_packaged_resource("schemas/sqlite-v3-managed-index.sql") == canonical
    )


def test_sqlite_managed_index_rejects_invalid_construction_and_connection_inputs():
    with pytest.raises(ValueError):
        tbm.SQLiteManagedIndexV3Repository(object())
    with pytest.raises(ValueError):
        tbm.SQLiteManagedIndexV3Repository.connect(initialize=1)
    with pytest.raises(tbm.SQLiteManagedIndexV3PersistenceError):
        tbm.SQLiteManagedIndexV3Repository.connect(object())


def test_sqlite_managed_index_closed_and_missing_schema_fail_closed():
    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    repository.close()
    repository.close()
    with pytest.raises(tbm.SQLiteManagedIndexV3Error):
        repository.load("managed_index_bundle_sha256_" + "a" * 64)
    with pytest.raises(tbm.SQLiteManagedIndexV3Error):
        repository.__enter__()

    connection = sqlite3.connect(":memory:")
    externally_closed = tbm.SQLiteManagedIndexV3Repository(connection)
    connection.close()
    with pytest.raises(tbm.SQLiteManagedIndexV3Error):
        externally_closed.load("managed_index_bundle_sha256_" + "a" * 64)

    with tbm.SQLiteManagedIndexV3Repository.connect() as missing:
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            missing.load("managed_index_bundle_sha256_" + "a" * 64)


def test_sqlite_managed_index_validates_public_inputs_and_absent_heads(monkeypatch):
    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    try:
        with pytest.raises(ValueError):
            repository.publish(object(), expected_current_bundle_id=None)
        with pytest.raises(ValueError):
            repository.load("not-a-bundle-id")
        with pytest.raises(ValueError):
            repository.load_current(
                tenant_id=" ",
                repository_id="repository_001",
                environment_id="environment_001",
            )
        with pytest.raises(KeyError):
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )

        monkeypatch.setattr(
            sqlite_managed_index_v3,
            "MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES",
            1,
        )
        with pytest.raises(ValueError):
            repository.publish(_bundle(), expected_current_bundle_id=None)
    finally:
        repository.close()


def test_sqlite_managed_index_stored_bundle_rejects_shape_bytes_and_columns():
    bundle = _bundle()
    payload = tbm.dumps_managed_index_bundle(bundle).encode("utf-8")
    row = (
        bundle.bundle_id,
        bundle.tenant_id,
        bundle.repository_id,
        bundle.environment_id,
        bundle.retriever_id,
        bundle.retriever_version,
        bundle.source_catalog_sha256,
        payload,
    )
    assert tbm.SQLiteManagedIndexV3Repository._stored_bundle(row) == bundle
    for invalid in (
        object(),
        row[:-1],
        (*row[:-1], "not-bytes"),
        (*row[:-1], b"{}"),
        (row[0], "other_tenant", *row[2:]),
    ):
        with pytest.raises(tbm.SQLiteManagedIndexV3PersistenceError):
            tbm.SQLiteManagedIndexV3Repository._stored_bundle(invalid)


class _SQLiteRowsCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args):
        return None

    def fetchall(self):
        return self.rows


def test_sqlite_managed_index_select_and_schema_row_shape_fail_closed():
    bundle_id = "managed_index_bundle_sha256_" + "a" * 64
    with pytest.raises(KeyError):
        tbm.SQLiteManagedIndexV3Repository._select_bundle(
            _SQLiteRowsCursor([]),
            bundle_id,
        )
    with pytest.raises(tbm.SQLiteManagedIndexV3PersistenceError):
        tbm.SQLiteManagedIndexV3Repository._select_bundle(
            _SQLiteRowsCursor([object(), object()]),
            bundle_id,
        )
    with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
        sqlite_managed_index_v3._read_schema_definitions(
            _SQLiteRowsCursor([("table", "name", "table", None)])
        )
    with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
        sqlite_managed_index_v3._normalized_schema_sql(None)


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (
            sqlite3.OperationalError("no such table: managed"),
            tbm.SQLiteManagedIndexV3SchemaError,
        ),
        (
            sqlite3.IntegrityError("UNIQUE constraint failed"),
            tbm.SQLiteManagedIndexV3ConflictError,
        ),
        (
            sqlite3.OperationalError("disk I/O error"),
            tbm.SQLiteManagedIndexV3PersistenceError,
        ),
    ),
)
def test_sqlite_managed_index_maps_database_errors(error, expected):
    with tbm.SQLiteManagedIndexV3Repository.connect(initialize=True) as repository:
        with pytest.raises(expected):
            repository._raise_sqlite(error)


def test_sqlite_managed_index_rejects_metadata_and_recursive_trigger_drift():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE trace_backed_memory_v3_managed_index_schema ("
        "singleton INTEGER, schema_version INTEGER, contract_version TEXT)"
    )
    connection.execute(
        "INSERT INTO trace_backed_memory_v3_managed_index_schema VALUES (1, 99, 'bad')"
    )
    repository = tbm.SQLiteManagedIndexV3Repository(connection)
    try:
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            repository.load("managed_index_bundle_sha256_" + "a" * 64)
    finally:
        repository.close()
        connection.close()

    repository = tbm.SQLiteManagedIndexV3Repository.connect(initialize=True)
    try:
        repository._connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(tbm.SQLiteManagedIndexV3SchemaError):
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
    finally:
        repository.close()


class _RollbackFailureConnection:
    in_transaction = True

    def rollback(self):
        raise RuntimeError("rollback failed")

    def close(self):
        raise RuntimeError("close failed")


def test_sqlite_managed_index_marks_connection_closed_after_rollback_failure():
    repository = object.__new__(tbm.SQLiteManagedIndexV3Repository)
    repository._connection = _RollbackFailureConnection()
    repository._closed = False
    primary = RuntimeError("primary")

    repository._rollback_or_close(primary, context="test transaction")

    assert repository._closed is True
    notes = getattr(primary, "__notes__", ())
    assert len(notes) == 3
