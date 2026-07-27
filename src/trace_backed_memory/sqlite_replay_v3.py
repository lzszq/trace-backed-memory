from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .replay_v3 import (
    ARTIFACT_MAX_BYTES,
    INJECTION_ARTIFACT_MAX_BYTES,
    REPLAY_JSON_MAX_BYTES,
    ContentAddressedArtifact,
    DataClassification,
    DecisionReplayManifest,
    InjectionArtifact,
    ReplayContractError,
    StoredReplayArtifact,
    dumps_decision_replay_manifest,
    dumps_injection_artifact,
    loads_decision_replay_manifest,
    loads_injection_artifact,
    verify_artifact_content,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_REPLAY_V3_SCHEMA_VERSION = 1
_MISSING_SCHEMA_MESSAGE = "SQLite replay v3 schema is missing or incomplete"
_ARTIFACT_ID_RE = re.compile(r"artifact_sha256_[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_replay_schema",
    "v3_replay_artifacts",
    "v3_replay_artifacts_immutable_delete",
    "v3_replay_artifacts_immutable_update",
    "v3_replay_injections",
    "v3_replay_injections_decision",
    "v3_replay_injections_immutable_delete",
    "v3_replay_injections_immutable_update",
    "v3_replay_manifests",
    "v3_replay_manifests_decision",
    "v3_replay_manifests_immutable_delete",
    "v3_replay_manifests_immutable_update",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteReplayV3Error(RuntimeError):
    pass


class SQLiteReplayV3SchemaError(SQLiteReplayV3Error):
    pass


class SQLiteReplayV3ConflictError(SQLiteReplayV3Error):
    pass


class SQLiteReplayV3PersistenceError(SQLiteReplayV3Error):
    pass


@dataclass(frozen=True)
class SQLiteReplayV3StoreResult:
    artifact_id: str
    artifact_inserted: bool
    injection_inserted: bool
    manifest_sha256: str | None
    manifest_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
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
            "foreign key mismatch",
        )
    )


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteReplayV3SchemaError(
            "SQLite replay v3 schema contains an invalid definition"
        )
    return "".join(value.split()).casefold()


