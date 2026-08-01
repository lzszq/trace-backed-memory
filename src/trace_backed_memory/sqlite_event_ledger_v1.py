from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from functools import lru_cache, wraps
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from threading import RLock
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

from .event_v1 import (
    EVENT_JSON_MAX_BYTES,
    CanonicalEvent,
    EventArtifactRef,
    EventV1ContractError,
    dumps_canonical_event,
    loads_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EventLedgerClassificationDeniedError,
    EventLedgerConflictError,
    EventLedgerIdempotencyConflictError,
    EventLedgerInvalidRequestError,
    EventLedgerPortError,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerGlobalReadRequest,
    LedgerIdempotency,
    LedgerPage,
    LedgerStreamReadRequest,
    LedgerStreamVerification,
    LedgerSubscriptionPage,
    LedgerSubscriptionRequest,
    LedgerTenantPartition,
    build_ledger_append_receipt,
    build_ledger_page,
    verify_ledger_append_precondition,
    verify_ledger_append_receipt,
    verify_ledger_global_page,
    verify_ledger_stream_page,
    verify_ledger_stream_verification,
)
from .locking import snapshot_write_lock
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_EVENT_LEDGER_V1_SCHEMA_VERSION = 1
SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE = (
    "schemas/sqlite-v3-event-ledger.sql"
)
_MISSING_SCHEMA_MESSAGE = (
    "SQLite event ledger schema is missing or incomplete"
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_event_ledger_schema",
    "v3_event_ledger_artifacts",
    "v3_event_ledger_artifacts_immutable_delete",
    "v3_event_ledger_artifacts_immutable_update",
    "v3_event_ledger_artifacts_validate_insert",
    "v3_event_ledger_checkpoints",
    "v3_event_ledger_checkpoints_immutable_delete",
    "v3_event_ledger_checkpoints_immutable_update",
    "v3_event_ledger_events",
    "v3_event_ledger_events_immutable_delete",
    "v3_event_ledger_events_immutable_update",
    "v3_event_ledger_events_partition_global",
    "v3_event_ledger_events_partition_stream",
    "v3_event_ledger_events_validate_insert",
    "v3_event_ledger_global_head",
    "v3_event_ledger_global_head_advance",
    "v3_event_ledger_global_head_no_delete",
    "v3_event_ledger_global_head_validate_insert",
    "v3_event_ledger_idempotency",
    "v3_event_ledger_idempotency_immutable_delete",
    "v3_event_ledger_idempotency_immutable_update",
    "v3_event_ledger_schema_immutable_delete",
    "v3_event_ledger_schema_immutable_update",
    "v3_event_ledger_stream_heads",
    "v3_event_ledger_stream_heads_advance",
    "v3_event_ledger_stream_heads_identity_immutable",
    "v3_event_ledger_stream_heads_no_delete",
    "v3_event_ledger_stream_heads_validate_insert",
)
_VERIFICATION_ISSUE_ORDER = (
    "EVENT_HASH_MISMATCH",
    "GLOBAL_POSITION_INVALID",
    "HASH_CHAIN_MISMATCH",
    "HEAD_MISMATCH",
    "PARTITION_MISMATCH",
    "STREAM_ID_MISMATCH",
    "STREAM_VERSION_GAP",
    "TRUSTED_CONTEXT_MISMATCH",
)
_ZERO_DIGEST = "sha256:" + "0" * 64
_CHECKPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_P = ParamSpec("_P")
_R = TypeVar("_R")
_CONNECTION_LOCKS_GUARD = RLock()
_CONNECTION_LOCKS: dict[sqlite3.Connection, tuple[Any, int]] = {}


class SQLiteEventLedgerV1Error(EventLedgerPortError):
    """Stable base failure for the SQLite canonical event ledger."""


class SQLiteEventLedgerV1SchemaError(SQLiteEventLedgerV1Error):
    """The installed SQLite ledger catalog is absent or has drifted."""


class SQLiteEventLedgerV1PersistenceError(SQLiteEventLedgerV1Error):
    """SQLite could not complete one atomic ledger operation."""


class SQLiteEventLedgerV1IntegrityError(SQLiteEventLedgerV1Error):
    """Retained ledger bytes do not reconstruct their immutable descriptor."""


def _schema_error(message: str) -> SQLiteEventLedgerV1SchemaError:
    return SQLiteEventLedgerV1SchemaError(
        "TBM_EVENT_LEDGER_SQLITE_SCHEMA",
        message,
    )


def _persistence_error(message: str) -> SQLiteEventLedgerV1PersistenceError:
    return SQLiteEventLedgerV1PersistenceError(
        "TBM_EVENT_LEDGER_SQLITE_PERSISTENCE",
        message,
    )


def _integrity_error(message: str) -> SQLiteEventLedgerV1IntegrityError:
    return SQLiteEventLedgerV1IntegrityError(
        "TBM_EVENT_LEDGER_SQLITE_INTEGRITY",
        message,
    )


def _acquire_connection_lock(connection: sqlite3.Connection) -> Any:
    with _CONNECTION_LOCKS_GUARD:
        retained = _CONNECTION_LOCKS.get(connection)
        if retained is None:
            lock = RLock()
            _CONNECTION_LOCKS[connection] = (lock, 1)
            return lock
        lock, references = retained
        _CONNECTION_LOCKS[connection] = (lock, references + 1)
        return lock


