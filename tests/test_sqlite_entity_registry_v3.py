from __future__ import annotations

import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import trace_backed_memory as tbm


ROOT = Path(__file__).resolve().parents[1]


def _registry() -> tbm.EntityRegistrySnapshot:
    return tbm.loads_entity_registry(
        (
            ROOT / "examples" / "entity_registry_v3.example.json"
        ).read_bytes()
    )


def _changed_registry(
    *,
    version: str = "registry_001",
) -> tbm.EntityRegistrySnapshot:
    original = _registry()
    environment = original.environments[0]
    return tbm.EntityRegistrySnapshot(
        registry_version=version,
        organizations=original.organizations,
        tenants=original.tenants,
        environments=(
            tbm.EnvironmentIdentity(
                environment_id=environment.environment_id,
                tenant_id=environment.tenant_id,
                repository_id=environment.repository_id,
                environment_kind=environment.environment_kind,
                display_name="Changed production",
                status=environment.status,
                attributes=environment.attributes,
            ),
        ),
        authorization_policy=original.authorization_policy,
    )


def test_sqlite_entity_registry_round_trip_and_normalized_rows() -> None:
    registry = _registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True
        assert repository.put(registry) is False
        assert repository.get(registry.registry_sha256) == registry
        assert repository.get_by_version(registry.registry_version) == registry
        assert repository.list_versions() == ("registry_001",)

        connection = repository._connection
        expected_counts = {
            "v3_entity_registry_organizations": 1,
            "v3_entity_registry_tenants": 1,
            "v3_entity_registry_repositories": 1,
            "v3_entity_registry_repository_tenants": 1,
            "v3_entity_registry_repository_legacy_aliases": 1,
            "v3_entity_registry_repository_aliases": 1,
            "v3_entity_registry_principals": 1,
            "v3_entity_registry_agent_clients": 1,
            "v3_entity_registry_environments": 1,
            "v3_entity_registry_environment_attributes": 1,
            "v3_entity_registry_role_bindings": 1,
            "v3_entity_registry_binding_permissions": 1,
            "v3_entity_registry_scope_attributes": 1,
        }
        for table, expected in expected_counts.items():
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone() == (expected,)
    finally:
        repository.close()


def test_sqlite_entity_registry_same_version_conflict_is_atomic() -> None:
    registry = _registry()
    changed = _changed_registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True

        with pytest.raises(tbm.SQLiteEntityRegistryV3ConflictError):
            repository.put(changed)

        assert repository.get_by_version("registry_001") == registry
        assert repository.list_versions() == ("registry_001",)
    finally:
        repository.close()


def test_sqlite_entity_registry_distinct_versions_are_isolated() -> None:
    first = _registry()
    second = _changed_registry(version="registry_002")
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(first) is True
        assert repository.put(second) is True

        assert repository.list_versions() == (
            "registry_001",
            "registry_002",
        )
        assert repository.get(first.registry_sha256) == first
        assert repository.get(second.registry_sha256) == second
    finally:
        repository.close()


def test_sqlite_entity_registry_preserves_caller_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-entity-registry.sql"
        ).decode()
    )
    connection.execute("CREATE TABLE caller_state (value TEXT)")
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES ('before')")
    repository = tbm.SQLiteEntityRegistryV3Repository(connection)

    assert repository.put(_registry()) is True
    assert connection.in_transaction
    assert connection.execute("SELECT value FROM caller_state").fetchall() == [
        ("before",)
    ]

    connection.rollback()
    assert repository.list_versions() == ()
    repository.close()
    connection.close()


def test_sqlite_entity_registry_detects_extra_normalized_row() -> None:
    registry = _registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True
        repository._connection.execute(
            "INSERT INTO v3_entity_registry_organizations "
            "(registry_sha256, organization_id, display_name, status) "
            "VALUES (?, 'organization_extra', 'Extra', 'disabled')",
            (registry.registry_sha256,),
        )
        repository._connection.commit()

        with pytest.raises(
            tbm.SQLiteEntityRegistryV3PersistenceError,
            match="do not match descriptor",
        ):
            repository.get(registry.registry_sha256)
    finally:
        repository.close()


def test_sqlite_entity_registry_bounds_corrupt_normalized_loads() -> None:
    registry = _registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True
        repository._connection.executemany(
            "INSERT INTO v3_entity_registry_organizations "
            "(registry_sha256, organization_id, display_name, status) "
            "VALUES (?, ?, 'Extra', 'disabled')",
            (
                (registry.registry_sha256, f"organization_{index:03d}")
                for index in range(2, 102)
            ),
        )
        repository._connection.commit()
        seen_sql: list[str] = []
        repository._connection.set_trace_callback(seen_sql.append)

        with pytest.raises(tbm.SQLiteEntityRegistryV3PersistenceError):
            repository.get(registry.registry_sha256)

        organization_selects = [
            sql
            for sql in seen_sql
            if "FROM v3_entity_registry_organizations" in sql
            and sql.lstrip().upper().startswith("SELECT")
        ]
        assert len(organization_selects) == 1
        assert "LIMIT 2" in organization_selects[0]
    finally:
        repository._connection.set_trace_callback(None)
        repository.close()


