from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_managed_index_v3 as postgres_managed_index_v3
from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from tests.test_managed_index_v3 import _bundle


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-managed-index.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-managed-index-rollback.sql"


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    installed = cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr


def test_postgres_managed_index_install_invariants_and_rollback(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_managed_index.schema_metadata",
        )
        == "1|tbm.managed-index-bundle.v3"
    )
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.has_schema_privilege("
            "'public', 'trace_backed_memory_v3_managed_index', 'USAGE')",
        )
        == "f"
    )
    blank_scope = postgres_cluster.run(
        "INSERT INTO trace_backed_memory_v3_managed_index."
        "v3_managed_index_bundles ("
        "bundle_id, tenant_id, repository_id, environment_id, "
        "retriever_id, retriever_version, source_catalog_sha256, payload_utf8"
        ") VALUES ("
        "'managed_index_bundle_sha256_"
        + "a"
        * 64
        + "', '   ', 'repository_001', 'environment_001', "
        "'retriever_001', 'v1', 'sha256:" + "a" * 64 + "', '\\x7b7d'::bytea)"
    )
    assert blank_scope.returncode != 0

    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        tbm.PostgresManagedIndexV3Repository(connection).publish(
            _bundle(),
            expected_current_bundle_id=None,
        )

    for statement in (
        "UPDATE trace_backed_memory_v3_managed_index."
        "v3_managed_index_bundles SET retriever_version = 'tampered'",
        "DELETE FROM trace_backed_memory_v3_managed_index.v3_managed_index_bundles",
        "TRUNCATE trace_backed_memory_v3_managed_index.v3_managed_index_bundles",
        "DELETE FROM trace_backed_memory_v3_managed_index.v3_managed_index_heads",
        "UPDATE trace_backed_memory_v3_managed_index."
        "schema_metadata SET schema_version = 2",
    ):
        rejected = postgres_cluster.run(statement)
        assert rejected.returncode != 0
        assert "immutable" in rejected.stderr or "CAS" in rejected.stderr

    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_managed_index') IS NULL",
        )
        == "t"
    )


def test_postgres_managed_index_repository_roundtrip_and_cas(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    first = _bundle()
    second = _bundle(retriever_version="v2")
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresManagedIndexV3Repository(connection)
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

        assert (created.changed, created.head_version) == (True, 1)
        assert (replayed.changed, replayed.head_version) == (False, 1)
        assert (
            advanced.changed,
            advanced.previous_bundle_id,
            advanced.head_version,
        ) == (True, first.bundle_id, 2)
        assert repository.load(first.bundle_id) == first
        assert (
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
            == second
        )

        with pytest.raises(tbm.PostgresManagedIndexV3ConflictError):
            repository.publish(
                first,
                expected_current_bundle_id="managed_index_bundle_sha256_" + "f" * 64,
            )
        assert (
            repository.load_current(
                tenant_id="tenant_001",
                repository_id="repository_001",
                environment_id="environment_001",
            )
            == second
        )


def test_postgres_managed_index_repository_uses_caller_savepoint(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    first = _bundle()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresManagedIndexV3Repository(connection)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_work(value integer)")
                cursor.execute("INSERT INTO caller_work VALUES (1)")
            repository.publish(first, expected_current_bundle_id=None)
            with pytest.raises(tbm.PostgresManagedIndexV3ConflictError):
                repository.publish(
                    _bundle(retriever_version="v2"),
                    expected_current_bundle_id="managed_index_bundle_sha256_"
                    + "f" * 64,
                )
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_work")
                assert cursor.fetchone() == (1,)


def test_postgres_managed_index_rejects_catalog_and_function_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    bundle = _bundle()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresManagedIndexV3Repository(connection)
        repository.publish(bundle, expected_current_bundle_id=None)
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE OR REPLACE FUNCTION "
                "trace_backed_memory_v3_managed_index."
                "reject_immutable_change() RETURNS trigger "
                "LANGUAGE plpgsql SET search_path = pg_catalog AS "
                "$body$ BEGIN RETURN OLD; END $body$"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresManagedIndexV3SchemaError):
            repository.load(bundle.bundle_id)

    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "function security mismatch" in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_managed_index') IS NOT NULL",
        )
        == "t"
    )


