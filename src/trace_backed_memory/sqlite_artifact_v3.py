from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path
import sqlite3
from threading import RLock
from typing import ParamSpec, TypeVar, cast

from .artifact_v3 import EncryptedArtifactRecord, ArtifactRetention
from .replay_v3 import ContentAddressedArtifact, DataClassification
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_ARTIFACT_V3_SCHEMA_VERSION = 1
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_artifact_authority_schema",
    "v3_encrypted_artifacts",
    "v3_encrypted_artifacts_scope",
    "v3_encrypted_artifacts_immutable_update",
    "v3_encrypted_artifacts_immutable_delete",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteArtifactV3Error(RuntimeError):
    pass


class SQLiteArtifactV3SchemaError(SQLiteArtifactV3Error):
    pass


class SQLiteArtifactV3ConflictError(SQLiteArtifactV3Error):
    pass


class SQLiteArtifactV3NotFoundError(SQLiteArtifactV3Error):
    pass


class SQLiteArtifactV3PersistenceError(SQLiteArtifactV3Error):
    pass


@dataclass(frozen=True)
class SQLiteArtifactV3StoreResult:
    artifact_id: str
    artifact_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with args[0]._lock:
            return method(*args, **kwargs)
    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteArtifactV3SchemaError("SQLite artifact schema is invalid")
    return "".join(value.split()).casefold()


def _read_schema_definitions(cursor: sqlite3.Cursor) -> tuple[tuple[str, str, str, str], ...]:
    placeholders = ", ".join("?" for _ in _SCHEMA_OBJECT_NAMES)
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        f"WHERE name IN ({placeholders}) OR tbl_name = ? ORDER BY name",
        (*_SCHEMA_OBJECT_NAMES, "v3_encrypted_artifacts"),
    )
    rows = tuple(
        row
        for row in cursor.fetchall()
        if not (type(row[1]) is str and row[1].startswith("sqlite_autoindex_"))
    )
    if len(rows) != len(_SCHEMA_OBJECT_NAMES):
        raise SQLiteArtifactV3SchemaError("SQLite artifact schema is missing or incomplete")
    return tuple((cast(str, r[0]), cast(str, r[1]), cast(str, r[2]), _normalized_schema_sql(r[3])) for r in rows)


@lru_cache(maxsize=1)
def _canonical_schema_definitions() -> tuple[tuple[str, str, str, str], ...]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(read_packaged_resource("schemas/sqlite-v3-artifact-authority.sql").decode("utf-8"))
            with closing(connection.cursor()) as cursor:
                return _read_schema_definitions(cursor)
        finally:
            connection.close()
    except (OSError, UnicodeError, sqlite3.Error, PackagedResourceError) as error:
        raise SQLiteArtifactV3SchemaError("could not validate canonical SQLite artifact schema") from error