def test_sqlite_entity_registry_normalized_rows_are_immutable() -> None:
    registry = _registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repository._connection.execute(
                "UPDATE v3_entity_registry_tenants "
                "SET display_name = 'Changed'"
            )
        repository._connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repository._connection.execute(
                "DELETE FROM v3_entity_registry_snapshots"
            )
    finally:
        repository.close()


def test_sqlite_entity_registry_database_blocks_cross_tenant_environment() -> None:
    registry = _registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True
        connection = repository._connection
        connection.execute(
            "INSERT INTO v3_entity_registry_tenants "
            "(registry_sha256, tenant_id, organization_id, display_name, "
            "status) VALUES (?, 'tenant_002', 'organization_001', "
            "'Other', 'disabled')",
            (registry.registry_sha256,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO v3_entity_registry_environments "
                "(registry_sha256, environment_id, tenant_id, repository_id, "
                "environment_kind, display_name, status) "
                "VALUES (?, 'environment_bad', 'tenant_002', "
                "'repository_001', 'test', 'Bad', 'disabled')",
                (registry.registry_sha256,),
            )
    finally:
        repository._connection.rollback()
        repository.close()


def test_sqlite_entity_registry_database_enforces_domain_enums() -> None:
    registry = _registry()
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        assert repository.put(registry) is True
        connection = repository._connection
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "INSERT INTO v3_entity_registry_binding_permissions "
                "(registry_sha256, binding_id, permission) "
                "VALUES (?, 'binding_001', 'memory:invent')",
                (registry.registry_sha256,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "INSERT INTO v3_entity_registry_agent_clients "
                "(registry_sha256, agent_client_id, tenant_id, client_kind, "
                "status) VALUES (?, 'client_bad', 'tenant_001', "
                "'browser_extension', 'disabled')",
                (registry.registry_sha256,),
            )
    finally:
        repository._connection.rollback()
        repository.close()


def test_sqlite_entity_registry_detects_schema_drift_and_pragmas() -> None:
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        repository._connection.execute(
            "DROP INDEX v3_entity_registry_snapshots_policy"
        )
        with pytest.raises(tbm.SQLiteEntityRegistryV3SchemaError):
            repository.list_versions()
    finally:
        repository.close()

    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        repository._connection.execute(
            "CREATE TRIGGER unexpected_registry_trigger "
            "AFTER INSERT ON v3_entity_registry_snapshots BEGIN SELECT 1; END"
        )
        with pytest.raises(tbm.SQLiteEntityRegistryV3SchemaError):
            repository.list_versions()
    finally:
        repository.close()

    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        repository._connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(
            tbm.SQLiteEntityRegistryV3SchemaError,
            match="requires foreign keys",
        ):
            repository.list_versions()
    finally:
        repository.close()


def test_sqlite_entity_registry_missing_and_validation_errors() -> None:
    repository = tbm.SQLiteEntityRegistryV3Repository.connect(
        initialize=True
    )
    try:
        with pytest.raises(KeyError):
            repository.get("sha256:" + "a" * 64)
        with pytest.raises(KeyError):
            repository.get_by_version("missing")
        with pytest.raises(ValueError):
            repository.get("not-a-digest")
        with pytest.raises(ValueError):
            repository.list_versions(limit=0)
        with pytest.raises(ValueError):
            repository.put(object())
    finally:
        repository.close()


def test_sqlite_entity_registry_resource_is_exact() -> None:
    assert tbm.read_packaged_resource(
        "schemas/sqlite-v3-entity-registry.sql"
    ) == (
        ROOT / "schemas" / "sqlite-v3-entity-registry.sql"
    ).read_bytes()


def test_sqlite_entity_registry_concurrent_exact_replay(tmp_path: Path) -> None:
    database = tmp_path / "entity-registry.sqlite3"
    registry = _registry()
    tbm.SQLiteEntityRegistryV3Repository.connect(
        database,
        initialize=True,
    ).close()

    def put_once() -> bool:
        repository = tbm.SQLiteEntityRegistryV3Repository.connect(
            database,
            timeout=10,
        )
        try:
            return repository.put(registry)
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: put_once(), range(2)))

    assert sorted(results) == [False, True]