def test_postgres_managed_index_concurrent_publish_is_idempotent(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    bundle = _bundle()

    def publish() -> tuple[bool, int]:
        with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
            result = tbm.PostgresManagedIndexV3Repository(connection).publish(
                bundle,
                expected_current_bundle_id=None,
            )
            return result.changed, result.head_version

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _item: publish(), range(2)))

    assert sorted(results) == [(False, 1), (True, 1)]


def test_postgres_managed_index_rollback_fails_closed_on_catalog_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE TABLE trace_backed_memory_v3_managed_index.unexpected (value integer)",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog mismatch" in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regclass("
            "'trace_backed_memory_v3_managed_index.unexpected') IS NOT NULL",
        )
        == "t"
    )


def test_postgres_managed_index_rejects_and_preserves_acl_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    bundle = _bundle()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresManagedIndexV3Repository(connection)
        repository.publish(bundle, expected_current_bundle_id=None)
        with connection.cursor() as cursor:
            cursor.execute(
                "GRANT SELECT ON "
                "trace_backed_memory_v3_managed_index."
                "v3_managed_index_bundles TO PUBLIC"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresManagedIndexV3SchemaError):
            repository.load(bundle.bundle_id)

    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "object ACL mismatch" in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_managed_index') IS NOT NULL",
        )
        == "t"
    )


@pytest.mark.parametrize(
    "drift",
    (
        (
            "ALTER TABLE trace_backed_memory_v3_managed_index."
            "v3_managed_index_bundles "
            "DROP CONSTRAINT managed_index_bundle_payload_check, "
            "ADD CONSTRAINT managed_index_bundle_payload_check "
            "CHECK (octet_length(payload_utf8) >= 1)"
        ),
        (
            "ALTER TABLE trace_backed_memory_v3_managed_index."
            "v3_managed_index_bundles "
            'ALTER COLUMN tenant_id TYPE text COLLATE "default"'
        ),
        (
            "CREATE FUNCTION trace_backed_memory_v3_managed_index."
            "reject_immutable_change(value integer) RETURNS integer "
            "LANGUAGE sql IMMUTABLE AS 'SELECT value'"
        ),
    ),
)
def test_postgres_managed_index_rejects_exact_catalog_drift(
    postgres_cluster: PostgresCluster,
    drift: str,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    bundle = _bundle()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        tbm.PostgresManagedIndexV3Repository(connection).publish(
            bundle,
            expected_current_bundle_id=None,
        )
    changed = postgres_cluster.run(drift)
    assert changed.returncode == 0, changed.stderr

    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        with pytest.raises(tbm.PostgresManagedIndexV3SchemaError):
            tbm.PostgresManagedIndexV3Repository(connection).load(bundle.bundle_id)

    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_managed_index') IS NOT NULL",
        )
        == "t"
    )


def test_postgres_managed_index_resources_are_packaged_byte_exact():
    for name in (
        "postgres-v3-managed-index.sql",
        "postgres-v3-managed-index-rollback.sql",
    ):
        canonical = (ROOT / "schemas" / name).read_bytes()
        assert tbm.read_packaged_resource(f"schemas/{name}") == canonical


def test_postgres_managed_index_rejects_invalid_construction_and_connect_failure(
    monkeypatch,
):
    with pytest.raises(ValueError):
        tbm.PostgresManagedIndexV3Repository(None)

    def fail_load():
        raise RuntimeError("driver unavailable")

    monkeypatch.setattr(postgres_managed_index_v3, "_load_psycopg", fail_load)
    with pytest.raises(tbm.PostgresManagedIndexV3PersistenceError) as caught:
        tbm.PostgresManagedIndexV3Repository.connect()
    assert "driver unavailable" not in str(caught.value)


class _PostgresConnectionStub:
    def __init__(self, *, closed=False):
        self.closed = closed
        self.close_calls = 0

    def close(self):
        self.closed = True
        self.close_calls += 1


