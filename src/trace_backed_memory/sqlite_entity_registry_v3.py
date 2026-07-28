from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from functools import lru_cache, wraps
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .entity_registry_v3 import (
    ENTITY_REGISTRY_CONTRACT_VERSION,
    ENTITY_REGISTRY_JSON_MAX_BYTES,
    EntityRegistryContractError,
    EntityRegistrySnapshot,
    dumps_entity_registry,
    loads_entity_registry,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_ENTITY_REGISTRY_V3_SCHEMA_VERSION = 1
SQLITE_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE = 1000
_MISSING_SCHEMA_MESSAGE = (
    "SQLite entity registry v3 schema is missing or incomplete"
)
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_SCHEMA_OBJECTS = 128
_P = ParamSpec("_P")
_R = TypeVar("_R")

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "v3_entity_registry_organizations": (
        "registry_sha256",
        "organization_id",
        "display_name",
        "status",
    ),
    "v3_entity_registry_tenants": (
        "registry_sha256",
        "tenant_id",
        "organization_id",
        "display_name",
        "status",
    ),
    "v3_entity_registry_repositories": (
        "registry_sha256",
        "repository_id",
        "provider",
        "provider_repository_id",
        "canonical_locator_hash",
        "display_name",
    ),
    "v3_entity_registry_repository_tenants": (
        "registry_sha256",
        "repository_id",
        "tenant_id",
    ),
    "v3_entity_registry_repository_legacy_aliases": (
        "registry_sha256",
        "repository_id",
        "alias",
    ),
    "v3_entity_registry_repository_aliases": (
        "registry_sha256",
        "tenant_id",
        "alias",
        "repository_id",
        "source",
    ),
    "v3_entity_registry_principals": (
        "registry_sha256",
        "principal_id",
        "issuer",
        "subject_hash",
        "tenant_id",
        "status",
    ),
    "v3_entity_registry_agent_clients": (
        "registry_sha256",
        "agent_client_id",
        "tenant_id",
        "client_kind",
        "status",
    ),
    "v3_entity_registry_environments": (
        "registry_sha256",
        "environment_id",
        "tenant_id",
        "repository_id",
        "environment_kind",
        "display_name",
        "status",
    ),
    "v3_entity_registry_environment_attributes": (
        "registry_sha256",
        "environment_id",
        "attribute_name",
        "attribute_value",
    ),
    "v3_entity_registry_role_bindings": (
        "registry_sha256",
        "binding_id",
        "principal_id",
        "agent_client_id",
        "role_name",
        "scope_kind",
        "tenant_id",
        "repository_id",
        "status",
        "valid_from",
        "expires_at",
    ),
    "v3_entity_registry_binding_permissions": (
        "registry_sha256",
        "binding_id",
        "permission",
    ),
    "v3_entity_registry_scope_attributes": (
        "registry_sha256",
        "binding_id",
        "attribute_name",
        "attribute_value",
    ),
}


class SQLiteEntityRegistryV3Error(RuntimeError):
    pass


class SQLiteEntityRegistryV3SchemaError(SQLiteEntityRegistryV3Error):
    pass


class SQLiteEntityRegistryV3ConflictError(SQLiteEntityRegistryV3Error):
    pass


class SQLiteEntityRegistryV3PersistenceError(SQLiteEntityRegistryV3Error):
    pass


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteEntityRegistryV3SchemaError(
            "SQLite entity registry v3 schema has an invalid definition"
        )
    return "".join(value.split()).casefold()


