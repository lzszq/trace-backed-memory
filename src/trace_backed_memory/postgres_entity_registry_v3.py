from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .entity_registry_v3 import (
    ENTITY_REGISTRY_CONTRACT_VERSION,
    EntityRegistryContractError,
    EntityRegistrySnapshot,
    dumps_entity_registry,
    loads_entity_registry,
)
from .postgres import _load_psycopg
from .postgres_authorization_v3 import _CATALOG_SHA256_QUERY
from .sqlite_entity_registry_v3 import (
    _TABLE_COLUMNS,
    SQLiteEntityRegistryV3Repository,
)


POSTGRES_ENTITY_REGISTRY_V3_SCHEMA_VERSION = 1
POSTGRES_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE = 1000
_SCHEMA = "trace_backed_memory_v3_entity_registry"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL entity registry v3 schema is missing or incomplete"
)
_EXPECTED_CATALOG_SHA256 = (
    "bb036194f553ee4b8d6a2bffac8c0f25a435bac9449faf9dca169fb6b1bca574"
)
_ENTITY_CATALOG_SHA256_QUERY = _CATALOG_SHA256_QUERY.replace(
    "trace_backed_memory_v3_authorization.",
    "trace_backed_memory_v3_entity_registry.",
)
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresEntityRegistryV3Error(RuntimeError):
    pass


class PostgresEntityRegistryV3SchemaError(PostgresEntityRegistryV3Error):
    pass


class PostgresEntityRegistryV3ConflictError(PostgresEntityRegistryV3Error):
    pass


class PostgresEntityRegistryV3PersistenceError(PostgresEntityRegistryV3Error):
    pass


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