def test_postgres_managed_index_closed_context_and_public_input_guards(monkeypatch):
    connection = _PostgresConnectionStub()
    repository = tbm.PostgresManagedIndexV3Repository(
        connection,
        owns_connection=True,
    )
    assert repository.__enter__() is repository
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

    monkeypatch.setattr(
        postgres_managed_index_v3,
        "MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES",
        1,
    )
    with pytest.raises(ValueError):
        repository.publish(_bundle(), expected_current_bundle_id=None)

    repository.__exit__()
    repository.close()
    assert connection.close_calls == 1
    with pytest.raises(tbm.PostgresManagedIndexV3Error):
        repository.load("managed_index_bundle_sha256_" + "a" * 64)

    externally_closed = tbm.PostgresManagedIndexV3Repository(
        _PostgresConnectionStub(closed=True)
    )
    with pytest.raises(tbm.PostgresManagedIndexV3Error):
        externally_closed.__enter__()


def _postgres_bundle_row(bundle, payload=None):
    return {
        "bundle_id": bundle.bundle_id,
        "tenant_id": bundle.tenant_id,
        "repository_id": bundle.repository_id,
        "environment_id": bundle.environment_id,
        "retriever_id": bundle.retriever_id,
        "retriever_version": bundle.retriever_version,
        "source_catalog_sha256": bundle.source_catalog_sha256,
        "payload_utf8": (
            tbm.dumps_managed_index_bundle(bundle).encode("utf-8")
            if payload is None
            else payload
        ),
    }


def test_postgres_managed_index_stored_bundle_rejects_shape_bytes_and_columns():
    bundle = _bundle()
    row = _postgres_bundle_row(bundle)
    assert tbm.PostgresManagedIndexV3Repository._stored_bundle(row) == bundle
    assert (
        tbm.PostgresManagedIndexV3Repository._stored_bundle(
            {**row, "payload_utf8": memoryview(row["payload_utf8"])}
        )
        == bundle
    )
    invalid_rows = (
        object(),
        {key: value for key, value in row.items() if key != "tenant_id"},
        {**row, "payload_utf8": "not-bytes"},
        {**row, "payload_utf8": b"{}"},
        {**row, "tenant_id": "other_tenant"},
    )
    for invalid in invalid_rows:
        with pytest.raises(tbm.PostgresManagedIndexV3PersistenceError):
            tbm.PostgresManagedIndexV3Repository._stored_bundle(invalid)


class _PostgresRowsCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args):
        return None

    def fetchall(self):
        return self.rows


def test_postgres_managed_index_select_bundle_rejects_absent_and_ambiguous_rows():
    bundle_id = "managed_index_bundle_sha256_" + "a" * 64
    with pytest.raises(KeyError):
        tbm.PostgresManagedIndexV3Repository._select_bundle(
            _PostgresRowsCursor([]),
            bundle_id,
        )
    with pytest.raises(tbm.PostgresManagedIndexV3PersistenceError):
        tbm.PostgresManagedIndexV3Repository._select_bundle(
            _PostgresRowsCursor([object(), object()]),
            bundle_id,
        )


class _PostgresError(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    (
        ("42P01", tbm.PostgresManagedIndexV3SchemaError),
        ("23505", tbm.PostgresManagedIndexV3ConflictError),
        (None, tbm.PostgresManagedIndexV3PersistenceError),
    ),
)
def test_postgres_managed_index_maps_database_errors(sqlstate, expected):
    repository = tbm.PostgresManagedIndexV3Repository(_PostgresConnectionStub())
    with pytest.raises(expected):
        repository._raise_postgres(_PostgresError(sqlstate))


class _FailingTransaction:
    def __enter__(self):
        raise RuntimeError("private transaction failure")

    def __exit__(self, *_args):
        return None


class _FailingPostgresConnection(_PostgresConnectionStub):
    def transaction(self):
        return _FailingTransaction()


def test_postgres_managed_index_sanitizes_transaction_failures():
    repository = tbm.PostgresManagedIndexV3Repository(_FailingPostgresConnection())
    with pytest.raises(tbm.PostgresManagedIndexV3PersistenceError) as caught:
        repository.publish(_bundle(), expected_current_bundle_id=None)
    assert "private" not in str(caught.value)

    with pytest.raises(tbm.PostgresManagedIndexV3PersistenceError) as caught:
        repository.load(_bundle().bundle_id)
    assert "private" not in str(caught.value)

    with pytest.raises(tbm.PostgresManagedIndexV3PersistenceError) as caught:
        repository.load_current(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        )
    assert "private" not in str(caught.value)