class SQLiteArtifactV3Repository:
    """Immutable, side-by-side SQLite storage for encrypted artifact envelopes."""

    def __init__(self, connection: sqlite3.Connection, *, owns_connection: bool = False) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a sqlite3.Connection")
        self._connection = connection
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False
        self._savepoint_number = 0

    @classmethod
    def connect(cls, database: str | bytes | Path = ":memory:", *, initialize: bool = False, **kwargs: object) -> SQLiteArtifactV3Repository:
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            if initialize:
                connection.executescript(read_packaged_resource("schemas/sqlite-v3-artifact-authority.sql").decode("utf-8"))
        except (OSError, UnicodeError, sqlite3.Error, PackagedResourceError, TypeError, ValueError) as error:
            if "connection" in locals():
                connection.close()
            raise SQLiteArtifactV3PersistenceError("failed to connect to SQLite artifact storage") from error
        return cls(connection, owns_connection=True)

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            name = f"tbm_sqlite_artifact_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {name}")
            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self._connection.execute(f"RELEASE SAVEPOINT {name}")
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        f"failed to clean up SQLite artifact savepoint {name}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context="outer transaction after artifact savepoint cleanup failed",
                    )

            try:
                yield
            except BaseException as error:
                rollback_savepoint(error)
                raise
            else:
                try:
                    self._connection.execute(f"RELEASE SAVEPOINT {name}")
                except BaseException as error:
                    rollback_savepoint(error)
                    raise
            return
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error, context="top-level SQLite artifact transaction"
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error, context="top-level SQLite artifact transaction"
                )
                raise

    def _rollback_connection_or_close(
        self, primary_error: BaseException, *, context: str
    ) -> None:
        for attempt in range(2):
            if not self._connection.in_transaction:
                return
            try:
                self._connection.rollback()
            except BaseException as rollback_error:
                primary_error.add_note(
                    f"rollback attempt {attempt + 1} failed for {context}: {rollback_error}"
                )
                continue
            if not self._connection.in_transaction:
                return
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(f"failed to close unusable artifact connection: {close_error}")

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteArtifactV3SchemaError("SQLite artifact storage requires foreign keys")
        cursor.execute("SELECT schema_version FROM trace_backed_memory_v3_artifact_authority_schema WHERE singleton = 1")
        if cursor.fetchall() != [(SQLITE_ARTIFACT_V3_SCHEMA_VERSION,)]:
            raise SQLiteArtifactV3SchemaError("SQLite artifact schema version mismatch")
        if _read_schema_definitions(cursor) != _canonical_schema_definitions():
            raise SQLiteArtifactV3SchemaError("SQLite artifact schema definitions do not match canonical version")

    @staticmethod
    def _row(record: EncryptedArtifactRecord) -> tuple[object, ...]:
        a = record.artifact
        return (
            a.artifact_id, a.content_sha256, a.size_bytes, a.media_type,
            a.classification, a.created_at, a.redaction_policy_id,
            record.tenant_id, record.repository_id, record.environment_id,
            record.write_authorization_event_id, record.encryption_provider_id,
            record.encryption_algorithm, record.encryption_key_id, record.nonce,
            record.ciphertext, record.ciphertext_sha256,
            record.retention.retain_until, int(record.retention.legal_hold), record.stored_at,
        )

    @staticmethod
    def _record(row: tuple[object, ...]) -> EncryptedArtifactRecord:
        if len(row) != 20 or type(row[14]) is not bytes or type(row[15]) is not bytes:
            raise SQLiteArtifactV3PersistenceError("SQLite artifact row has invalid shape")
        try:
            artifact = ContentAddressedArtifact(
                artifact_id=cast(str, row[0]), content_sha256=cast(str, row[1]),
                size_bytes=cast(int, row[2]), media_type=cast(str, row[3]),
                classification=cast(DataClassification, row[4]), created_at=cast(str, row[5]),
                encryption_key_id=cast(str, row[13]), redaction_policy_id=cast(str | None, row[6]),
            )
            record = EncryptedArtifactRecord(
                artifact=artifact, tenant_id=cast(str, row[7]), repository_id=cast(str, row[8]),
                environment_id=cast(str, row[9]), write_authorization_event_id=cast(str, row[10]),
                encryption_provider_id=cast(str, row[11]), encryption_algorithm=cast(str, row[12]),
                encryption_key_id=cast(str, row[13]), nonce=row[14], ciphertext=row[15],
                ciphertext_sha256=cast(str, row[16]),
                retention=ArtifactRetention(retain_until=cast(str | None, row[17]), legal_hold=row[18] == 1),
                stored_at=cast(str, row[19]),
            )
        except (TypeError, ValueError) as error:
            raise SQLiteArtifactV3PersistenceError("SQLite artifact record failed validation") from error
        if row != SQLiteArtifactV3Repository._row(record):
            raise SQLiteArtifactV3PersistenceError("SQLite artifact columns do not match")
        return record

    @_synchronized
    def put(self, record: EncryptedArtifactRecord) -> SQLiteArtifactV3StoreResult:
        if type(record) is not EncryptedArtifactRecord:
            raise ValueError("record must be exactly EncryptedArtifactRecord")
        try:
            with self._transaction(write=True), closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                expected = self._row(record)
                cursor.execute("SELECT * FROM v3_encrypted_artifacts WHERE artifact_id = ?", (record.artifact.artifact_id,))
                stored = cursor.fetchone()
                inserted = stored is None
                if inserted:
                    cursor.execute("INSERT INTO v3_encrypted_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", expected)
                    stored = expected
                if stored != expected:
                    raise SQLiteArtifactV3ConflictError("artifact ID has conflicting immutable content")
                self._record(stored)
                return SQLiteArtifactV3StoreResult(record.artifact.artifact_id, inserted)
        except (SQLiteArtifactV3Error, ValueError):
            raise
        except sqlite3.Error as error:
            raise SQLiteArtifactV3PersistenceError("failed to store encrypted artifact") from error

    @_synchronized
    def load(self, artifact_id: str) -> EncryptedArtifactRecord:
        try:
            with self._transaction(write=False), closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                cursor.execute("SELECT * FROM v3_encrypted_artifacts WHERE artifact_id = ?", (artifact_id,))
                row = cursor.fetchone()
                if row is None:
                    raise SQLiteArtifactV3NotFoundError("encrypted artifact was not found")
                return self._record(row)
        except SQLiteArtifactV3Error:
            raise
        except sqlite3.Error as error:
            raise SQLiteArtifactV3PersistenceError("failed to load encrypted artifact") from error

    @_synchronized
    def find(self, artifact_id: str) -> EncryptedArtifactRecord | None:
        try:
            with self._transaction(write=False), closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                cursor.execute("SELECT * FROM v3_encrypted_artifacts WHERE artifact_id = ?", (artifact_id,))
                row = cursor.fetchone()
                return None if row is None else self._record(row)
        except SQLiteArtifactV3Error:
            raise
        except sqlite3.Error as error:
            raise SQLiteArtifactV3PersistenceError("failed to find encrypted artifact") from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteArtifactV3Repository:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