class PostgresEntityRegistryV3Repository:
    """Immutable normalized PostgreSQL authority for entity registries."""

    def __init__(self, connection: object, *, owns_connection: bool = False):
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresEntityRegistryV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresEntityRegistryV3PersistenceError(
                "failed to connect to PostgreSQL entity registry v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresEntityRegistryV3Error(
                "PostgreSQL entity registry v3 repository is closed"
            )

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            "SELECT active.schema_version AS active_version, "
            "entity.schema_version AS entity_version, "
            "entity.contract_version AS contract_version "
            "FROM public.trace_backed_memory_schema AS active "
            "CROSS JOIN "
            "trace_backed_memory_v3_entity_registry.schema_metadata AS entity "
            "WHERE active.singleton AND entity.singleton = 1 "
            "FOR SHARE OF active, entity"
        )
        rows = cursor.fetchall()
        if rows != [
            {
                "active_version": 2,
                "entity_version": POSTGRES_ENTITY_REGISTRY_V3_SCHEMA_VERSION,
                "contract_version": ENTITY_REGISTRY_CONTRACT_VERSION,
            }
        ]:
            raise PostgresEntityRegistryV3SchemaError(
                "PostgreSQL entity registry v3 metadata mismatch"
            )
        cursor.execute(
            "SELECT "
            "(SELECT count(*) FROM pg_catalog.pg_policy AS policy "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = policy.polrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s) AS policy_count, "
            "(SELECT count(*) FROM pg_catalog.pg_rewrite AS rule "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = rule.ev_class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND rule.rulename <> '_RETURN') AS rule_count, "
            "(SELECT count(*) FROM pg_catalog.pg_class AS class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND class.relkind NOT IN ('r', 'i', 'p')) "
            "AS unsupported_relation_count",
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        extension_rows = cursor.fetchall()
        if extension_rows != [
            {
                "policy_count": 0,
                "rule_count": 0,
                "unsupported_relation_count": 0,
            }
        ]:
            raise PostgresEntityRegistryV3SchemaError(
                "PostgreSQL entity registry v3 contains unsupported "
                "policies, rules, or relation kinds"
            )
        cursor.execute(_ENTITY_CATALOG_SHA256_QUERY, (_SCHEMA,) * 7)
        catalog_rows = cursor.fetchall()
        if (
            len(catalog_rows) != 1
            or catalog_rows[0].get("catalog_sha256")
            != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresEntityRegistryV3SchemaError(
                "PostgreSQL entity registry v3 catalog does not match"
            )

    @staticmethod
    def _snapshot_values(
        registry: EntityRegistrySnapshot,
    ) -> tuple[str, str, str, str]:
        return (
            registry.registry_sha256,
            registry.registry_version,
            registry.authorization_policy.policy_sha256,
            dumps_entity_registry(registry),
        )

    @classmethod
    def _stored_snapshot(
        cls,
        row: Mapping[str, object],
    ) -> EntityRegistrySnapshot:
        values = (
            row.get("registry_sha256"),
            row.get("registry_version"),
            row.get("policy_sha256"),
            row.get("descriptor"),
        )
        if type(values[3]) is not str:
            cls._persistence("entity registry snapshot row has invalid shape")
        try:
            registry = loads_entity_registry(cast(str, values[3]))
        except EntityRegistryContractError as error:
            raise PostgresEntityRegistryV3PersistenceError(
                "entity registry descriptor failed validation"
            ) from error
        if values != cls._snapshot_values(registry):
            cls._persistence(
                "entity registry snapshot columns do not match descriptor"
            )
        return registry

    @staticmethod
    def _expected_rows(
        registry: EntityRegistrySnapshot,
    ) -> dict[str, tuple[tuple[object, ...], ...]]:
        return SQLiteEntityRegistryV3Repository._expected_rows(registry)

    @classmethod
    def _verify_normalized_rows(
        cls,
        cursor: object,
        registry: EntityRegistrySnapshot,
    ) -> None:
        expected = cls._expected_rows(registry)
        for table, columns in _TABLE_COLUMNS.items():
            cursor.execute(
                f"SELECT {', '.join(columns)} FROM {_SCHEMA}.{table} "
                "WHERE registry_sha256 = %s LIMIT %s",
                (registry.registry_sha256, len(expected[table]) + 1),
            )
            actual = tuple(
                sorted(
                    (
                        tuple(row[column] for column in columns)
                        for row in cursor.fetchall()
                    ),
                    key=repr,
                )
            )
            if actual != expected[table]:
                cls._persistence(
                    f"normalized {table} rows do not match descriptor"
                )

    @classmethod
    def _insert_rows(
        cls,
        cursor: object,
        registry: EntityRegistrySnapshot,
    ) -> None:
        for table, rows in cls._expected_rows(registry).items():
            if not rows:
                continue
            columns = _TABLE_COLUMNS[table]
            cursor.executemany(
                f"INSERT INTO {_SCHEMA}.{table} "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('%s' for _ in columns)})",
                rows,
            )

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise PostgresEntityRegistryV3PersistenceError(message)

    @staticmethod
    def _digest(value: object) -> str:
        if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("registry_sha256 must be a sha256 digest")
        return value

    @staticmethod
    def _version(value: object) -> str:
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
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        f"INSERT INTO {_SCHEMA}.v3_entity_registry_snapshots "
                        "(registry_sha256, registry_version, policy_sha256, "
                        "descriptor) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING RETURNING registry_sha256",
                        self._snapshot_values(registry),
                    )
                    inserted = cursor.fetchone()
                    if inserted is None:
                        cursor.execute(
                            "SELECT registry_sha256, registry_version, "
                            "policy_sha256, descriptor "
                            f"FROM {_SCHEMA}.v3_entity_registry_snapshots "
                            "WHERE registry_sha256 = %s "
                            "OR registry_version = %s FOR SHARE",
                            (
                                registry.registry_sha256,
                                registry.registry_version,
                            ),
                        )
                        rows = cursor.fetchall()
                        if len(rows) != 1:
                            self._persistence(
                                "entity registry identity lookup is ambiguous"
                            )
                        stored = self._stored_snapshot(rows[0])
                        self._verify_normalized_rows(cursor, stored)
                        if stored == registry:
                            return False
                        raise PostgresEntityRegistryV3ConflictError(
                            "entity registry identity has different content"
                        )
                    self._insert_rows(cursor, registry)
                    self._verify_normalized_rows(cursor, registry)
                    return True
        except PostgresEntityRegistryV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    def _get_by(
        self,
        column: str,
        value: str,
    ) -> EntityRegistrySnapshot:
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        "SELECT registry_sha256, registry_version, "
                        "policy_sha256, descriptor "
                        f"FROM {_SCHEMA}.v3_entity_registry_snapshots "
                        f"WHERE {column} = %s FOR SHARE",
                        (value,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise KeyError(value)
                    if len(rows) != 1:
                        self._persistence(
                            "entity registry lookup is ambiguous"
                        )
                    registry = self._stored_snapshot(rows[0])
                    self._verify_normalized_rows(cursor, registry)
                    return registry
        except (KeyError, PostgresEntityRegistryV3Error):
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def get(self, registry_sha256: str) -> EntityRegistrySnapshot:
        self._require_open()
        return self._get_by(
            "registry_sha256",
            self._digest(registry_sha256),
        )

    @_synchronized
    def get_by_version(
        self,
        registry_version: str,
    ) -> EntityRegistrySnapshot:
        self._require_open()
        return self._get_by(
            "registry_version",
            self._version(registry_version),
        )

    @_synchronized
    def list_versions(
        self,
        *,
        limit: int = POSTGRES_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE,
    ) -> tuple[str, ...]:
        self._require_open()
        if (
            type(limit) is not int
            or not 1 <= limit <= POSTGRES_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE
        ):
            raise ValueError(
                "limit must be an integer between 1 and "
                f"{POSTGRES_ENTITY_REGISTRY_V3_MAX_PAGE_SIZE}"
            )
        try:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        "SELECT registry_version "
                        f"FROM {_SCHEMA}.v3_entity_registry_snapshots "
                        "ORDER BY registry_version LIMIT %s",
                        (limit,),
                    )
                    rows = cursor.fetchall()
                    if any(
                        type(row.get("registry_version")) is not str
                        for row in rows
                    ):
                        self._persistence(
                            "entity registry version rows have invalid shape"
                        )
                    return tuple(
                        cast(str, row["registry_version"]) for row in rows
                    )
        except PostgresEntityRegistryV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    def _raise_database(self, error: Exception) -> NoReturn:
        if getattr(error, "sqlstate", None) in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresEntityRegistryV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        raise PostgresEntityRegistryV3PersistenceError(
            "PostgreSQL entity registry v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresEntityRegistryV3Repository:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()
