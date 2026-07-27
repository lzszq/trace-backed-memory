from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import ParamSpec, TypeVar

from .contracts_v3 import CommitRelationVerifier
from .migration_v3 import (
    V3_MIGRATION_BUNDLE_MAX_BYTES,
    SnapshotV3MigrationBundle,
    V3MigrationBundleError,
    dumps_snapshot_v3_migration_bundle,
    loads_snapshot_v3_migration_bundle,
    verify_snapshot_v3_migration_bundle,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_V3_MIGRATION_SCHEMA_VERSION = 1
_MISSING_SCHEMA_MESSAGE = "SQLite v3 migration schema is missing or incomplete"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_migration_schema",
    "v3_migration_bundles",
    "v3_migration_bundles_immutable_delete",
    "v3_migration_bundles_immutable_update",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteV3MigrationError(RuntimeError):
    pass


class SQLiteV3MigrationSchemaError(SQLiteV3MigrationError):
    pass


class SQLiteV3MigrationConflictError(SQLiteV3MigrationError):
    pass


class SQLiteV3MigrationPersistenceError(SQLiteV3MigrationError):
    pass


@dataclass(frozen=True)
class SQLiteV3MigrationStageResult:
    bundle_id: str
    state: str
    inserted: bool


def _synchronized(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


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


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteV3MigrationSchemaError(
            "SQLite v3 migration schema contains an invalid definition"
        )
    return "".join(value.split()).casefold()


def _read_schema_definitions(
    cursor: sqlite3.Cursor,
) -> tuple[tuple[str, str, str, str], ...]:
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name IN (?, ?, ?, ?) ORDER BY name",
        _SCHEMA_OBJECT_NAMES,
    )
    rows = cursor.fetchall()
    if len(rows) != len(_SCHEMA_OBJECT_NAMES):
        raise SQLiteV3MigrationSchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteV3MigrationSchemaError(
                "SQLite v3 migration schema definition has an invalid shape"
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    return tuple(definitions)


@lru_cache(maxsize=1)
def _canonical_schema_definitions() -> tuple[
    tuple[str, str, str, str],
    ...,
]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                read_packaged_resource(
                    "schemas/sqlite-v3-migration.sql"
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
        raise SQLiteV3MigrationSchemaError(
            "could not validate the canonical SQLite v3 migration schema"
        ) from error


class SQLiteV3MigrationRepository:
    """Immutable SQLite staging for inert v2-to-v3 migration bundles."""

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
    ) -> "SQLiteV3MigrationRepository":
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        "schemas/sqlite-v3-migration.sql"
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
            raise SQLiteV3MigrationPersistenceError(
                "failed to connect to SQLite v3 migration staging"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteV3MigrationError(
                "SQLite v3 migration repository is closed"
            )
        try:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteV3MigrationError(
                "SQLite v3 migration repository is closed"
            ) from error

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
                "failed to close unusable SQLite v3 migration connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_v3_migration_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to roll back SQLite v3 migration savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after v3 migration "
                            "savepoint cleanup failed"
                        ),
                    )
                    return
                try:
                    self._connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                except BaseException as cleanup_error:
                    try:
                        self._connection.execute(
                            f"RELEASE SAVEPOINT {savepoint}"
                        )
                    except BaseException as retry_error:
                        primary_error.add_note(
                            "failed to release SQLite v3 migration savepoint "
                            f"{savepoint}: {cleanup_error}; retry failed: "
                            f"{retry_error}"
                        )
                        self._rollback_connection_or_close(
                            primary_error,
                            context=(
                                "the outer SQLite transaction after an "
                                "unreleased v3 migration savepoint"
                            ),
                        )

            try:
                yield
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

        def rollback_top_level(primary_error: BaseException) -> None:
            self._rollback_connection_or_close(
                primary_error,
                context="the top-level SQLite v3 migration transaction",
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
            "SELECT schema_version "
            "FROM trace_backed_memory_v3_migration_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise SQLiteV3MigrationSchemaError(
                "SQLite v3 migration schema metadata must contain exactly "
                "one row"
            )
        version = rows[0][0]
        if version != SQLITE_V3_MIGRATION_SCHEMA_VERSION:
            raise SQLiteV3MigrationSchemaError(
                "SQLite v3 migration schema version mismatch: expected "
                f"{SQLITE_V3_MIGRATION_SCHEMA_VERSION}, found {version}"
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteV3MigrationSchemaError(
                "SQLite v3 migration schema definitions do not match the "
                "canonical version"
            )

    @staticmethod
    def _expected_row(
        bundle: SnapshotV3MigrationBundle,
        payload: str,
    ) -> tuple[object, ...]:
        return (
            bundle.bundle_id,
            bundle.bundle_version,
            bundle.state,
            bundle.source_snapshot_sha256,
            bundle.normalized_source_snapshot_sha256,
            bundle.mapping_sha256,
            bundle.plan_sha256,
            payload,
        )

    @staticmethod
    def _row_bundle(row: tuple[object, ...]) -> SnapshotV3MigrationBundle:
        if len(row) != 8:
            raise SQLiteV3MigrationPersistenceError(
                "SQLite v3 migration row has an invalid shape"
            )
        payload = row[7]
        if type(payload) is not str:
            raise SQLiteV3MigrationPersistenceError(
                "SQLite v3 migration payload must be text"
            )
        try:
            bundle = loads_snapshot_v3_migration_bundle(payload)
        except V3MigrationBundleError as error:
            raise SQLiteV3MigrationPersistenceError(
                "SQLite v3 migration payload failed bundle validation"
            ) from error
        if row != SQLiteV3MigrationRepository._expected_row(bundle, payload):
            raise SQLiteV3MigrationPersistenceError(
                "SQLite v3 migration columns do not match bundle payload"
            )
        return bundle

    @_synchronized
    def stage(
        self,
        bundle: SnapshotV3MigrationBundle,
        *,
        commit_relation_verifier: CommitRelationVerifier | None = None,
    ) -> SQLiteV3MigrationStageResult:
        self._require_open()
        if type(bundle) is not SnapshotV3MigrationBundle:
            raise ValueError(
                "bundle must be exactly SnapshotV3MigrationBundle"
            )
        verify_snapshot_v3_migration_bundle(
            bundle,
            commit_relation_verifier=commit_relation_verifier,
        )
        payload = dumps_snapshot_v3_migration_bundle(bundle)
        if len(payload.encode("utf-8")) > V3_MIGRATION_BUNDLE_MAX_BYTES:
            raise ValueError("v3 migration bundle exceeds the storage limit")
        expected = self._expected_row(bundle, payload)
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT bundle_id, bundle_version, state, "
                        "source_snapshot_sha256, "
                        "normalized_source_snapshot_sha256, mapping_sha256, "
                        "plan_sha256, payload "
                        "FROM v3_migration_bundles WHERE bundle_id = ?",
                        (bundle.bundle_id,),
                    )
                    stored = cursor.fetchone()
                    inserted = stored is None
                    if inserted:
                        cursor.execute(
                            "INSERT INTO v3_migration_bundles ("
                            "bundle_id, bundle_version, state, "
                            "source_snapshot_sha256, "
                            "normalized_source_snapshot_sha256, "
                            "mapping_sha256, plan_sha256, payload"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            expected,
                        )
                        cursor.execute(
                            "SELECT bundle_id, bundle_version, state, "
                            "source_snapshot_sha256, "
                            "normalized_source_snapshot_sha256, "
                            "mapping_sha256, plan_sha256, payload "
                            "FROM v3_migration_bundles WHERE bundle_id = ?",
                            (bundle.bundle_id,),
                        )
                        stored = cursor.fetchone()
                    if stored != expected:
                        raise SQLiteV3MigrationConflictError(
                            "SQLite v3 migration bundle ID has conflicting "
                            "immutable content"
                        )
                    loaded = self._row_bundle(stored)
                    if loaded != bundle:
                        raise SQLiteV3MigrationConflictError(
                            "SQLite v3 migration staged bundle failed exact "
                            "replay"
                        )
            return SQLiteV3MigrationStageResult(
                bundle_id=bundle.bundle_id,
                state=bundle.state,
                inserted=inserted,
            )
        except (
            SQLiteV3MigrationConflictError,
            SQLiteV3MigrationSchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise SQLiteV3MigrationSchemaError(
                    _MISSING_SCHEMA_MESSAGE
                ) from error
            raise SQLiteV3MigrationPersistenceError(
                "failed to stage v3 migration bundle in SQLite"
            ) from error

    @_synchronized
    def load(self, bundle_id: str) -> SnapshotV3MigrationBundle:
        self._require_open()
        _validate_bundle_id(bundle_id)
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT length(CAST(payload AS BLOB)) "
                        "FROM v3_migration_bundles WHERE bundle_id = ?",
                        (bundle_id,),
                    )
                    size_row = cursor.fetchone()
                    if size_row is None:
                        raise KeyError(bundle_id)
                    if (
                        len(size_row) != 1
                        or type(size_row[0]) is not int
                        or size_row[0] < 0
                        or size_row[0] > V3_MIGRATION_BUNDLE_MAX_BYTES
                    ):
                        raise SQLiteV3MigrationPersistenceError(
                            "SQLite v3 migration payload exceeds the bounded "
                            "load contract"
                        )
                    cursor.execute(
                        "SELECT bundle_id, bundle_version, state, "
                        "source_snapshot_sha256, "
                        "normalized_source_snapshot_sha256, mapping_sha256, "
                        "plan_sha256, payload "
                        "FROM v3_migration_bundles WHERE bundle_id = ?",
                        (bundle_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise SQLiteV3MigrationPersistenceError(
                            "SQLite v3 migration row disappeared during load"
                        )
                    return self._row_bundle(row)
        except (KeyError, SQLiteV3MigrationSchemaError):
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise SQLiteV3MigrationSchemaError(
                    _MISSING_SCHEMA_MESSAGE
                ) from error
            raise SQLiteV3MigrationPersistenceError(
                "failed to load v3 migration bundle from SQLite"
            ) from error

    @_synchronized
    def list_bundle_ids(self, *, limit: int = 1_000) -> tuple[str, ...]:
        self._require_open()
        if type(limit) is not int or limit < 1 or limit > 10_000:
            raise ValueError("limit must be an integer from 1 through 10000")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT bundle_id FROM v3_migration_bundles "
                        "ORDER BY bundle_id LIMIT ?",
                        (limit,),
                    )
                    bundle_ids = tuple(row[0] for row in cursor.fetchall())
                    for bundle_id in bundle_ids:
                        _validate_bundle_id(bundle_id)
                    return bundle_ids
        except SQLiteV3MigrationSchemaError:
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise SQLiteV3MigrationSchemaError(
                    _MISSING_SCHEMA_MESSAGE
                ) from error
            raise SQLiteV3MigrationPersistenceError(
                "failed to list v3 migration bundles from SQLite"
            ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    @_synchronized
    def __enter__(self) -> "SQLiteV3MigrationRepository":
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _validate_bundle_id(value: object) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("bundle_id must use sha256:<64 lowercase hex>")