def _read_schema_definitions(
    cursor: sqlite3.Cursor,
) -> tuple[tuple[str, str, str, str], ...]:
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%' "
        "AND (name = 'trace_backed_memory_v3_entity_registry_schema' "
        "OR name LIKE 'v3_entity_registry_%' "
        "OR tbl_name = 'trace_backed_memory_v3_entity_registry_schema' "
        "OR tbl_name LIKE 'v3_entity_registry_%') "
        "ORDER BY type, name LIMIT ?",
        (_MAX_SCHEMA_OBJECTS + 1,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise SQLiteEntityRegistryV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteEntityRegistryV3SchemaError(
                "SQLite entity registry v3 schema definition has invalid shape"
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    return tuple(definitions)


@lru_cache(maxsize=1)
def _canonical_schema_definitions() -> tuple[tuple[str, str, str, str], ...]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                read_packaged_resource(
                    "schemas/sqlite-v3-entity-registry.sql"
                ).decode("utf-8")
            )
            with closing(connection.cursor()) as cursor:
                return _read_schema_definitions(cursor)
        finally:
            connection.close()
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        raise SQLiteEntityRegistryV3SchemaError(
            "could not validate canonical SQLite entity registry v3 schema"
        ) from error


class SQLiteEntityRegistryV3Repository:
    """Immutable normalized local authority for entity-registry snapshots."""

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
    ) -> SQLiteEntityRegistryV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        "schemas/sqlite-v3-entity-registry.sql"
                    ).decode("utf-8")
                )
        except (
            OSError,
            UnicodeError,
            sqlite3.Error,
            PackagedResourceError,
            TypeError,
            ValueError,
        ) as error:
            if "connection" in locals():
                connection.close()
            raise SQLiteEntityRegistryV3PersistenceError(
                "failed to connect to SQLite entity registry v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteEntityRegistryV3Error(
                "SQLite entity registry v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteEntityRegistryV3Error(
                "SQLite entity registry v3 repository is closed"
            ) from error

    def _lock_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_entity_registry_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [
            (
                SQLITE_ENTITY_REGISTRY_V3_SCHEMA_VERSION,
                ENTITY_REGISTRY_CONTRACT_VERSION,
            )
        ]:
            raise SQLiteEntityRegistryV3SchemaError(
                "SQLite entity registry v3 schema metadata mismatch"
            )
        if _read_schema_definitions(cursor) != _canonical_schema_definitions():
            raise SQLiteEntityRegistryV3SchemaError(
                "SQLite entity registry v3 schema definitions do not match"
            )
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteEntityRegistryV3SchemaError(
                "SQLite entity registry v3 requires foreign keys"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteEntityRegistryV3SchemaError(
                "SQLite entity registry v3 requires recursive triggers"
            )

    def _rollback_or_close(
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
                prefix = "failed to roll back" if attempt == 0 else "retry failed"
                primary_error.add_note(
                    f"{prefix} {context}: {rollback_error}"
                )
                continue
            if not self._connection.in_transaction:
                return
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite entity registry connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Cursor]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_entity_registry_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to clean up SQLite entity registry savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_or_close(
                        primary_error,
                        context="outer transaction after savepoint failure",
                    )

            try:
                with closing(self._connection.cursor()) as cursor:
                    self._lock_schema(cursor)
                    yield cursor
            except BaseException as error:
                rollback_savepoint(error)
                raise
            else:
                try:
                    self._connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                except BaseException as error:
                    rollback_savepoint(error)
                    raise
            return

        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            with closing(self._connection.cursor()) as cursor:
                self._lock_schema(cursor)
                yield cursor
        except BaseException as error:
            self._rollback_or_close(
                error,
                context="top-level SQLite entity registry transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_or_close(
                    error,
                    context="top-level SQLite entity registry transaction",
                )
                raise

    @staticmethod
    def _snapshot_values(
        registry: EntityRegistrySnapshot,
    ) -> tuple[str, str, str, str]:
        descriptor = dumps_entity_registry(registry)
        if len(descriptor.encode("utf-8")) > ENTITY_REGISTRY_JSON_MAX_BYTES:
            raise ValueError("entity registry descriptor exceeds storage limit")
        return (
            registry.registry_sha256,
            registry.registry_version,
            registry.authorization_policy.policy_sha256,
            descriptor,
        )

    @classmethod
    def _stored_snapshot(
        cls,
        row: tuple[object, ...],
    ) -> EntityRegistrySnapshot:
        if len(row) != 4 or type(row[3]) is not str:
            cls._persistence("entity registry snapshot row has invalid shape")
        try:
            registry = loads_entity_registry(cast(str, row[3]))
        except EntityRegistryContractError as error:
            raise SQLiteEntityRegistryV3PersistenceError(
                "entity registry descriptor failed validation"
            ) from error
        if row != cls._snapshot_values(registry):
            cls._persistence(
                "entity registry snapshot columns do not match descriptor"
            )
        return registry

    @staticmethod
    def _expected_rows(
        registry: EntityRegistrySnapshot,
    ) -> dict[str, tuple[tuple[object, ...], ...]]:
        digest = registry.registry_sha256
        policy = registry.authorization_policy
        rows: dict[str, list[tuple[object, ...]]] = {
            table: [] for table in _TABLE_COLUMNS
        }
        for item in registry.organizations:
            rows["v3_entity_registry_organizations"].append(
                (digest, item.organization_id, item.display_name, item.status)
            )
        for item in registry.tenants:
            rows["v3_entity_registry_tenants"].append(
                (
                    digest,
                    item.tenant_id,
                    item.organization_id,
                    item.display_name,
                    item.status,
                )
            )
        for item in policy.repositories:
            rows["v3_entity_registry_repositories"].append(
                (
                    digest,
                    item.repository_id,
                    item.provider,
                    item.provider_repository_id,
                    item.canonical_locator_hash,
                    item.display_name,
                )
            )
            for alias in item.legacy_aliases:
                rows[
                    "v3_entity_registry_repository_legacy_aliases"
                ].append((digest, item.repository_id, alias))
        for item in policy.repository_tenants:
            rows["v3_entity_registry_repository_tenants"].append(
                (digest, item.repository_id, item.tenant_id)
            )
        for item in policy.repository_aliases:
            rows["v3_entity_registry_repository_aliases"].append(
                (
                    digest,
                    item.tenant_id,
                    item.alias,
                    item.repository_id,
                    item.source,
                )
            )
        for item in policy.principals:
            rows["v3_entity_registry_principals"].append(
                (
                    digest,
                    item.principal_id,
                    item.issuer,
                    item.subject_hash,
                    item.tenant_id,
                    item.status,
                )
            )
        for item in policy.agent_clients:
            rows["v3_entity_registry_agent_clients"].append(
                (
                    digest,
                    item.agent_client_id,
                    item.tenant_id,
                    item.client_kind,
                    item.status,
                )
            )
        for item in registry.environments:
            rows["v3_entity_registry_environments"].append(
                (
                    digest,
                    item.environment_id,
                    item.tenant_id,
                    item.repository_id,
                    item.environment_kind,
                    item.display_name,
                    item.status,
                )
            )
            for name, value in item.attributes:
                rows["v3_entity_registry_environment_attributes"].append(
                    (digest, item.environment_id, name, value)
                )
        for item in policy.role_bindings:
            scope = item.scope
            rows["v3_entity_registry_role_bindings"].append(
                (
                    digest,
                    item.binding_id,
                    item.principal_id,
                    item.agent_client_id,
                    item.role_name,
                    scope.kind,
                    scope.tenant_id,
                    scope.repository_id,
                    item.status,
                    item.to_dict()["valid_from"],
                    item.to_dict()["expires_at"],
                )
            )
            for permission in item.permissions:
                rows["v3_entity_registry_binding_permissions"].append(
                    (digest, item.binding_id, permission)
                )
            for name, value in scope.attributes:
                rows["v3_entity_registry_scope_attributes"].append(
                    (digest, item.binding_id, name, value)
                )
        return {
            table: tuple(sorted(values, key=repr))
            for table, values in rows.items()
        }

    @classmethod
    def _verify_normalized_rows(
        cls,
        cursor: sqlite3.Cursor,
        registry: EntityRegistrySnapshot,
    ) -> None:
        expected = cls._expected_rows(registry)
        for table, columns in _TABLE_COLUMNS.items():
            column_sql = ", ".join(columns)
            maximum_rows = len(expected[table]) + 1
            cursor.execute(
                f"SELECT {column_sql} FROM {table} "
                "WHERE registry_sha256 = ? "
                "LIMIT ?",
                (registry.registry_sha256, maximum_rows),
            )
            actual = tuple(sorted(cursor.fetchall(), key=repr))
            if actual != expected[table]:
                cls._persistence(
                    f"normalized {table} rows do not match descriptor"
                )

    @staticmethod
    def _insert_rows(
        cursor: sqlite3.Cursor,
        registry: EntityRegistrySnapshot,
    ) -> None:
        for table, rows in SQLiteEntityRegistryV3Repository._expected_rows(
            registry
        ).items():
            if not rows:
                continue
            columns = _TABLE_COLUMNS[table]
            placeholders = ", ".join("?" for _ in columns)
            cursor.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                rows,
            )

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise SQLiteEntityRegistryV3PersistenceError(message)

    @staticmethod
    def _validate_digest(value: object) -> str:
        if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("registry_sha256 must be a sha256 digest")
        return value

    @staticmethod
    def _validate_version(value: object) -> str:
        if type(value) is not str or not value.strip() or len(value) > 512:
            raise ValueError(
                "registry_version must be non-empty bounded metadata"
            )
        return value

    @_synchronized
    def put(self, registry: EntityRegistrySnapshot) -> bool:
        self._require_open()
        if type(registry) is not EntityRegistrySnapshot:
            raise ValueError(
                "registry must be exactly EntityRegistrySnapshot"
            )
        values = self._snapshot_values(registry)
        try:
            with self._transaction(write=True) as cursor:
                cursor.execute(
                    "SELECT registry_sha256, registry_version, policy_sha256, "
                    "descriptor FROM v3_entity_registry_snapshots "
                    "WHERE registry_sha256 = ? OR registry_version = ?",
                    (registry.registry_sha256, registry.registry_version),
                )
                existing = cursor.fetchall()
                if existing:
                    if len(existing) != 1:
                        self._persistence(
                            "entity registry identity lookup is ambiguous"
                        )
                    stored = self._stored_snapshot(existing[0])
                    self._verify_normalized_rows(cursor, stored)
                    if stored == registry:
                        return False
                    raise SQLiteEntityRegistryV3ConflictError(
                        "entity registry identity already has different content"
                    )
                cursor.execute(
                    "INSERT INTO v3_entity_registry_snapshots "
                    "(registry_sha256, registry_version, policy_sha256, "
                    "descriptor) VALUES (?, ?, ?, ?)",
                    values,
                )
                self._insert_rows(cursor, registry)
                self._verify_normalized_rows(cursor, registry)
                return True
        except SQLiteEntityRegistryV3Error:
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def get(self, registry_sha256: str) -> EntityRegistrySnapshot:
        self._require_open()
        digest = self._validate_digest(registry_sha256)
        try:
            with self._transaction(write=False) as cursor:
                cursor.execute(
                    "SELECT registry_sha256, registry_version, policy_sha256, "
                    "descriptor FROM v3_entity_registry_snapshots "
                    "WHERE registry_sha256 = ?",
                    (digest,),
                )
                rows = cursor.fetchall()
                if not rows:
                    raise KeyError(digest)
                if len(rows) != 1:
                    self._persistence(
                        "entity registry digest lookup is ambiguous"
                    )
                registry = self._stored_snapshot(rows[0])
                self._verify_normalized_rows(cursor, registry)
                return registry
        except (KeyError, SQLiteEntityRegistryV3Error):
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def get_by_version(
        self,
        registry_version: str,
    ) -> EntityRegistrySnapshot:
        self._require_open()
        version = self._validate_version(registry_version)
        try:
            with self._transaction(write=False) as cursor:
                cursor.execute(
                    "SELECT registry_sha256, registry_version, policy_sha256, "
                    "descriptor FROM v3_entity_registry_snapshots "
                    "WHERE registry_version = ?",
                    (version,),
                )
                rows = cursor.fetchall()
                if not rows:
                    raise KeyError(version)
                if len(rows) != 1:
                    self._persistence(
                        "entity registry version lookup is ambiguous"
                    )
                registry = self._stored_snapshot(rows[0])
                self._verify_normalized_rows(cursor, registry)
                return registry
        except (KeyError, SQLiteEntityRegistryV3Error):
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def list_versions(
        self,
        *,
        limit: int = SQLITE_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE,
    ) -> tuple[str, ...]:
        self._require_open()
        if type(limit) is not int or not 1 <= limit <= SQLITE_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE:
            raise ValueError(
                "limit must be an integer between 1 and "
                f"{SQLITE_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE}"
            )
        try:
            with self._transaction(write=False) as cursor:
                cursor.execute(
                    "SELECT registry_version "
                    "FROM v3_entity_registry_snapshots "
                    "ORDER BY registry_version LIMIT ?",
                    (limit,),
                )
                rows = cursor.fetchall()
                if any(len(row) != 1 or type(row[0]) is not str for row in rows):
                    self._persistence(
                        "entity registry version rows have invalid shape"
                    )
                return tuple(cast(str, row[0]) for row in rows)
        except SQLiteEntityRegistryV3Error:
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    def _raise_sqlite(self, error: sqlite3.Error) -> NoReturn:
        message = str(error).casefold()
        if any(
            marker in message
            for marker in (
                "no such table",
                "no such column",
                "malformed",
                "foreign key mismatch",
                "schema",
            )
        ):
            raise SQLiteEntityRegistryV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        raise SQLiteEntityRegistryV3PersistenceError(
            "SQLite entity registry v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteEntityRegistryV3Repository:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()