def _read_schema_definitions(
    cursor: sqlite3.Cursor,
) -> tuple[tuple[str, str, str, str], ...]:
    placeholders = ", ".join("?" for _ in _SCHEMA_OBJECT_NAMES)
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        f"WHERE name IN ({placeholders}) ORDER BY name",
        _SCHEMA_OBJECT_NAMES,
    )
    rows = cursor.fetchall()
    if len(rows) != len(_SCHEMA_OBJECT_NAMES):
        raise SQLiteReplayV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteReplayV3SchemaError(
                "SQLite replay v3 schema definition has an invalid shape"
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
                    "schemas/sqlite-v3-replay.sql"
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
        raise SQLiteReplayV3SchemaError(
            "could not validate the canonical SQLite replay v3 schema"
        ) from error


class SQLiteReplayV3Repository:
    """Opt-in immutable SQLite storage for replay descriptors and bytes."""

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
    ) -> SQLiteReplayV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        "schemas/sqlite-v3-replay.sql"
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
            raise SQLiteReplayV3PersistenceError(
                "failed to connect to SQLite replay v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteReplayV3Error(
                "SQLite replay v3 repository is closed"
            )
        try:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteReplayV3Error(
                "SQLite replay v3 repository is closed"
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
                "failed to close unusable SQLite replay v3 connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_replay_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to roll back SQLite replay v3 savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after replay v3 "
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
                            "failed to release SQLite replay v3 savepoint "
                            f"{savepoint}: {cleanup_error}; retry failed: "
                            f"{retry_error}"
                        )
                        self._rollback_connection_or_close(
                            primary_error,
                            context=(
                                "the outer SQLite transaction after an "
                                "unreleased replay v3 savepoint"
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

        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the top-level SQLite replay v3 transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level SQLite replay v3 transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        foreign_keys = cursor.fetchone()
        if foreign_keys != (1,):
            raise SQLiteReplayV3SchemaError(
                "SQLite replay v3 requires foreign keys to remain enabled"
            )
        cursor.execute(
            "SELECT schema_version "
            "FROM trace_backed_memory_v3_replay_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise SQLiteReplayV3SchemaError(
                "SQLite replay v3 schema metadata must contain exactly "
                "one row"
            )
        version = rows[0][0]
        if version != SQLITE_REPLAY_V3_SCHEMA_VERSION:
            raise SQLiteReplayV3SchemaError(
                "SQLite replay v3 schema version mismatch: expected "
                f"{SQLITE_REPLAY_V3_SCHEMA_VERSION}, found {version}"
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteReplayV3SchemaError(
                "SQLite replay v3 schema definitions do not match the "
                "canonical version"
            )

    @staticmethod
    def _artifact_row(
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> tuple[object, ...]:
        payload = artifact.to_dict()
        return (
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.size_bytes,
            artifact.media_type,
            artifact.classification,
            payload["created_at"],
            artifact.encryption_key_id,
            artifact.redaction_policy_id,
            content,
        )

    @staticmethod
    def _stored_artifact(row: tuple[object, ...]) -> StoredReplayArtifact:
        if len(row) != 9 or type(row[8]) is not bytes:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay artifact row has an invalid shape"
            )
        try:
            artifact = ContentAddressedArtifact(
                artifact_id=cast(str, row[0]),
                content_sha256=cast(str, row[1]),
                size_bytes=cast(int, row[2]),
                media_type=cast(str, row[3]),
                classification=cast(DataClassification, row[4]),
                created_at=cast(str, row[5]),
                encryption_key_id=cast(str | None, row[6]),
                redaction_policy_id=cast(str | None, row[7]),
            )
        except ReplayContractError as error:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay artifact metadata failed validation"
            ) from error
        if artifact.classification not in {"public", "internal"}:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay v3 cannot load sensitive artifact bytes"
            )
        expected = SQLiteReplayV3Repository._artifact_row(
            artifact,
            row[8],
        )
        if row != expected or not verify_artifact_content(artifact, row[8]):
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay artifact columns or bytes do not match"
            )
        return StoredReplayArtifact(artifact=artifact, content=row[8])

    @staticmethod
    def _injection_row(
        injection: InjectionArtifact,
    ) -> tuple[object, ...]:
        descriptor = dumps_injection_artifact(injection)
        return (
            injection.artifact.artifact_id,
            injection.session_id,
            injection.decision_id,
            injection.usage_decision_id,
            descriptor,
        )

    @staticmethod
    def _stored_injection(row: tuple[object, ...]) -> InjectionArtifact:
        if len(row) != 5 or type(row[4]) is not str:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection row has an invalid shape"
            )
        try:
            injection = loads_injection_artifact(row[4])
        except ReplayContractError as error:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection descriptor failed validation"
            ) from error
        if row != SQLiteReplayV3Repository._injection_row(injection):
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection columns do not match descriptor"
            )
        return injection

    @staticmethod
    def _manifest_row(
        manifest: DecisionReplayManifest,
    ) -> tuple[object, ...]:
        descriptor = dumps_decision_replay_manifest(manifest)
        return (
            manifest.manifest_sha256,
            manifest.session_id,
            manifest.decision_id,
            manifest.usage_decision_id,
            manifest.injection_artifact_id,
            manifest.completeness,
            descriptor,
        )

    @staticmethod
    def _stored_manifest(
        row: tuple[object, ...],
    ) -> DecisionReplayManifest:
        if len(row) != 7 or type(row[6]) is not str:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay manifest row has an invalid shape"
            )
        try:
            manifest = loads_decision_replay_manifest(row[6])
        except ReplayContractError as error:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay manifest descriptor failed validation"
            ) from error
        if row != SQLiteReplayV3Repository._manifest_row(manifest):
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay manifest columns do not match descriptor"
            )
        return manifest

    def _put_artifact(
        self,
        cursor: sqlite3.Cursor,
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> bool:
        if type(artifact) is not ContentAddressedArtifact:
            raise ValueError(
                "artifact must be exactly ContentAddressedArtifact"
            )
        if type(content) is not bytes:
            raise ValueError("content must be bytes")
        if artifact.classification not in {"public", "internal"}:
            raise ValueError(
                "SQLite replay v3 requires an encryption provider for "
                "sensitive artifacts"
            )
        if not verify_artifact_content(artifact, content):
            raise ValueError("content does not match artifact")
        expected = self._artifact_row(artifact, content)
        cursor.execute(
            "SELECT artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content FROM v3_replay_artifacts "
            "WHERE artifact_id = ?",
            (artifact.artifact_id,),
        )
        stored = cursor.fetchone()
        inserted = stored is None
        if inserted:
            cursor.execute(
                "INSERT INTO v3_replay_artifacts ("
                "artifact_id, content_sha256, size_bytes, media_type, "
                "classification, created_at, encryption_key_id, "
                "redaction_policy_id, content"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                expected,
            )
            stored = expected
        if stored != expected:
            raise SQLiteReplayV3ConflictError(
                "SQLite replay artifact ID has conflicting immutable content"
            )
        self._stored_artifact(stored)
        return inserted

    def _put_injection(
        self,
        cursor: sqlite3.Cursor,
        injection: InjectionArtifact,
    ) -> bool:
        expected = self._injection_row(injection)
        if len(cast(str, expected[4]).encode("utf-8")) > REPLAY_JSON_MAX_BYTES:
            raise ValueError("injection descriptor exceeds storage limit")
        cursor.execute(
            "SELECT artifact_id, session_id, decision_id, usage_decision_id, "
            "descriptor FROM v3_replay_injections WHERE artifact_id = ?",
            (injection.artifact.artifact_id,),
        )
        stored = cursor.fetchone()
        inserted = stored is None
        if inserted:
            cursor.execute(
                "INSERT INTO v3_replay_injections ("
                "artifact_id, session_id, decision_id, usage_decision_id, "
                "descriptor) VALUES (?, ?, ?, ?, ?)",
                expected,
            )
            stored = expected
        if stored != expected:
            raise SQLiteReplayV3ConflictError(
                "SQLite replay injection ID has conflicting immutable content"
            )
        self._stored_injection(stored)
        return inserted

    def _put_manifest(
        self,
        cursor: sqlite3.Cursor,
        manifest: DecisionReplayManifest,
    ) -> bool:
        expected = self._manifest_row(manifest)
        if len(cast(str, expected[6]).encode("utf-8")) > REPLAY_JSON_MAX_BYTES:
            raise ValueError("replay manifest exceeds storage limit")
        if manifest.injection_artifact_id is not None:
            cursor.execute(
                "SELECT artifact_id, session_id, decision_id, "
                "usage_decision_id, descriptor "
                "FROM v3_replay_injections WHERE artifact_id = ?",
                (manifest.injection_artifact_id,),
            )
            injection_row = cursor.fetchone()
            if injection_row is None:
                raise SQLiteReplayV3ConflictError(
                    "replay manifest references an unknown injection"
                )
            injection = self._stored_injection(injection_row)
            if (
                manifest.session_id != injection.session_id
                or manifest.decision_id != injection.decision_id
                or manifest.usage_decision_id
                != injection.usage_decision_id
            ):
                raise SQLiteReplayV3ConflictError(
                    "replay manifest linkage conflicts with injection"
                )
        cursor.execute(
            "SELECT manifest_sha256, session_id, decision_id, "
            "usage_decision_id, injection_artifact_id, completeness, "
            "descriptor FROM v3_replay_manifests WHERE manifest_sha256 = ?",
            (manifest.manifest_sha256,),
        )
        stored = cursor.fetchone()
        inserted = stored is None
        if inserted:
            cursor.execute(
                "INSERT INTO v3_replay_manifests ("
                "manifest_sha256, session_id, decision_id, "
                "usage_decision_id, injection_artifact_id, completeness, "
                "descriptor) VALUES (?, ?, ?, ?, ?, ?, ?)",
                expected,
            )
            stored = expected
        if stored != expected:
            raise SQLiteReplayV3ConflictError(
                "SQLite replay manifest hash has conflicting immutable content"
            )
        self._stored_manifest(stored)
        return inserted

    @_synchronized
    def store_artifact(
        self,
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> bool:
        self._require_open()
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._put_artifact(cursor, artifact, content)
        except (SQLiteReplayV3ConflictError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "store replay artifact")

    @_synchronized
    def store_injection(
        self,
        injection: InjectionArtifact,
        content: bytes,
    ) -> SQLiteReplayV3StoreResult:
        self._require_open()
        if type(injection) is not InjectionArtifact:
            raise ValueError("injection must be exactly InjectionArtifact")
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    artifact_inserted = self._put_artifact(
                        cursor,
                        injection.artifact,
                        content,
                    )
                    injection_inserted = self._put_injection(
                        cursor,
                        injection,
                    )
            return SQLiteReplayV3StoreResult(
                artifact_id=injection.artifact.artifact_id,
                artifact_inserted=artifact_inserted,
                injection_inserted=injection_inserted,
                manifest_sha256=None,
                manifest_inserted=False,
            )
        except (SQLiteReplayV3ConflictError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "store replay injection")

    @_synchronized
    def store_manifest(self, manifest: DecisionReplayManifest) -> bool:
        self._require_open()
        if type(manifest) is not DecisionReplayManifest:
            raise ValueError(
                "manifest must be exactly DecisionReplayManifest"
            )
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._put_manifest(cursor, manifest)
        except (SQLiteReplayV3ConflictError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "store replay manifest")

    @_synchronized
    def store_bundle(
        self,
        injection: InjectionArtifact,
        content: bytes,
        manifest: DecisionReplayManifest,
    ) -> SQLiteReplayV3StoreResult:
        self._require_open()
        if (
            type(injection) is not InjectionArtifact
            or type(manifest) is not DecisionReplayManifest
        ):
            raise ValueError(
                "injection and manifest must be exact replay records"
            )
        if (
            manifest.injection_artifact_id
            != injection.artifact.artifact_id
            or manifest.session_id != injection.session_id
            or manifest.decision_id != injection.decision_id
            or manifest.usage_decision_id != injection.usage_decision_id
        ):
            raise ValueError(
                "manifest and injection linkage must match exactly"
            )
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    artifact_inserted = self._put_artifact(
                        cursor,
                        injection.artifact,
                        content,
                    )
                    injection_inserted = self._put_injection(
                        cursor,
                        injection,
                    )
                    manifest_inserted = self._put_manifest(cursor, manifest)
            return SQLiteReplayV3StoreResult(
                artifact_id=injection.artifact.artifact_id,
                artifact_inserted=artifact_inserted,
                injection_inserted=injection_inserted,
                manifest_sha256=manifest.manifest_sha256,
                manifest_inserted=manifest_inserted,
            )
        except (SQLiteReplayV3ConflictError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "store replay bundle")

    def _load_artifact(
        self,
        cursor: sqlite3.Cursor,
        artifact_id: str,
    ) -> StoredReplayArtifact:
        cursor.execute(
            "SELECT size_bytes, length(content) FROM v3_replay_artifacts "
            "WHERE artifact_id = ?",
            (artifact_id,),
        )
        sizes = cursor.fetchone()
        if sizes is None:
            raise KeyError(artifact_id)
        if (
            len(sizes) != 2
            or type(sizes[0]) is not int
            or type(sizes[1]) is not int
            or sizes[0] < 0
            or sizes[0] > ARTIFACT_MAX_BYTES
            or sizes[1] != sizes[0]
        ):
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay artifact exceeds bounded load contract"
            )
        cursor.execute(
            "SELECT artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content FROM v3_replay_artifacts "
            "WHERE artifact_id = ?",
            (artifact_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay artifact disappeared during load"
        )
        return self._stored_artifact(row)

    def _load_injection(
        self,
        cursor: sqlite3.Cursor,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]:
        cursor.execute(
            "SELECT length(CAST(descriptor AS BLOB)) "
            "FROM v3_replay_injections WHERE artifact_id = ?",
            (artifact_id,),
        )
        size = cursor.fetchone()
        if size is None:
            raise KeyError(artifact_id)
        if (
            len(size) != 1
            or type(size[0]) is not int
            or size[0] < 1
            or size[0] > REPLAY_JSON_MAX_BYTES
        ):
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection exceeds bounded load contract"
            )
        cursor.execute(
            "SELECT artifact_id, session_id, decision_id, "
            "usage_decision_id, descriptor "
            "FROM v3_replay_injections WHERE artifact_id = ?",
            (artifact_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection disappeared during load"
            )
        injection = self._stored_injection(row)
        stored = self._load_artifact(cursor, artifact_id)
        if injection.artifact != stored.artifact:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection artifact linkage differs"
            )
        if stored.artifact.size_bytes > INJECTION_ARTIFACT_MAX_BYTES:
            raise SQLiteReplayV3PersistenceError(
                "SQLite replay injection bytes exceed bound"
            )
        return injection, stored.content

    @_synchronized
    def load_artifact(self, artifact_id: str) -> StoredReplayArtifact:
        self._require_open()
        _validate_artifact_id(artifact_id)
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._load_artifact(cursor, artifact_id)
        except (KeyError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "load replay artifact")

    @_synchronized
    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]:
        self._require_open()
        _validate_artifact_id(artifact_id)
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._load_injection(cursor, artifact_id)
        except (KeyError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "load replay injection")

    @_synchronized
    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> DecisionReplayManifest:
        self._require_open()
        _validate_digest(manifest_sha256, "manifest_sha256")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT length(CAST(descriptor AS BLOB)) "
                        "FROM v3_replay_manifests WHERE manifest_sha256 = ?",
                        (manifest_sha256,),
                    )
                    size = cursor.fetchone()
                    if size is None:
                        raise KeyError(manifest_sha256)
                    if (
                        len(size) != 1
                        or type(size[0]) is not int
                        or size[0] < 1
                        or size[0] > REPLAY_JSON_MAX_BYTES
                    ):
                        raise SQLiteReplayV3PersistenceError(
                            "SQLite replay manifest exceeds bounded load "
                            "contract"
                        )
                    cursor.execute(
                        "SELECT manifest_sha256, session_id, decision_id, "
                        "usage_decision_id, injection_artifact_id, "
                        "completeness, descriptor "
                        "FROM v3_replay_manifests WHERE manifest_sha256 = ?",
                        (manifest_sha256,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise SQLiteReplayV3PersistenceError(
                            "SQLite replay manifest disappeared during load"
                        )
                    manifest = self._stored_manifest(row)
                    if manifest.injection_artifact_id is not None:
                        try:
                            injection, _ = self._load_injection(
                                cursor,
                                manifest.injection_artifact_id,
                            )
                        except KeyError as error:
                            raise SQLiteReplayV3PersistenceError(
                                "SQLite replay manifest references an unknown "
                                "injection"
                            ) from error
                        if (
                            manifest.session_id != injection.session_id
                            or manifest.decision_id != injection.decision_id
                            or manifest.usage_decision_id
                            != injection.usage_decision_id
                        ):
                            raise SQLiteReplayV3PersistenceError(
                                "SQLite replay manifest linkage differs from "
                                "injection"
                            )
                    return manifest
        except (KeyError, SQLiteReplayV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "load replay manifest")

    def _raise_database_error(
        self,
        error: sqlite3.DatabaseError,
        action: str,
    ) -> NoReturn:
        if _is_schema_error(error):
            raise SQLiteReplayV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        raise SQLiteReplayV3PersistenceError(
            f"failed to {action} in SQLite"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    @_synchronized
    def __enter__(self) -> SQLiteReplayV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _validate_artifact_id(value: object) -> None:
    if type(value) is not str or _ARTIFACT_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "artifact_id must use artifact_sha256_<64 lowercase hex>"
        )


def _validate_digest(value: object, field_name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