def _release_connection_lock(connection: sqlite3.Connection) -> None:
    with _CONNECTION_LOCKS_GUARD:
        lock, references = _CONNECTION_LOCKS[connection]
        if references == 1:
            del _CONNECTION_LOCKS[connection]
        else:
            _CONNECTION_LOCKS[connection] = (lock, references - 1)


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
        raise _schema_error("SQLite event ledger schema definition is invalid")
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
        raise _schema_error(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise _schema_error(
                "SQLite event ledger schema definition has an invalid shape"
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    return tuple(definitions)


@lru_cache(maxsize=1)
def _canonical_schema_definitions() -> tuple[
    tuple[str, str, str, str], ...
]:
    try:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        try:
            connection.executescript(
                read_packaged_resource(
                    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE
                ).decode("utf-8")
            )
            with closing(connection.cursor()) as cursor:
                return _read_schema_definitions(cursor)
        finally:
            connection.close()
    except SQLiteEventLedgerV1Error:
        raise
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        raise _schema_error(
            "could not validate the canonical SQLite event ledger schema"
        ) from error


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EventLedgerInvalidRequestError(
            "TBM_EVENT_LEDGER_NON_CANONICAL_JSON",
            "ledger value is not canonical JSON",
        ) from error


def _artifact_descriptor(artifact: EventArtifactRef) -> str:
    return _canonical_json(artifact.to_dict())


def _event_row(event: CanonicalEvent, partition_sha256: str) -> tuple[object, ...]:
    descriptor = dumps_canonical_event(event)
    if len(descriptor.encode("utf-8")) > EVENT_JSON_MAX_BYTES:
        raise EventLedgerInvalidRequestError(
            "TBM_EVENT_LEDGER_REQUEST_INVALID",
            "canonical event exceeds the storage byte limit",
        )
    return (
        event.event_id,
        event.event_sha256,
        partition_sha256,
        event.organization_id,
        event.tenant_id,
        event.repository_id,
        event.environment_id,
        event.stream_id,
        event.stream_version,
        event.global_position,
        event.previous_stream_event_sha256,
        event.classification,
        len(event.artifact_refs),
        descriptor,
    )


def _artifact_row(
    event: CanonicalEvent,
    ordinal: int,
) -> tuple[object, ...]:
    artifact = event.artifact_refs[ordinal]
    return (
        event.event_id,
        ordinal,
        artifact.artifact_id,
        artifact.content_sha256,
        artifact.media_type,
        artifact.size_bytes,
        artifact.classification,
        artifact.retention_policy_id,
        artifact.encryption_key_id,
        artifact.availability,
        _artifact_descriptor(artifact),
    )


def _valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


class SQLiteEventLedgerV1:
    """Access-bound canonical SQLite event ledger with exact replay."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        access_context: LedgerAccessContext,
        *,
        owns_connection: bool = False,
        require_wal: bool = False,
        database_path: Path | None = None,
        file_lock: AbstractContextManager[None] | None = None,
    ) -> None:
        if type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be exactly sqlite3.Connection")
        if type(access_context) is not LedgerAccessContext:
            raise ValueError("access_context must be exactly LedgerAccessContext")
        if type(owns_connection) is not bool or type(require_wal) is not bool:
            raise ValueError("SQLite ledger ownership flags must be booleans")
        self._connection = connection
        self._access_context = access_context
        self._owns_connection = owns_connection
        self._require_wal = require_wal
        self._database_path = database_path
        self._file_lock = file_lock
        self._lock = _acquire_connection_lock(connection)
        self._lock_retained = True
        self._closed = False
        self._savepoint_number = 0
        try:
            if not connection.in_transaction:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA recursive_triggers = ON")
        except sqlite3.Error as error:
            _release_connection_lock(connection)
            self._lock_retained = False
            raise _persistence_error(
                "failed to configure SQLite event ledger connection"
            ) from error

    @classmethod
    def connect(
        cls,
        database: str | Path,
        access_context: LedgerAccessContext,
        *,
        initialize: bool = False,
        timeout_seconds: int | float = 30.0,
    ) -> SQLiteEventLedgerV1:
        if type(access_context) is not LedgerAccessContext:
            raise ValueError("access_context must be exactly LedgerAccessContext")
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        if type(database) is not str and not isinstance(database, Path):
            raise ValueError("database must be a string or Path")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be non-negative")

        is_memory = database == ":memory:"
        database_path: Path | None = None
        lock_context: AbstractContextManager[None] | None = None
        connection: sqlite3.Connection | None = None
        try:
            if not is_memory:
                database_path = Path(database).expanduser().resolve(strict=False)
                database_path.parent.mkdir(parents=True, exist_ok=True)
                lock_context = snapshot_write_lock(
                    database_path,
                    timeout_seconds=timeout_seconds,
                )
                lock_context.__enter__()
            connection = sqlite3.connect(
                ":memory:" if is_memory else database_path,
                timeout=float(timeout_seconds),
                isolation_level=None,
                check_same_thread=False,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            connection.execute("PRAGMA synchronous = FULL")
            require_wal = not is_memory
            if require_wal:
                mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if mode is None or str(mode[0]).casefold() != "wal":
                    raise _schema_error(
                        "SQLite event ledger requires WAL journal mode"
                    )
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE
                    ).decode("utf-8")
                )
            repository = cls(
                connection,
                access_context,
                owns_connection=True,
                require_wal=require_wal,
                database_path=database_path,
                file_lock=lock_context,
            )
            connection = None
            lock_context = None
            return repository
        except SQLiteEventLedgerV1Error:
            raise
        except (
            OSError,
            UnicodeError,
            sqlite3.Error,
            PackagedResourceError,
            TypeError,
            ValueError,
        ) as error:
            raise _persistence_error(
                "failed to connect to SQLite event ledger storage"
            ) from error
        finally:
            if connection is not None:
                connection.close()
            if lock_context is not None:
                lock_context.__exit__(None, None, None)

    @property
    def access_context(self) -> LedgerAccessContext:
        return self._access_context

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteEventLedgerV1Error(
                "TBM_EVENT_LEDGER_SQLITE_CLOSED",
                "SQLite event ledger is closed",
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteEventLedgerV1Error(
                "TBM_EVENT_LEDGER_SQLITE_CLOSED",
                "SQLite event ledger is closed",
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
                prefix = "failed to roll back" if attempt == 0 else "rollback retry failed"
                primary_error.add_note(f"{prefix} {context}: {rollback_error}")
                continue
            if not self._connection.in_transaction:
                return
            primary_error.add_note(f"rollback left {context} active")
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite event ledger connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_event_ledger_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to clean up SQLite event ledger savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context="the outer SQLite event ledger transaction",
                    )

            try:
                yield
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
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the top-level SQLite event ledger transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level SQLite event ledger transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise _schema_error(
                "SQLite event ledger requires foreign keys to remain enabled"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise _schema_error(
                "SQLite event ledger requires recursive triggers to remain enabled"
            )
        if self._require_wal:
            cursor.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()
            if mode is None or str(mode[0]).casefold() != "wal":
                raise _schema_error(
                    "SQLite event ledger requires WAL journal mode"
                )
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_event_ledger_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [(1, "tbm.event-ledger-port.v1")]:
            raise _schema_error("SQLite event ledger schema metadata mismatch")
        if _read_schema_definitions(cursor) != _canonical_schema_definitions():
            raise _schema_error("SQLite event ledger schema catalog mismatch")

    @staticmethod
    def _event_select() -> str:
        return (
            "event_id, event_sha256, partition_sha256, organization_id, "
            "tenant_id, repository_id, environment_id, stream_id, "
            "stream_version, global_position, previous_stream_event_sha256, "
            "classification, artifact_ref_count, canonical_event"
        )

    def _event_from_row(
        self,
        cursor: sqlite3.Cursor,
        row: tuple[object, ...],
    ) -> CanonicalEvent:
        if len(row) != 14 or type(row[13]) is not str:
            raise _integrity_error("stored event row has an invalid shape")
        try:
            event = loads_canonical_event(row[13])
        except EventV1ContractError as error:
            raise _integrity_error(
                "stored canonical event bytes failed validation"
            ) from error
        expected = _event_row(event, str(row[2]))
        if tuple(row) != expected:
            raise _integrity_error(
                "stored event columns do not match canonical event bytes"
            )
        cursor.execute(
            "SELECT event_id, ordinal, artifact_id, content_sha256, "
            "media_type, size_bytes, classification, retention_policy_id, "
            "encryption_key_id, availability, descriptor "
            "FROM v3_event_ledger_artifacts WHERE event_id = ? "
            "ORDER BY ordinal",
            (event.event_id,),
        )
        artifact_rows = cursor.fetchall()
        expected_artifacts = tuple(
            _artifact_row(event, ordinal)
            for ordinal in range(len(event.artifact_refs))
        )
        if tuple(artifact_rows) != expected_artifacts:
            raise _integrity_error(
                "stored artifact descriptors do not match canonical event"
            )
        return event

    def _select_event_by_sha256(
        self,
        cursor: sqlite3.Cursor,
        event_sha256: str,
    ) -> CanonicalEvent | None:
        cursor.execute(
            f"SELECT {self._event_select()} FROM v3_event_ledger_events "
            "WHERE event_sha256 = ?",
            (event_sha256,),
        )
        row = cursor.fetchone()
        return None if row is None else self._event_from_row(cursor, row)

    def _select_head_event(
        self,
        cursor: sqlite3.Cursor,
        stream_id: str,
    ) -> CanonicalEvent | None:
        partition = self._access_context.partition
        cursor.execute(
            "SELECT organization_id, tenant_id, repository_id, environment_id, "
            "current_stream_version, current_event_id, current_event_sha256 "
            "FROM v3_event_ledger_stream_heads "
            "WHERE partition_sha256 = ? AND stream_id = ?",
            (partition.partition_sha256, stream_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if tuple(row[:4]) != (
            partition.organization_id,
            partition.tenant_id,
            partition.repository_id,
            partition.environment_id,
        ):
            raise _integrity_error("stream head tenant partition is inconsistent")
        if row[4] == 0 and row[5] is None and row[6] is None:
            return None
        if type(row[4]) is not int or type(row[6]) is not str:
            raise _integrity_error("stream head has an invalid shape")
        event = self._select_event_by_sha256(cursor, row[6])
        if (
            event is None
            or event.event_id != row[5]
            or event.stream_id != stream_id
            or event.stream_version != row[4]
        ):
            raise _integrity_error(
                "stream head does not match its current canonical event"
            )
        return event

    @staticmethod
    def _select_global_position(cursor: sqlite3.Cursor) -> int:
        cursor.execute(
            "SELECT current_global_position FROM v3_event_ledger_global_head "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
            raise _integrity_error("global ledger head is invalid")
        return rows[0][0]

    def _select_idempotency(
        self,
        cursor: sqlite3.Cursor,
        idempotency_key_sha256: str,
    ) -> tuple[object, ...] | None:
        cursor.execute(
            "SELECT command_sha256, request_sha256, stream_id, "
            "previous_stream_version, current_stream_version, "
            "first_global_position, last_global_position, "
            "event_sha256s_json, receipt_sha256 "
            "FROM v3_event_ledger_idempotency "
            "WHERE partition_sha256 = ? AND idempotency_key_sha256 = ?",
            (
                self._access_context.partition.partition_sha256,
                idempotency_key_sha256,
            ),
        )
        return cursor.fetchone()

    def _receipt_from_idempotency_row(
        self,
        cursor: sqlite3.Cursor,
        idempotency_key_sha256: str,
        row: tuple[object, ...],
    ) -> LedgerAppendReceipt:
        if len(row) != 9 or type(row[7]) is not str:
            raise _integrity_error("stored idempotency row has an invalid shape")
        try:
            event_sha256s = json.loads(row[7])
        except (TypeError, json.JSONDecodeError) as error:
            raise _integrity_error(
                "stored idempotency event list is invalid"
            ) from error
        if (
            type(event_sha256s) is not list
            or not event_sha256s
            or len(event_sha256s) > 100
            or any(not _valid_digest(item) for item in event_sha256s)
            or _canonical_json(event_sha256s) != row[7]
        ):
            raise _integrity_error(
                "stored idempotency event list is noncanonical"
            )
        events: list[CanonicalEvent] = []
        for event_sha256 in event_sha256s:
            event = self._select_event_by_sha256(cursor, event_sha256)
            if event is None:
                raise _integrity_error(
                    "stored idempotency record references a missing event"
                )
            events.append(event)
        try:
            return LedgerAppendReceipt(
                request_sha256=row[1],
                idempotency_key_sha256=idempotency_key_sha256,
                command_sha256=row[0],
                stream_id=row[2],
                previous_stream_version=row[3],
                current_stream_version=row[4],
                first_global_position=row[5],
                last_global_position=row[6],
                events=tuple(events),
                outcome="committed",
                receipt_sha256=row[8],
            )
        except EventLedgerPortError as error:
            raise _integrity_error(
                "stored idempotency receipt failed contract validation"
            ) from error

    def _insert_event(
        self,
        cursor: sqlite3.Cursor,
        event: CanonicalEvent,
        partition_sha256: str,
    ) -> None:
        cursor.execute(
            "INSERT INTO v3_event_ledger_events ("
            "event_id, event_sha256, partition_sha256, organization_id, "
            "tenant_id, repository_id, environment_id, stream_id, "
            "stream_version, global_position, previous_stream_event_sha256, "
            "classification, artifact_ref_count, canonical_event"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _event_row(event, partition_sha256),
        )
        for ordinal in range(len(event.artifact_refs)):
            cursor.execute(
                "INSERT INTO v3_event_ledger_artifacts ("
                "event_id, ordinal, artifact_id, content_sha256, media_type, "
                "size_bytes, classification, retention_policy_id, "
                "encryption_key_id, availability, descriptor"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _artifact_row(event, ordinal),
            )
        cursor.execute(
            "UPDATE v3_event_ledger_stream_heads "
            "SET current_stream_version = ?, current_event_id = ?, "
            "current_event_sha256 = ? "
            "WHERE partition_sha256 = ? AND stream_id = ? "
            "AND current_stream_version = ? "
            "AND current_event_sha256 IS ?",
            (
                event.stream_version,
                event.event_id,
                event.event_sha256,
                partition_sha256,
                event.stream_id,
                event.stream_version - 1,
                event.previous_stream_event_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_STALE_STREAM_VERSION",
                "stream head changed during append",
            )
        cursor.execute(
            "UPDATE v3_event_ledger_global_head "
            "SET current_global_position = ?, current_event_id = ?, "
            "current_event_sha256 = ? "
            "WHERE singleton = 1 AND current_global_position = ?",
            (
                event.global_position,
                event.event_id,
                event.event_sha256,
                event.global_position - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT",
                "global ledger head changed during append",
            )

    @_synchronized
    def append(
        self,
        stream_id: str,
        expected_version: int,
        events: tuple[CanonicalEvent, ...],
        idempotency: LedgerIdempotency,
    ) -> LedgerAppendReceipt:
        self._require_open()
        request = LedgerAppendRequest(
            access=self._access_context,
            stream_id=stream_id,
            expected_stream_version=expected_version,
            events=events,
            idempotency=idempotency,
        )
        try:
            with self._transaction(write=True), closing(
                self._connection.cursor()
            ) as cursor:
                self._require_schema(cursor)
                retained_row = self._select_idempotency(
                    cursor,
                    idempotency.idempotency_key_sha256,
                )
                if retained_row is not None:
                    if (
                        retained_row[0] != idempotency.command_sha256
                        or retained_row[1] != request.request_sha256
                    ):
                        raise EventLedgerIdempotencyConflictError(
                            "TBM_EVENT_LEDGER_IDEMPOTENCY_CONFLICT",
                            "idempotency key is bound to another command",
                        )
                    retained = self._receipt_from_idempotency_row(
                        cursor,
                        idempotency.idempotency_key_sha256,
                        retained_row,
                    )
                    verify_ledger_append_receipt(request, retained)
                    return retained

                current_head = self._select_head_event(cursor, stream_id)
                next_global_position = self._select_global_position(cursor) + 1
                verify_ledger_append_precondition(
                    request,
                    current_head=current_head,
                    next_global_position=next_global_position,
                )
                partition = self._access_context.partition
                if current_head is None:
                    cursor.execute(
                        "INSERT INTO v3_event_ledger_stream_heads ("
                        "partition_sha256, stream_id, organization_id, tenant_id, "
                        "repository_id, environment_id, current_stream_version, "
                        "current_event_id, current_event_sha256"
                        ") VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL)",
                        (
                            partition.partition_sha256,
                            stream_id,
                            partition.organization_id,
                            partition.tenant_id,
                            partition.repository_id,
                            partition.environment_id,
                        ),
                    )
                for event in events:
                    self._insert_event(
                        cursor,
                        event,
                        partition.partition_sha256,
                    )

                receipt = build_ledger_append_receipt(request)
                event_sha256s_json = _canonical_json(
                    [event.event_sha256 for event in events]
                )
                cursor.execute(
                    "INSERT INTO v3_event_ledger_idempotency ("
                    "partition_sha256, idempotency_key_sha256, command_sha256, "
                    "request_sha256, stream_id, previous_stream_version, "
                    "current_stream_version, first_global_position, "
                    "last_global_position, event_sha256s_json, receipt_sha256"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        partition.partition_sha256,
                        idempotency.idempotency_key_sha256,
                        idempotency.command_sha256,
                        request.request_sha256,
                        stream_id,
                        receipt.previous_stream_version,
                        receipt.current_stream_version,
                        receipt.first_global_position,
                        receipt.last_global_position,
                        event_sha256s_json,
                        receipt.receipt_sha256,
                    ),
                )
                retained_row = self._select_idempotency(
                    cursor,
                    idempotency.idempotency_key_sha256,
                )
                if retained_row is None:
                    raise _integrity_error(
                        "committed idempotency receipt could not be read back"
                    )
                retained = self._receipt_from_idempotency_row(
                    cursor,
                    idempotency.idempotency_key_sha256,
                    retained_row,
                )
                verify_ledger_append_receipt(request, retained)
                return retained
        except EventLedgerPortError:
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
            raise _persistence_error(
                "SQLite event ledger append failed atomically"
            ) from error

    def _events_from_query(
        self,
        cursor: sqlite3.Cursor,
        query: str,
        parameters: tuple[object, ...],
    ) -> tuple[CanonicalEvent, ...]:
        cursor.execute(query, parameters)
        return tuple(
            self._event_from_row(cursor, row) for row in cursor.fetchall()
        )

    @_synchronized
    def read_stream(
        self,
        stream_id: str,
        from_version: int = 1,
        limit: int = 100,
    ) -> LedgerPage:
        self._require_open()
        request = LedgerStreamReadRequest(
            self._access_context,
            stream_id,
            from_version,
            limit,
        )
        try:
            with self._transaction(write=False), closing(
                self._connection.cursor()
            ) as cursor:
                self._require_schema(cursor)
                events = self._events_from_query(
                    cursor,
                    f"SELECT {self._event_select()} "
                    "FROM v3_event_ledger_events "
                    "WHERE partition_sha256 = ? AND stream_id = ? "
                    "AND stream_version >= ? ORDER BY stream_version LIMIT ?",
                    (
                        self._access_context.partition.partition_sha256,
                        stream_id,
                        from_version,
                        limit + 1,
                    ),
                )
                has_more = len(events) > limit
                selected = events[:limit]
                page = build_ledger_page(
                    read_kind="stream",
                    events=selected,
                    high_watermark_global_position=self._select_global_position(
                        cursor
                    ),
                    next_stream_version=(
                        selected[-1].stream_version + 1
                        if has_more and selected
                        else None
                    ),
                    next_global_position=None,
                    has_more=has_more,
                )
                verify_ledger_stream_page(request, page)
                return page
        except EventLedgerPortError:
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
            raise _persistence_error("SQLite stream read failed") from error

    @_synchronized
    def read_global(
        self,
        after_position: int = 0,
        limit: int = 100,
    ) -> LedgerPage:
        self._require_open()
        request = LedgerGlobalReadRequest(
            self._access_context,
            after_position,
            limit,
        )
        allowed = self._access_context.classification_filter.allowed
        placeholders = ", ".join("?" for _ in allowed)
        try:
            with self._transaction(write=False), closing(
                self._connection.cursor()
            ) as cursor:
                self._require_schema(cursor)
                events = self._events_from_query(
                    cursor,
                    f"SELECT {self._event_select()} "
                    "FROM v3_event_ledger_events "
                    "WHERE partition_sha256 = ? AND global_position > ? "
                    f"AND classification IN ({placeholders}) "
                    "ORDER BY global_position LIMIT ?",
                    (
                        self._access_context.partition.partition_sha256,
                        after_position,
                        *allowed,
                        limit + 1,
                    ),
                )
                has_more = len(events) > limit
                selected = events[:limit]
                page = build_ledger_page(
                    read_kind="global",
                    events=selected,
                    high_watermark_global_position=self._select_global_position(
                        cursor
                    ),
                    next_stream_version=None,
                    next_global_position=(
                        selected[-1].global_position
                        if has_more and selected
                        else None
                    ),
                    has_more=has_more,
                )
                verify_ledger_global_page(request, page)
                return page
        except EventLedgerPortError:
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
            raise _persistence_error("SQLite global read failed") from error

    def _verification_result(
        self,
        cursor: sqlite3.Cursor,
        stream_id: str,
    ) -> LedgerStreamVerification:
        partition_sha256 = self._access_context.partition.partition_sha256
        cursor.execute(
            f"SELECT {self._event_select()} FROM v3_event_ledger_events "
            "WHERE partition_sha256 = ? AND stream_id = ? "
            "ORDER BY stream_version",
            (partition_sha256, stream_id),
        )
        rows = cursor.fetchall()
        cursor.execute(
            "SELECT current_stream_version, current_event_id, "
            "current_event_sha256 FROM v3_event_ledger_stream_heads "
            "WHERE partition_sha256 = ? AND stream_id = ?",
            (partition_sha256, stream_id),
        )
        head_row = cursor.fetchone()
        issues: set[str] = set()
        events: list[CanonicalEvent | None] = []
        for row in rows:
            try:
                event = self._event_from_row(cursor, row)
            except SQLiteEventLedgerV1IntegrityError:
                issues.add("EVENT_HASH_MISMATCH")
                event = None
            events.append(event)

        previous: CanonicalEvent | None = None
        previous_global = 0
        for index, event in enumerate(events, start=1):
            if event is None:
                previous = None
                continue
            if event.stream_id != stream_id:
                issues.add("STREAM_ID_MISMATCH")
            if event.stream_version != index:
                issues.add("STREAM_VERSION_GAP")
            if (
                event.organization_id
                != self._access_context.partition.organization_id
                or event.tenant_id != self._access_context.partition.tenant_id
                or event.repository_id
                != self._access_context.partition.repository_id
                or event.environment_id
                != self._access_context.partition.environment_id
            ):
                issues.add("PARTITION_MISMATCH")
            if not self._access_context.classification_filter.allows(
                event.classification
            ):
                raise EventLedgerClassificationDeniedError(
                    "TBM_EVENT_LEDGER_CLASSIFICATION_DENIED",
                    "stream classification is not allowed for verification",
                )
            if event.global_position <= previous_global:
                issues.add("GLOBAL_POSITION_INVALID")
            previous_global = event.global_position
            if previous is None:
                if index > 1 or event.previous_stream_event_sha256 is not None:
                    issues.add("HASH_CHAIN_MISMATCH")
            else:
                try:
                    verify_event_parent(event, previous)
                except EventV1ContractError:
                    issues.add("HASH_CHAIN_MISMATCH")
            previous = event

        observed_head_sha256: str | None = None
        if rows:
            raw_digest = rows[-1][1]
            observed_head_sha256 = (
                raw_digest if _valid_digest(raw_digest) else _ZERO_DIGEST
            )
        if rows:
            last_event = events[-1]
            if (
                head_row is None
                or type(head_row[0]) is not int
                or head_row[0] != len(rows)
                or last_event is None
                or head_row[1] != last_event.event_id
                or head_row[2] != last_event.event_sha256
            ):
                issues.add("HEAD_MISMATCH")
        elif head_row is not None and head_row != (0, None, None):
            issues.add("HEAD_MISMATCH")

        ordered_issues = tuple(
            issue for issue in _VERIFICATION_ISSUE_ORDER if issue in issues
        )
        result = LedgerStreamVerification(
            stream_id=stream_id,
            partition_sha256=partition_sha256,
            verified_stream_version=len(rows),
            verified_event_count=len(rows),
            head_event_sha256=observed_head_sha256,
            valid=not ordered_issues,
            issue_codes=ordered_issues,
        )
        verify_ledger_stream_verification(
            self._access_context,
            stream_id,
            result,
        )
        return result

    def _verify_storage_metadata(self, cursor: sqlite3.Cursor) -> None:
        """Verify global ordering, every retained stream, and durable receipts."""

        cursor.execute(
            f"SELECT {self._event_select()} FROM v3_event_ledger_events "
            "ORDER BY global_position"
        )
        rows = cursor.fetchall()
        events: list[CanonicalEvent] = []
        stream_heads: dict[
            tuple[str, str],
            tuple[str, str, str, str, int, str, str],
        ] = {}
        previous_by_stream: dict[tuple[str, str], CanonicalEvent] = {}
        for expected_position, row in enumerate(rows, start=1):
            event = self._event_from_row(cursor, row)
            if event.global_position != expected_position:
                raise _integrity_error(
                    "global event positions are not gap-free and ordered"
                )
            partition = LedgerTenantPartition(
                organization_id=event.organization_id,
                tenant_id=event.tenant_id,
                repository_id=event.repository_id,
                environment_id=event.environment_id,
            )
            partition_sha256 = str(row[2])
            if partition.partition_sha256 != partition_sha256:
                raise _integrity_error(
                    "stored event partition hash does not match its identities"
                )
            key = (partition_sha256, event.stream_id)
            previous = previous_by_stream.get(key)
            expected_stream_version = 1 if previous is None else (
                previous.stream_version + 1
            )
            if event.stream_version != expected_stream_version:
                raise _integrity_error(
                    "stored stream versions are not gap-free and ordered"
                )
            try:
                verify_event_parent(event, previous)
            except EventV1ContractError as error:
                raise _integrity_error(
                    "stored stream hash chain is invalid"
                ) from error
            previous_by_stream[key] = event
            stream_heads[key] = (
                event.organization_id,
                event.tenant_id,
                event.repository_id,
                event.environment_id,
                event.stream_version,
                event.event_id,
                event.event_sha256,
            )
            events.append(event)

        cursor.execute(
            "SELECT current_global_position, current_event_id, "
            "current_event_sha256 FROM v3_event_ledger_global_head "
            "WHERE singleton = 1"
        )
        global_rows = cursor.fetchall()
        if len(global_rows) != 1:
            raise _integrity_error("global ledger head is missing or duplicated")
        expected_global_head: tuple[object, ...] = (
            (0, None, None)
            if not events
            else (
                len(events),
                events[-1].event_id,
                events[-1].event_sha256,
            )
        )
        if tuple(global_rows[0]) != expected_global_head:
            raise _integrity_error(
                "global ledger head does not match the retained event tail"
            )

        cursor.execute(
            "SELECT partition_sha256, stream_id, organization_id, tenant_id, "
            "repository_id, environment_id, current_stream_version, "
            "current_event_id, current_event_sha256 "
            "FROM v3_event_ledger_stream_heads "
            "ORDER BY partition_sha256, stream_id"
        )
        retained_heads = cursor.fetchall()
        if len(retained_heads) != len(stream_heads):
            raise _integrity_error(
                "stream head count does not match the retained streams"
            )
        for row in retained_heads:
            if len(row) != 9 or type(row[0]) is not str or type(row[1]) is not str:
                raise _integrity_error("stored stream head has an invalid shape")
            expected = stream_heads.get((row[0], row[1]))
            if expected is None or tuple(row[2:]) != expected:
                raise _integrity_error(
                    "stored stream head does not match its retained event tail"
                )

        cursor.execute(
            "SELECT partition_sha256, idempotency_key_sha256, command_sha256, "
            "request_sha256, stream_id, previous_stream_version, "
            "current_stream_version, first_global_position, "
            "last_global_position, event_sha256s_json, receipt_sha256 "
            "FROM v3_event_ledger_idempotency "
            "ORDER BY partition_sha256, idempotency_key_sha256"
        )
        for row in cursor.fetchall():
            if len(row) != 11 or type(row[0]) is not str or type(row[1]) is not str:
                raise _integrity_error(
                    "stored idempotency record has an invalid shape"
                )
            receipt = self._receipt_from_idempotency_row(
                cursor,
                row[1],
                tuple(row[2:]),
            )
            if (
                len(receipt.events)
                != receipt.current_stream_version
                - receipt.previous_stream_version
                or len(receipt.events)
                != receipt.last_global_position
                - receipt.first_global_position
                + 1
            ):
                raise _integrity_error(
                    "stored idempotency receipt has inconsistent ranges"
                )
            for offset, event in enumerate(receipt.events):
                if (
                    LedgerTenantPartition(
                        organization_id=event.organization_id,
                        tenant_id=event.tenant_id,
                        repository_id=event.repository_id,
                        environment_id=event.environment_id,
                    ).partition_sha256
                    != row[0]
                    or event.stream_id != receipt.stream_id
                    or event.stream_version
                    != receipt.previous_stream_version + offset + 1
                    or event.global_position
                    != receipt.first_global_position + offset
                    or event.idempotency_key_sha256 != row[1]
                    or event.request_sha256 != receipt.command_sha256
                ):
                    raise _integrity_error(
                        "stored idempotency receipt does not match its events"
                    )

        cursor.execute(
            "SELECT projection_name, projection_version, partition_sha256, "
            "global_position, state_sha256, descriptor "
            "FROM v3_event_ledger_checkpoints ORDER BY projection_name, "
            "projection_version, partition_sha256, global_position"
        )
        for (
            projection_name,
            projection_version,
            partition_sha256,
            global_position,
            state_sha256,
            descriptor,
        ) in cursor.fetchall():
            if (
                type(projection_name) is not str
                or _CHECKPOINT_NAME_RE.fullmatch(projection_name) is None
                or type(projection_version) is not int
                or projection_version < 1
                or not _valid_digest(partition_sha256)
                or type(global_position) is not int
                or not 0 <= global_position <= len(events)
                or not _valid_digest(state_sha256)
                or type(descriptor) is not str
            ):
                raise _integrity_error(
                    "stored projection checkpoint has an invalid shape"
                )
            try:
                parsed_descriptor = json.loads(descriptor)
            except (TypeError, json.JSONDecodeError) as error:
                raise _integrity_error(
                    "stored projection checkpoint descriptor is invalid"
                ) from error
            if _canonical_json(parsed_descriptor) != descriptor:
                raise _integrity_error(
                    "stored projection checkpoint descriptor is noncanonical"
                )

    @_synchronized
    def verify_stream(self, stream_id: str) -> LedgerStreamVerification:
        self._require_open()
        LedgerStreamReadRequest(self._access_context, stream_id, 1, 1)
        try:
            with self._transaction(write=False), closing(
                self._connection.cursor()
            ) as cursor:
                self._require_schema(cursor)
                return self._verification_result(cursor, stream_id)
        except EventLedgerPortError:
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
            raise _persistence_error(
                "SQLite stream verification failed"
            ) from error

    @_synchronized
    def verify_integrity(self) -> tuple[LedgerStreamVerification, ...]:
        """Run SQLite integrity_check and verify every retained stream."""

        self._require_open()
        try:
            with self._transaction(write=False), closing(
                self._connection.cursor()
            ) as cursor:
                self._require_schema(cursor)
                cursor.execute("PRAGMA integrity_check")
                if cursor.fetchall() != [("ok",)]:
                    raise _integrity_error(
                        "SQLite integrity_check rejected the event ledger"
                    )
                self._verify_storage_metadata(cursor)
                cursor.execute(
                    "SELECT stream_id FROM v3_event_ledger_stream_heads "
                    "WHERE partition_sha256 = ? ORDER BY stream_id",
                    (self._access_context.partition.partition_sha256,),
                )
                stream_ids = tuple(row[0] for row in cursor.fetchall())
                return tuple(
                    self._verification_result(cursor, stream_id)
                    for stream_id in stream_ids
                )
        except EventLedgerPortError:
            raise
        except sqlite3.DatabaseError as error:
            if _is_schema_error(error):
                raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
            raise _persistence_error(
                "SQLite event ledger integrity verification failed"
            ) from error

    @_synchronized
    def subscribe(
        self,
        after_position: int = 0,
        limit: int = 100,
        poll_timeout_seconds: int = 10,
    ) -> SQLiteEventLedgerSubscription:
        self._require_open()
        request = LedgerSubscriptionRequest(
            self._access_context,
            after_position,
            limit,
            poll_timeout_seconds,
        )
        return SQLiteEventLedgerSubscription(self, request)

    @_synchronized
    def backup(
        self,
        destination: str | Path,
        *,
        overwrite: bool = False,
        timeout_seconds: int | float = 30.0,
    ) -> Path:
        """Publish one verified atomic SQLite backup while the owner lock is held."""

        self._require_open()
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean")
        destination_path = Path(destination).expanduser().resolve(strict=False)
        if (
            self._database_path is not None
            and os.path.normcase(str(destination_path))
            == os.path.normcase(str(self._database_path))
        ):
            raise ValueError("backup destination must differ from the ledger database")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            self.verify_integrity()
            with snapshot_write_lock(
                destination_path,
                timeout_seconds=timeout_seconds,
            ):
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=destination_path.parent,
                    prefix=f".{destination_path.name}.",
                    suffix=".tmp",
                )
                os.close(descriptor)
                temporary_path = Path(temporary_name)
                target = sqlite3.connect(temporary_path, isolation_level=None)
                try:
                    self._connection.backup(target)
                    target.execute("PRAGMA foreign_keys = ON")
                    target.execute("PRAGMA recursive_triggers = ON")
                    if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise _integrity_error(
                            "SQLite backup failed integrity verification"
                        )
                    with closing(target.cursor()) as cursor:
                        if (
                            _read_schema_definitions(cursor)
                            != _canonical_schema_definitions()
                        ):
                            raise _integrity_error(
                                "SQLite backup schema catalog mismatch"
                            )
                finally:
                    target.close()
                with temporary_path.open("r+b") as handle:
                    os.fsync(handle.fileno())
                if overwrite:
                    os.replace(temporary_path, destination_path)
                else:
                    os.link(temporary_path, destination_path)
                return destination_path
        except EventLedgerPortError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise SQLiteEventLedgerV1PersistenceError(
                "TBM_EVENT_LEDGER_SQLITE_BACKUP_FAILED",
                "SQLite event ledger backup failed",
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_error: BaseException | None = None
        if self._owns_connection:
            try:
                self._connection.close()
            except BaseException as error:
                close_error = error
        if self._lock_retained:
            _release_connection_lock(self._connection)
            self._lock_retained = False
        if self._file_lock is not None:
            try:
                self._file_lock.__exit__(None, None, None)
            except BaseException as error:
                if close_error is None:
                    close_error = error
                else:
                    close_error.add_note(
                        f"also failed to release SQLite ledger file lock: {error}"
                    )
            self._file_lock = None
        if close_error is not None:
            raise _persistence_error(
                "failed to close SQLite event ledger cleanly"
            ) from close_error

    def __enter__(self) -> SQLiteEventLedgerV1:
        self._require_open()
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()


class SQLiteEventLedgerSubscription:
    """Bounded at-least-once polling cursor over one SQLite ledger port."""

    def __init__(
        self,
        ledger: SQLiteEventLedgerV1,
        request: LedgerSubscriptionRequest,
    ) -> None:
        self._ledger = ledger
        self._request = request
        self._cursor = request.after_position
        self._outstanding: LedgerSubscriptionPage | None = None
        self._closed = False
        self._lock = RLock()
        self._subscription_id = f"subscription_{uuid4().hex}"

    def _require_open(self) -> None:
        if self._closed:
            raise EventLedgerInvalidRequestError(
                "TBM_EVENT_LEDGER_REQUEST_INVALID",
                "event ledger subscription is closed",
            )

    def poll(self) -> LedgerSubscriptionPage:
        with self._lock:
            self._require_open()
            if self._outstanding is not None:
                return self._outstanding
            page = self._ledger.read_global(
                self._cursor,
                self._request.limit,
            )
            delivery = LedgerSubscriptionPage(
                subscription_id=self._subscription_id,
                delivery_id=f"delivery_{uuid4().hex}",
                page=page,
                heartbeat=not page.events,
            )
            self._outstanding = delivery
            return delivery

    def acknowledge(
        self,
        delivery_id: str,
        *,
        expected_next_global_position: int | None,
    ) -> None:
        with self._lock:
            self._require_open()
            outstanding = self._outstanding
            if (
                outstanding is None
                or type(delivery_id) is not str
                or delivery_id != outstanding.delivery_id
                or expected_next_global_position
                != outstanding.page.next_global_position
            ):
                raise EventLedgerConflictError(
                    "TBM_EVENT_LEDGER_SUBSCRIPTION_CONFLICT",
                    "subscription acknowledgement does not match delivery",
                )
            if outstanding.page.events:
                self._cursor = outstanding.page.events[-1].global_position
            self._outstanding = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._outstanding = None


__all__ = [
    "SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE",
    "SQLITE_EVENT_LEDGER_V1_SCHEMA_VERSION",
    "SQLiteEventLedgerSubscription",
    "SQLiteEventLedgerV1",
    "SQLiteEventLedgerV1Error",
    "SQLiteEventLedgerV1IntegrityError",
    "SQLiteEventLedgerV1PersistenceError",
    "SQLiteEventLedgerV1SchemaError",
]
