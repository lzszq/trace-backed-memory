from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from functools import lru_cache, wraps
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .managed_index_v3 import (
    MANAGED_INDEX_BUNDLE_CONTRACT_VERSION,
    MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES,
    ManagedIndexBundle,
    ManagedIndexPublication,
    dumps_managed_index_bundle,
    loads_managed_index_bundle,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_MANAGED_INDEX_V3_SCHEMA_VERSION = 1
_SCHEMA_RESOURCE = "schemas/sqlite-v3-managed-index.sql"
_BUNDLE_ID_RE = re.compile(r"^managed_index_bundle_sha256_[0-9a-f]{64}$")
_MISSING_SCHEMA_MESSAGE = "SQLite managed index v3 schema is missing or incomplete"
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteManagedIndexV3Error(RuntimeError):
    pass


class SQLiteManagedIndexV3SchemaError(SQLiteManagedIndexV3Error):
    pass


class SQLiteManagedIndexV3ConflictError(SQLiteManagedIndexV3Error):
    pass


class SQLiteManagedIndexV3PersistenceError(SQLiteManagedIndexV3Error):
    pass


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str:
        raise SQLiteManagedIndexV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    return " ".join(value.split())


def _read_schema_definitions(
    cursor: sqlite3.Cursor,
) -> tuple[tuple[str, str, str, str], ...]:
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM ("
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "UNION ALL "
        "SELECT type, name, tbl_name, sql FROM sqlite_temp_master"
        ") "
        "WHERE name LIKE 'trace_backed_memory_v3_managed_index_%' "
        "OR name LIKE 'v3_managed_index_%' "
        "OR (tbl_name IN ("
        "'trace_backed_memory_v3_managed_index_schema', "
        "'v3_managed_index_bundles', "
        "'v3_managed_index_heads'"
        ") AND name NOT LIKE 'sqlite_autoindex_%') "
        "ORDER BY type, name"
    )
    rows = cursor.fetchall()
    result: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            type(row) is not tuple
            or len(row) != 4
            or any(type(item) is not str for item in row)
        ):
            raise SQLiteManagedIndexV3SchemaError(_MISSING_SCHEMA_MESSAGE)
        result.append(
            (
                cast(str, row[0]),
                cast(str, row[1]),
                cast(str, row[2]),
                _normalized_schema_sql(row[3]),
            )
        )
    return tuple(result)


@lru_cache(maxsize=1)
def _canonical_schema_definitions() -> tuple[tuple[str, str, str, str], ...]:
    try:
        schema = read_packaged_resource(_SCHEMA_RESOURCE).decode("utf-8")
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(schema)
            cursor = connection.cursor()
            try:
                return _read_schema_definitions(cursor)
            finally:
                cursor.close()
        finally:
            connection.close()
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        raise SQLiteManagedIndexV3SchemaError(
            "canonical SQLite managed index v3 schema is unavailable"
        ) from error


