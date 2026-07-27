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

from ._timestamps import parse_rfc3339
from .audit_v3 import (
    AUDIT_JSON_MAX_BYTES,
    AUDIT_MAX_SEQUENCE,
    AuditContractError,
    AuditEvent,
    RecoveryAction,
    dumps_audit_event,
    dumps_recovery_action,
    loads_audit_event,
    loads_recovery_action,
    verify_audit_event_parent,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_AUDIT_V3_SCHEMA_VERSION = 1
SQLITE_AUDIT_V3_MAX_PAGE_SIZE = 1000
_MISSING_SCHEMA_MESSAGE = "SQLite audit v3 schema is missing or incomplete"
_AUDIT_EVENT_ID_RE = re.compile(r"audit_event_sha256_[0-9a-f]{64}")
_RECOVERY_ACTION_ID_RE = re.compile(
    r"recovery_action_sha256_[0-9a-f]{64}"
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_audit_schema",
    "v3_audit_events",
    "v3_audit_events_append",
    "v3_audit_events_immutable_delete",
    "v3_audit_events_immutable_update",
    "v3_audit_events_session",
    "v3_audit_events_type",
    "v3_audit_stream_heads",
    "v3_audit_stream_heads_advance",
    "v3_audit_stream_heads_identity_immutable",
    "v3_audit_stream_heads_initial",
    "v3_audit_stream_heads_immutable_delete",
    "v3_recovery_actions",
    "v3_recovery_actions_immutable_delete",
    "v3_recovery_actions_immutable_update",
    "v3_recovery_actions_session",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteAuditV3Error(RuntimeError):
    pass


class SQLiteAuditV3SchemaError(SQLiteAuditV3Error):
    pass


class SQLiteAuditV3ConflictError(SQLiteAuditV3Error):
    pass


class SQLiteAuditV3PersistenceError(SQLiteAuditV3Error):
    pass


@dataclass(frozen=True)
class AuditStreamHead:
    stream_id: str
    tenant_id: str
    repository_id: str
    session_id: str
    trace_id: str
    run_id: str
    current_sequence: int
    current_event_id: str


@dataclass(frozen=True)
class SQLiteAuditV3AppendResult:
    event_id: str
    event_inserted: bool
    recovery_action_id: str | None
    recovery_inserted: bool


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
        raise SQLiteAuditV3SchemaError(
            "SQLite audit v3 schema contains an invalid definition"
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
        raise SQLiteAuditV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteAuditV3SchemaError(
                "SQLite audit v3 schema definition has an invalid shape"
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
                    "schemas/sqlite-v3-audit.sql"
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
        raise SQLiteAuditV3SchemaError(
            "could not validate the canonical SQLite audit v3 schema"
        ) from error


class SQLiteAuditV3Repository:
    """Opt-in immutable SQLite ledger for audit and recovery evidence."""

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
    ) -> SQLiteAuditV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        "schemas/sqlite-v3-audit.sql"
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
            raise SQLiteAuditV3PersistenceError(
                "failed to connect to SQLite audit v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteAuditV3Error(
                "SQLite audit v3 repository is closed"
            )
        try:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteAuditV3Error(
                "SQLite audit v3 repository is closed"
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
                "failed to close unusable SQLite audit v3 connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_audit_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to clean up SQLite audit v3 savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after audit v3 "
                            "savepoint cleanup failed"
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
                context="the top-level SQLite audit v3 transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level SQLite audit v3 transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteAuditV3SchemaError(
                "SQLite audit v3 requires foreign keys to remain enabled"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteAuditV3SchemaError(
                "SQLite audit v3 requires recursive triggers to remain "
                "enabled"
            )
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_audit_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if rows != [(SQLITE_AUDIT_V3_SCHEMA_VERSION, "tbm.audit-event.v3")]:
            raise SQLiteAuditV3SchemaError(
                "SQLite audit v3 schema metadata mismatch"
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteAuditV3SchemaError(
                "SQLite audit v3 schema definitions do not match the "
                "canonical version"
            )

    @staticmethod
    def _event_row(event: AuditEvent) -> tuple[object, ...]:
        recovery_action_ids = tuple(
            reference.record_id
            for reference in event.references
            if reference.kind == "recovery_action"
        )
        recovery_action_id = (
            recovery_action_ids[0]
            if len(recovery_action_ids) == 1
            else None
        )
        return (
            event.event_id,
            event.stream_id,
            event.sequence,
            event.previous_event_id,
            event.tenant_id,
            event.repository_id,
            event.session_id,
            event.trace_id,
            event.run_id,
            event.actor_type,
            event.actor_id,
            event.event_type,
            recovery_action_id,
            event.reason_code,
            event.payload_sha256,
            event.occurred_at,
            dumps_audit_event(event),
        )

    @staticmethod
    def _stored_event(row: tuple[object, ...]) -> AuditEvent:
        if len(row) != 17 or type(row[16]) is not str:
            raise SQLiteAuditV3PersistenceError(
                "SQLite audit event row has an invalid shape"
            )
        try:
            event = loads_audit_event(row[16])
        except AuditContractError as error:
            raise SQLiteAuditV3PersistenceError(
                "SQLite audit event descriptor failed validation"
            ) from error
        if row != SQLiteAuditV3Repository._event_row(event):
            raise SQLiteAuditV3PersistenceError(
                "SQLite audit event columns do not match descriptor"
            )
        return event

    @staticmethod
    def _recovery_row(
        recovery: RecoveryAction,
        event_id: str,
    ) -> tuple[object, ...]:
        return (
            recovery.recovery_action_id,
            event_id,
            recovery.session_id,
            recovery.request_sha256,
            dumps_recovery_action(recovery),
        )

    @staticmethod
    def _stored_recovery(
        row: tuple[object, ...],
    ) -> tuple[RecoveryAction, str]:
        if len(row) != 5 or type(row[4]) is not str:
            raise SQLiteAuditV3PersistenceError(
                "SQLite recovery action row has an invalid shape"
            )
        try:
            recovery = loads_recovery_action(row[4])
        except AuditContractError as error:
            raise SQLiteAuditV3PersistenceError(
                "SQLite recovery action descriptor failed validation"
            ) from error
        event_id = cast(str, row[1])
        if row != SQLiteAuditV3Repository._recovery_row(
            recovery,
            event_id,
        ):
            raise SQLiteAuditV3PersistenceError(
                "SQLite recovery action columns do not match descriptor"
            )
        return recovery, event_id

    @staticmethod
    def _head_from_row(row: tuple[object, ...]) -> AuditStreamHead:
        if (
            len(row) != 8
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
            or type(row[3]) is not str
            or type(row[4]) is not str
            or type(row[5]) is not str
            or type(row[6]) is not int
            or type(row[7]) is not str
            or not 1 <= row[6] <= AUDIT_MAX_SEQUENCE
            or _AUDIT_EVENT_ID_RE.fullmatch(row[7]) is None
        ):
            raise SQLiteAuditV3PersistenceError(
                "SQLite audit stream head has an invalid shape"
            )
        try:
            for name, value in zip(
                (
                    "stream_id",
                    "tenant_id",
                    "repository_id",
                    "session_id",
                    "trace_id",
                    "run_id",
                ),
                row[:6],
                strict=True,
            ):
                _validate_identifier(value, name)
        except ValueError as error:
            raise SQLiteAuditV3PersistenceError(
                "SQLite audit stream head identity failed validation"
            ) from error
        return AuditStreamHead(
            stream_id=row[0],
            tenant_id=row[1],
            repository_id=row[2],
            session_id=row[3],
            trace_id=row[4],
            run_id=row[5],
            current_sequence=row[6],
            current_event_id=row[7],
        )

    @staticmethod
    def _event_select() -> str:
        return (
            "event_id, stream_id, sequence, previous_event_id, tenant_id, "
            "repository_id, session_id, trace_id, run_id, actor_type, "
            "actor_id, event_type, recovery_action_id, reason_code, "
            "payload_sha256, occurred_at, descriptor"
        )

    def _select_event_by_id(
        self,
        cursor: sqlite3.Cursor,
        event_id: str,
    ) -> AuditEvent | None:
        cursor.execute(
            f"SELECT {self._event_select()} FROM v3_audit_events "
            "WHERE event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._stored_event(row)

    def _select_head(
        self,
        cursor: sqlite3.Cursor,
        stream_id: str,
    ) -> AuditStreamHead | None:
        cursor.execute(
            "SELECT stream_id, tenant_id, repository_id, session_id, "
            "trace_id, run_id, current_sequence, current_event_id "
            "FROM v3_audit_stream_heads WHERE stream_id = ?",
            (stream_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        head = self._head_from_row(row)
        event = self._select_event_by_id(cursor, head.current_event_id)
        if (
            event is None
            or event.stream_id != head.stream_id
            or event.sequence != head.current_sequence
            or event.tenant_id != head.tenant_id
            or event.repository_id != head.repository_id
            or event.session_id != head.session_id
            or event.trace_id != head.trace_id
            or event.run_id != head.run_id
        ):
            raise SQLiteAuditV3PersistenceError(
                "SQLite audit stream head does not match its current event"
            )
        return head

    def _put_event(
        self,
        cursor: sqlite3.Cursor,
        event: AuditEvent,
    ) -> bool:
        existing = self._select_event_by_id(cursor, event.event_id)
        if existing is not None:
            if self._event_row(existing) != self._event_row(event):
                raise SQLiteAuditV3ConflictError(
                    "SQLite audit event ID has conflicting immutable content"
                )
            return False

        head = self._select_head(cursor, event.stream_id)
        if head is None:
            if event.sequence != 1 or event.previous_event_id is not None:
                raise SQLiteAuditV3ConflictError(
                    "SQLite audit stream must begin at sequence one"
                )
            cursor.execute(
                "INSERT INTO v3_audit_stream_heads ("
                "stream_id, tenant_id, repository_id, session_id, trace_id, "
                "run_id, current_sequence, current_event_id"
                ") VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                (
                    event.stream_id,
                    event.tenant_id,
                    event.repository_id,
                    event.session_id,
                    event.trace_id,
                    event.run_id,
                ),
            )
            previous_sequence = 0
            previous_event_id = None
        else:
            if (
                event.tenant_id != head.tenant_id
                or event.repository_id != head.repository_id
                or event.session_id != head.session_id
                or event.trace_id != head.trace_id
                or event.run_id != head.run_id
            ):
                raise SQLiteAuditV3ConflictError(
                    "SQLite audit event identity differs from stream head"
                )
            parent = self._select_event_by_id(
                cursor,
                head.current_event_id,
            )
            if parent is None:
                raise SQLiteAuditV3PersistenceError(
                    "SQLite audit stream head references a missing event"
                )
            try:
                verify_audit_event_parent(event, parent)
            except AuditContractError as error:
                raise SQLiteAuditV3ConflictError(
                    "SQLite audit event does not extend the current stream"
                ) from error
            previous_sequence = head.current_sequence
            previous_event_id = head.current_event_id

        descriptor = dumps_audit_event(event)
        if len(descriptor.encode("utf-8")) > AUDIT_JSON_MAX_BYTES:
            raise ValueError("audit event descriptor exceeds storage limit")
        cursor.execute(
            "INSERT INTO v3_audit_events ("
            "event_id, stream_id, sequence, previous_event_id, tenant_id, "
            "repository_id, session_id, trace_id, run_id, actor_type, "
            "actor_id, event_type, recovery_action_id, reason_code, "
            "payload_sha256, occurred_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._event_row(event),
        )
        cursor.execute(
            "UPDATE v3_audit_stream_heads "
            "SET current_sequence = ?, current_event_id = ? "
            "WHERE stream_id = ? AND current_sequence = ? "
            "AND current_event_id IS ?",
            (
                event.sequence,
                event.event_id,
                event.stream_id,
                previous_sequence,
                previous_event_id,
            ),
        )
        if cursor.rowcount != 1:
            raise SQLiteAuditV3ConflictError(
                "SQLite audit stream changed during append"
            )
        return True

    @staticmethod
    def _validate_recovery_event(
        recovery: RecoveryAction,
        event: AuditEvent,
    ) -> None:
        expected_type = (
            "recovery_succeeded"
            if recovery.result == "succeeded"
            else "recovery_failed"
        )
        recovery_references = tuple(
            reference.record_id
            for reference in event.references
            if reference.kind == "recovery_action"
        )
        if (
            event.event_type != expected_type
            or recovery_references != (recovery.recovery_action_id,)
            or event.actor_id != recovery.executor_id
            or event.session_id != recovery.session_id
            or event.trace_id != recovery.trace_id
            or event.run_id != recovery.run_id
            or parse_rfc3339(event.occurred_at)
            < parse_rfc3339(recovery.finished_at)
        ):
            raise SQLiteAuditV3ConflictError(
                "recovery action and audit event linkage differs"
            )

    def _put_recovery(
        self,
        cursor: sqlite3.Cursor,
        recovery: RecoveryAction,
        event: AuditEvent,
    ) -> bool:
        cursor.execute(
            "SELECT recovery_action_id, event_id, session_id, "
            "request_sha256, descriptor FROM v3_recovery_actions "
            "WHERE recovery_action_id = ?",
            (recovery.recovery_action_id,),
        )
        row = cursor.fetchone()
        expected = self._recovery_row(recovery, event.event_id)
        if row is not None:
            stored, stored_event_id = self._stored_recovery(row)
            if self._recovery_row(stored, stored_event_id) != expected:
                raise SQLiteAuditV3ConflictError(
                    "SQLite recovery action ID has conflicting content"
                )
            return False
        descriptor = dumps_recovery_action(recovery)
        if len(descriptor.encode("utf-8")) > AUDIT_JSON_MAX_BYTES:
            raise ValueError("recovery action descriptor exceeds storage limit")
        cursor.execute(
            "INSERT INTO v3_recovery_actions ("
            "recovery_action_id, event_id, session_id, request_sha256, "
            "descriptor) VALUES (?, ?, ?, ?, ?)",
            expected,
        )
        return True

    @staticmethod
    def _raise_database_error(
        error: sqlite3.DatabaseError,
        message: str,
    ) -> NoReturn:
        if _is_schema_error(error):
            raise SQLiteAuditV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        if isinstance(error, sqlite3.IntegrityError):
            raise SQLiteAuditV3ConflictError(message) from error
        raise SQLiteAuditV3PersistenceError(message) from error

    @_synchronized
    def append_event(self, event: AuditEvent) -> bool:
        self._require_open()
        if type(event) is not AuditEvent:
            raise ValueError("event must be exactly AuditEvent")
        if event.event_type in {"recovery_succeeded", "recovery_failed"}:
            raise ValueError(
                "recovery events must be appended with append_recovery"
            )
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._put_event(cursor, event)
        except (SQLiteAuditV3ConflictError, SQLiteAuditV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to append SQLite audit event",
            )

    @_synchronized
    def append_recovery(
        self,
        recovery: RecoveryAction,
        event: AuditEvent,
    ) -> SQLiteAuditV3AppendResult:
        self._require_open()
        if type(recovery) is not RecoveryAction or type(event) is not AuditEvent:
            raise ValueError(
                "recovery and event must be exact audit records"
            )
        self._validate_recovery_event(recovery, event)
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    event_inserted = self._put_event(cursor, event)
                    recovery_inserted = self._put_recovery(
                        cursor,
                        recovery,
                        event,
                    )
            return SQLiteAuditV3AppendResult(
                event_id=event.event_id,
                event_inserted=event_inserted,
                recovery_action_id=recovery.recovery_action_id,
                recovery_inserted=recovery_inserted,
            )
        except (SQLiteAuditV3ConflictError, SQLiteAuditV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to append SQLite recovery evidence",
            )

    @_synchronized
    def load_event(self, event_id: str) -> AuditEvent:
        self._require_open()
        _validate_event_id(event_id)
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    event = self._select_event_by_id(cursor, event_id)
                    if event is None:
                        raise KeyError(event_id)
                    return event
        except (KeyError, SQLiteAuditV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite audit event",
            )

    @_synchronized
    def load_recovery(
        self,
        recovery_action_id: str,
    ) -> tuple[RecoveryAction, AuditEvent]:
        self._require_open()
        _validate_recovery_action_id(recovery_action_id)
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT recovery_action_id, event_id, session_id, "
                        "request_sha256, descriptor "
                        "FROM v3_recovery_actions "
                        "WHERE recovery_action_id = ?",
                        (recovery_action_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(recovery_action_id)
                    recovery, event_id = self._stored_recovery(row)
                    event = self._select_event_by_id(cursor, event_id)
                    if event is None:
                        raise SQLiteAuditV3PersistenceError(
                            "SQLite recovery action references a missing event"
                        )
                    try:
                        self._validate_recovery_event(recovery, event)
                    except SQLiteAuditV3ConflictError as error:
                        raise SQLiteAuditV3PersistenceError(
                            "SQLite recovery action linkage failed "
                            "validation"
                        ) from error
                    return recovery, event
        except (KeyError, SQLiteAuditV3SchemaError):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite recovery evidence",
            )

    @_synchronized
    def stream_head(self, stream_id: str) -> AuditStreamHead | None:
        self._require_open()
        _validate_identifier(stream_id, "stream_id")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._select_head(cursor, stream_id)
        except SQLiteAuditV3SchemaError:
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite audit stream head",
            )

    @_synchronized
    def list_events(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        self._require_open()
        _validate_identifier(stream_id, "stream_id")
        if (
            type(after_sequence) is not int
            or not 0 <= after_sequence <= AUDIT_MAX_SEQUENCE
        ):
            raise ValueError("after_sequence must be a bounded integer")
        if (
            type(limit) is not int
            or not 1 <= limit <= SQLITE_AUDIT_V3_MAX_PAGE_SIZE
        ):
            raise ValueError(
                "limit must be between 1 and "
                f"{SQLITE_AUDIT_V3_MAX_PAGE_SIZE}"
            )
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        f"SELECT {self._event_select()} "
                        "FROM v3_audit_events "
                        "WHERE stream_id = ? AND sequence > ? "
                        "ORDER BY sequence LIMIT ?",
                        (stream_id, after_sequence, limit),
                    )
                    events = tuple(
                        self._stored_event(row) for row in cursor.fetchall()
                    )
                    if not events:
                        return ()
                    parent: AuditEvent | None = None
                    if events[0].sequence > 1:
                        cursor.execute(
                            f"SELECT {self._event_select()} "
                            "FROM v3_audit_events "
                            "WHERE stream_id = ? AND sequence = ?",
                            (stream_id, events[0].sequence - 1),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise SQLiteAuditV3PersistenceError(
                                "SQLite audit stream contains a sequence gap"
                            )
                        parent = self._stored_event(row)
                    for event in events:
                        if parent is not None:
                            try:
                                verify_audit_event_parent(event, parent)
                            except AuditContractError as error:
                                raise SQLiteAuditV3PersistenceError(
                                    "SQLite audit stream failed parent "
                                    "validation"
                                ) from error
                        parent = event
                    return events
        except SQLiteAuditV3SchemaError:
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to list SQLite audit events",
            )

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteAuditV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _validate_event_id(value: object) -> None:
    if type(value) is not str or _AUDIT_EVENT_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "event_id must use audit_event_sha256_<64 lowercase hex>"
        )


def _validate_recovery_action_id(value: object) -> None:
    if (
        type(value) is not str
        or _RECOVERY_ACTION_ID_RE.fullmatch(value) is None
    ):
        raise ValueError(
            "recovery_action_id must use "
            "recovery_action_sha256_<64 lowercase hex>"
        )


def _validate_identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a bounded identifier")


__all__ = [
    "SQLITE_AUDIT_V3_MAX_PAGE_SIZE",
    "SQLITE_AUDIT_V3_SCHEMA_VERSION",
    "AuditStreamHead",
    "SQLiteAuditV3AppendResult",
    "SQLiteAuditV3ConflictError",
    "SQLiteAuditV3Error",
    "SQLiteAuditV3PersistenceError",
    "SQLiteAuditV3Repository",
    "SQLiteAuditV3SchemaError",
]
