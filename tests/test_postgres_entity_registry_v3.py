from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

import trace_backed_memory as tbm
from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from trace_backed_memory.postgres_entity_registry_v3 import (
    PostgresEntityRegistryV3ConflictError,
    PostgresEntityRegistryV3Repository,
    PostgresEntityRegistryV3SchemaError,
)
from trace_backed_memory.postgres import _load_psycopg


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-entity-registry.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-entity-registry-rollback.sql"


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    result = postgres_cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr


def _registry() -> tbm.EntityRegistrySnapshot:
    return tbm.loads_entity_registry(
        (ROOT / "examples" / "entity_registry_v3.example.json").read_bytes()
    )


def _repository(
    postgres_cluster: PostgresCluster,
) -> PostgresEntityRegistryV3Repository:
    return PostgresEntityRegistryV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )


def test_postgres_entity_registry_public_exports_are_intentional() -> None:
    assert (
        tbm.PostgresEntityRegistryV3Repository
        is PostgresEntityRegistryV3Repository
    )
    assert tbm.POSTGRES_ENTITY_REGISTRY_V3_SCHEMA_VERSION == 1
    assert "PostgresEntityRegistryV3Repository" in tbm.__all__
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-entity-registry.sql"
    ) == INSTALL.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-entity-registry-rollback.sql"
    ) == ROLLBACK.read_bytes()


def test_postgres_entity_registry_install_is_version_gated_and_immutable(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT schema_version || ':' || contract_version "
            "FROM trace_backed_memory_v3_entity_registry.schema_metadata",
        )
        == "1:tbm.entity-registry.v3"
    )
    mutation = postgres_cluster.run(
        "UPDATE trace_backed_memory_v3_entity_registry.schema_metadata "
        "SET schema_version = 1"
    )
    assert mutation.returncode != 0
    assert "records are immutable" in mutation.stderr


def test_postgres_entity_registry_round_trip_and_conflict(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    registry = _registry()
    repository = _repository(postgres_cluster)
    try:
        assert repository.put(registry) is True
        assert repository.put(registry) is False
        assert repository.get(registry.registry_sha256) == registry
        assert repository.get_by_version("registry_001") == registry
        assert repository.list_versions() == ("registry_001",)

        changed_environment = replace(
            registry.environments[0],
            display_name="Changed",
        )
        changed = replace(
            registry,
            environments=(changed_environment,),
        )
        with pytest.raises(PostgresEntityRegistryV3ConflictError):
            repository.put(changed)
    finally:
        repository.close()


def test_postgres_entity_registry_catalog_acl_drift_fails_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "GRANT SELECT ON "
        "trace_backed_memory_v3_entity_registry.v3_entity_registry_snapshots "
        "TO PUBLIC",
    )
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(PostgresEntityRegistryV3SchemaError):
            repository.list_versions()
    finally:
        repository.close()


def test_postgres_entity_registry_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    )
    repository = PostgresEntityRegistryV3Repository(connection)
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_work (value text)")
                cursor.execute("INSERT INTO caller_work VALUES ('before')")
            assert repository.put(_registry()) is True
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_work")
                assert cursor.fetchall() == [{"value": "before"}]
        assert repository.get_by_version("registry_001") == _registry()
    finally:
        repository.close()
        connection.close()


def test_postgres_entity_registry_detects_extra_normalized_row(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    registry = _registry()
    repository = _repository(postgres_cluster)
    try:
        assert repository.put(registry) is True
        assert_sql_succeeds(
            postgres_cluster,
            "INSERT INTO trace_backed_memory_v3_entity_registry."
            "v3_entity_registry_organizations "
            "(registry_sha256, organization_id, display_name, status) "
            f"VALUES ('{registry.registry_sha256}', "
            "'organization_extra', 'Extra', 'disabled')",
        )
        with pytest.raises(
            tbm.PostgresEntityRegistryV3PersistenceError,
            match="do not match descriptor",
        ):
            repository.get(registry.registry_sha256)
    finally:
        repository.close()


def test_postgres_entity_registry_concurrent_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    registry = _registry()

    def put_once() -> bool:
        repository = _repository(postgres_cluster)
        try:
            return repository.put(registry)
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: put_once(), range(2)))

    assert sorted(results) == [False, True]


def test_postgres_entity_registry_rollback_is_exact_and_fail_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER INDEX "
        "trace_backed_memory_v3_entity_registry."
        "v3_entity_registry_snapshots_policy "
        "RENAME TO unexpected_snapshots_policy",
    )

    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog fingerprint mismatch" in rejected.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER INDEX "
        "trace_backed_memory_v3_entity_registry.unexpected_snapshots_policy "
        "RENAME TO v3_entity_registry_snapshots_policy",
    )
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE VIEW public.external_registry_dependency AS "
        "SELECT registry_version FROM "
        "trace_backed_memory_v3_entity_registry."
        "v3_entity_registry_snapshots",
    )
    dependency_rejected = postgres_cluster.run_script(ROLLBACK)
    assert dependency_rejected.returncode != 0
    assert "external_registry_dependency" in dependency_rejected.stderr
    assert "CASCADE" in dependency_rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT to_regclass("
            "'public.external_registry_dependency') IS NOT NULL",
        )
        == "t"
    )
    assert_sql_succeeds(
        postgres_cluster,
        "DROP VIEW public.external_registry_dependency",
    )

    removed = postgres_cluster.run_script(ROLLBACK)
    assert removed.returncode == 0, removed.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT to_regnamespace("
            "'trace_backed_memory_v3_entity_registry') IS NULL",
        )
        == "t"
    )


def test_postgres_entity_registry_rejects_unfingerprinted_policy(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE POLICY unexpected_policy ON "
        "trace_backed_memory_v3_entity_registry."
        "v3_entity_registry_snapshots USING (true)",
    )
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(
            PostgresEntityRegistryV3SchemaError,
            match="unsupported policies, rules, or relation kinds",
        ):
            repository.list_versions()
    finally:
        repository.close()

    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "unsupported catalog object" in rejected.stderr


def test_postgres_entity_registry_rejects_unfingerprinted_relation_kind(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE VIEW trace_backed_memory_v3_entity_registry."
        "unexpected_view AS SELECT registry_version FROM "
        "trace_backed_memory_v3_entity_registry."
        "v3_entity_registry_snapshots",
    )
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(
            PostgresEntityRegistryV3SchemaError,
            match="unsupported policies, rules, or relation kinds",
        ):
            repository.list_versions()
    finally:
        repository.close()