class SQLiteManagedIndexV3Repository:
    """Immutable managed-index bundles plus a scope-local CAS head."""

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
    ) -> SQLiteManagedIndexV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(_SCHEMA_RESOURCE).decode("utf-8")
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
            raise SQLiteManagedIndexV3PersistenceError(
                "failed to connect to SQLite managed index v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteManagedIndexV3Error(
                "SQLite managed index v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteManagedIndexV3Error(
                "SQLite managed index v3 repository is closed"
            ) from error

    def _lock_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_managed_index_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [
            (
                SQLITE_MANAGED_INDEX_V3_SCHEMA_VERSION,
                MANAGED_INDEX_BUNDLE_CONTRACT_VERSION,
            )
        ]:
            raise SQLiteManagedIndexV3SchemaError(
                "SQLite managed index v3 schema metadata mismatch"
            )
        if _read_schema_definitions(cursor) != (_canonical_schema_definitions()):
            raise SQLiteManagedIndexV3SchemaError(
                "SQLite managed index v3 schema definitions do not match"
            )
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteManagedIndexV3SchemaError(
                "SQLite managed index v3 requires foreign keys"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteManagedIndexV3SchemaError(
                "SQLite managed index v3 requires recursive triggers"
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
                primary_error.add_note(f"{prefix} {context}: {rollback_error}")
                continue
            if not self._connection.in_transaction:
                return
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite managed index connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Cursor]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_managed_index_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to clean up SQLite managed index savepoint "
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
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
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
                context="top-level SQLite managed index transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_or_close(
                    error,
                    context="top-level SQLite managed index transaction",
                )
                raise

    @staticmethod
    def _bundle_bytes(bundle: ManagedIndexBundle) -> bytes:
        payload = dumps_managed_index_bundle(bundle).encode("utf-8")
        if len(payload) > MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES:
            raise ValueError("managed index bundle exceeds maximum bytes")
        return payload

    @staticmethod
    def _stored_bundle(row: object) -> ManagedIndexBundle:
        if (
            type(row) is not tuple
            or len(row) != 8
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
            or type(row[3]) is not str
            or type(row[4]) is not str
            or type(row[5]) is not str
            or type(row[6]) is not str
            or type(row[7]) is not bytes
        ):
            raise SQLiteManagedIndexV3PersistenceError(
                "stored managed index bundle has invalid shape"
            )
        try:
            bundle = loads_managed_index_bundle(row[7])
        except ValueError as error:
            raise SQLiteManagedIndexV3PersistenceError(
                "stored managed index bundle is invalid"
            ) from error
        if (
            bundle.bundle_id != row[0]
            or bundle.tenant_id != row[1]
            or bundle.repository_id != row[2]
            or bundle.environment_id != row[3]
            or bundle.retriever_id != row[4]
            or bundle.retriever_version != row[5]
            or bundle.source_catalog_sha256 != row[6]
            or dumps_managed_index_bundle(bundle).encode("utf-8") != row[7]
        ):
            raise SQLiteManagedIndexV3PersistenceError(
                "stored managed index columns do not match exact bundle bytes"
            )
        return bundle

    @staticmethod
    def _validate_bundle_id(value: object) -> str:
        if type(value) is not str or _BUNDLE_ID_RE.fullmatch(value) is None:
            raise ValueError("bundle_id must be a managed index bundle ID")
        return value

    @staticmethod
    def _validate_scope(
        tenant_id: object,
        repository_id: object,
        environment_id: object,
    ) -> tuple[str, str, str]:
        values = (tenant_id, repository_id, environment_id)
        if any(
            type(value) is not str
            or not cast(str, value).strip()
            or len(cast(str, value)) > 128
            for value in values
        ):
            raise ValueError("managed index scope identifiers are invalid")
        return cast(tuple[str, str, str], values)

    @staticmethod
    def _select_bundle(
        cursor: sqlite3.Cursor,
        bundle_id: str,
    ) -> ManagedIndexBundle:
        cursor.execute(
            "SELECT bundle_id, tenant_id, repository_id, environment_id, "
            "retriever_id, retriever_version, source_catalog_sha256, "
            "payload_utf8 FROM v3_managed_index_bundles "
            "WHERE bundle_id = ?",
            (bundle_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise KeyError(bundle_id)
        if len(rows) != 1:
            raise SQLiteManagedIndexV3PersistenceError(
                "managed index bundle lookup is ambiguous"
            )
        return SQLiteManagedIndexV3Repository._stored_bundle(rows[0])

    @_synchronized
    def publish(
        self,
        bundle: ManagedIndexBundle,
        *,
        expected_current_bundle_id: str | None,
    ) -> ManagedIndexPublication:
        self._require_open()
        if type(bundle) is not ManagedIndexBundle:
            raise ValueError("bundle must be exactly ManagedIndexBundle")
        if expected_current_bundle_id is not None:
            self._validate_bundle_id(expected_current_bundle_id)
        payload = self._bundle_bytes(bundle)
        try:
            with self._transaction(write=True) as cursor:
                cursor.execute(
                    "SELECT bundle_id, tenant_id, repository_id, "
                    "environment_id, retriever_id, retriever_version, "
                    "source_catalog_sha256, payload_utf8 "
                    "FROM v3_managed_index_bundles WHERE bundle_id = ?",
                    (bundle.bundle_id,),
                )
                existing = cursor.fetchall()
                if existing:
                    if len(existing) != 1:
                        raise SQLiteManagedIndexV3PersistenceError(
                            "managed index bundle lookup is ambiguous"
                        )
                    if self._stored_bundle(existing[0]) != bundle:
                        raise SQLiteManagedIndexV3ConflictError(
                            "managed index bundle ID already has different content"
                        )
                else:
                    cursor.execute(
                        "INSERT INTO v3_managed_index_bundles "
                        "(bundle_id, tenant_id, repository_id, "
                        "environment_id, retriever_id, retriever_version, "
                        "source_catalog_sha256, payload_utf8) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bundle.bundle_id,
                            bundle.tenant_id,
                            bundle.repository_id,
                            bundle.environment_id,
                            bundle.retriever_id,
                            bundle.retriever_version,
                            bundle.source_catalog_sha256,
                            payload,
                        ),
                    )
                scope = (
                    bundle.tenant_id,
                    bundle.repository_id,
                    bundle.environment_id,
                )
                cursor.execute(
                    "SELECT bundle_id, head_version "
                    "FROM v3_managed_index_heads "
                    "WHERE tenant_id = ? AND repository_id = ? "
                    "AND environment_id = ?",
                    scope,
                )
                head_rows = cursor.fetchall()
                if not head_rows:
                    if expected_current_bundle_id is not None:
                        raise SQLiteManagedIndexV3ConflictError(
                            "managed index head does not match expected current bundle"
                        )
                    cursor.execute(
                        "INSERT INTO v3_managed_index_heads "
                        "(tenant_id, repository_id, environment_id, "
                        "bundle_id, head_version) VALUES (?, ?, ?, ?, 1)",
                        (*scope, bundle.bundle_id),
                    )
                    previous = None
                    head_version = 1
                    changed = True
                else:
                    if (
                        len(head_rows) != 1
                        or type(head_rows[0]) is not tuple
                        or len(head_rows[0]) != 2
                        or type(head_rows[0][0]) is not str
                        or type(head_rows[0][1]) is not int
                    ):
                        raise SQLiteManagedIndexV3PersistenceError(
                            "managed index head has invalid shape"
                        )
                    current = cast(str, head_rows[0][0])
                    current_version = cast(int, head_rows[0][1])
                    if current == bundle.bundle_id:
                        previous = current
                        head_version = current_version
                        changed = False
                    else:
                        if expected_current_bundle_id != current:
                            raise SQLiteManagedIndexV3ConflictError(
                                "managed index head does not match expected current bundle"
                            )
                        cursor.execute(
                            "UPDATE v3_managed_index_heads "
                            "SET bundle_id = ?, head_version = head_version + 1 "
                            "WHERE tenant_id = ? AND repository_id = ? "
                            "AND environment_id = ? AND bundle_id = ? "
                            "AND head_version = ?",
                            (
                                bundle.bundle_id,
                                *scope,
                                current,
                                current_version,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise SQLiteManagedIndexV3ConflictError(
                                "managed index head changed during publication"
                            )
                        previous = current
                        head_version = current_version + 1
                        changed = True
                stored = self._select_bundle(cursor, bundle.bundle_id)
                cursor.execute(
                    "SELECT bundle_id, head_version "
                    "FROM v3_managed_index_heads "
                    "WHERE tenant_id = ? AND repository_id = ? "
                    "AND environment_id = ?",
                    scope,
                )
                readback = cursor.fetchall()
                if readback != [(bundle.bundle_id, head_version)]:
                    raise SQLiteManagedIndexV3PersistenceError(
                        "managed index publication read-back failed"
                    )
                return ManagedIndexPublication(
                    bundle=stored,
                    previous_bundle_id=previous,
                    head_version=head_version,
                    changed=changed,
                )
        except SQLiteManagedIndexV3Error:
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def load(self, bundle_id: str) -> ManagedIndexBundle:
        self._require_open()
        validated = self._validate_bundle_id(bundle_id)
        try:
            with self._transaction(write=False) as cursor:
                return self._select_bundle(cursor, validated)
        except (KeyError, SQLiteManagedIndexV3Error):
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def load_current(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        environment_id: str,
    ) -> ManagedIndexBundle:
        self._require_open()
        scope = self._validate_scope(
            tenant_id,
            repository_id,
            environment_id,
        )
        try:
            with self._transaction(write=False) as cursor:
                cursor.execute(
                    "SELECT bundle_id FROM v3_managed_index_heads "
                    "WHERE tenant_id = ? AND repository_id = ? "
                    "AND environment_id = ?",
                    scope,
                )
                rows = cursor.fetchall()
                if not rows:
                    raise KeyError(scope)
                if (
                    len(rows) != 1
                    or type(rows[0]) is not tuple
                    or len(rows[0]) != 1
                    or type(rows[0][0]) is not str
                ):
                    raise SQLiteManagedIndexV3PersistenceError(
                        "managed index head has invalid shape"
                    )
                bundle = self._select_bundle(
                    cursor,
                    cast(str, rows[0][0]),
                )
                if (
                    bundle.tenant_id,
                    bundle.repository_id,
                    bundle.environment_id,
                ) != scope:
                    raise SQLiteManagedIndexV3PersistenceError(
                        "managed index head scope does not match bundle"
                    )
                return bundle
        except (KeyError, SQLiteManagedIndexV3Error):
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
            raise SQLiteManagedIndexV3SchemaError(_MISSING_SCHEMA_MESSAGE) from error
        if "unique" in message or "constraint" in message:
            raise SQLiteManagedIndexV3ConflictError(
                "managed index persistence conflict"
            ) from error
        raise SQLiteManagedIndexV3PersistenceError(
            "SQLite managed index v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteManagedIndexV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "SQLITE_MANAGED_INDEX_V3_SCHEMA_VERSION",
    "SQLiteManagedIndexV3ConflictError",
    "SQLiteManagedIndexV3Error",
    "SQLiteManagedIndexV3PersistenceError",
    "SQLiteManagedIndexV3Repository",
    "SQLiteManagedIndexV3SchemaError",
]
