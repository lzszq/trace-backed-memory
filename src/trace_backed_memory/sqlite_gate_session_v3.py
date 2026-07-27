from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from ._timestamps import (
    aware_datetime_to_rfc3339,
    canonical_rfc3339,
    parse_rfc3339,
)
from .contracts_v3 import V3ContractError
from .gate_session_v3 import (
    GATE_SESSION_CONTRACT_VERSION,
    GATE_SESSION_MAX_LEASE_SECONDS,
    GATE_SESSION_MAX_BYTES,
    GATE_SESSION_MAX_TTL_SECONDS,
    GateSession,
    GateSessionContractError,
    GateSessionStatus,
    create_gate_session,
    dumps_gate_session,
    loads_gate_session,
    renew_gate_session_lease,
    transition_gate_session,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_GATE_SESSION_SCHEMA_VERSION = 1
_SCHEMA_RESOURCE = "schemas/sqlite-v3-gate-session.sql"
_MISSING_SCHEMA_MESSAGE = (
    "SQLite GateSession v3 schema is missing or incomplete"
)
_SCHEMA_OBJECT_NAMES = (
    "gate_session_heads",
    "gate_session_heads_identity_immutable",
    "gate_session_heads_immutable_delete",
    "gate_session_heads_version_forward",
    "gate_session_revisions",
    "gate_session_revisions_due",
    "gate_session_revisions_immutable_delete",
    "gate_session_revisions_immutable_update",
    "gate_session_revisions_validate_insert",
    "trace_backed_memory_v3_gate_session_schema",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteGateSessionError(V3ContractError):
    """Stable base failure for the side-by-side SQLite GateSession store."""


class SQLiteGateSessionSchemaError(SQLiteGateSessionError):
    pass


class SQLiteGateSessionConflictError(SQLiteGateSessionError):
    pass


class SQLiteGateSessionNotFoundError(SQLiteGateSessionError):
    pass


class SQLiteGateSessionPersistenceError(SQLiteGateSessionError):
    pass


@dataclass(frozen=True)
class SQLiteGateSessionCreateResult:
    session: GateSession
    inserted: bool


def _service_timestamp() -> str:
    return aware_datetime_to_rfc3339(datetime.now(timezone.utc))


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
        raise SQLiteGateSessionSchemaError(
            "TBM_SQLITE_GATE_SESSION_SCHEMA",
            "SQLite GateSession schema contains an invalid definition",
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
        raise SQLiteGateSessionSchemaError(
            "TBM_SQLITE_GATE_SESSION_SCHEMA",
            _MISSING_SCHEMA_MESSAGE,
        )
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                "SQLite GateSession schema definition has an invalid shape",
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE sql IS NOT NULL AND ("
        "tbl_name IN ("
        "'trace_backed_memory_v3_gate_session_schema', "
        "'gate_session_heads', "
        "'gate_session_revisions'"
        ") "
        "OR name = 'trace_backed_memory_v3_gate_session_schema'"
        ") AND name NOT IN ("
        + placeholders
        + ") ORDER BY name",
        _SCHEMA_OBJECT_NAMES,
    )
    unexpected = cursor.fetchone()
    if unexpected is not None:
        raise SQLiteGateSessionSchemaError(
            "TBM_SQLITE_GATE_SESSION_SCHEMA",
            "SQLite GateSession schema contains an unexpected object",
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
                read_packaged_resource(_SCHEMA_RESOURCE).decode("utf-8")
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
        raise SQLiteGateSessionSchemaError(
            "TBM_SQLITE_GATE_SESSION_SCHEMA",
            "could not validate the canonical SQLite GateSession schema",
        ) from error


class SQLiteGateSessionRepository:
    """Append-only durable GateSession revisions in an isolated SQLite schema."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        owns_connection: bool = False,
        clock: Callable[[], str] = _service_timestamp,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a sqlite3.Connection")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._connection = connection
        self._owns_connection = owns_connection
        self._clock = clock
        self._lock = RLock()
        self._closed = False
        self._savepoint_number = 0
        try:
            if not self._connection.in_transaction:
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA recursive_triggers = ON")
            foreign_keys = self._connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()
            recursive_triggers = self._connection.execute(
                "PRAGMA recursive_triggers"
            ).fetchone()
        except sqlite3.Error as error:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "could not enforce SQLite GateSession foreign keys",
            ) from error
        if foreign_keys != (1,):
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_FOREIGN_KEYS",
                "SQLite GateSession repository requires foreign keys",
            )
        if recursive_triggers != (1,):
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_RECURSIVE_TRIGGERS",
                "SQLite GateSession repository requires recursive triggers",
            )

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        clock: Callable[[], str] = _service_timestamp,
        **kwargs: object,
    ) -> "SQLiteGateSessionRepository":
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
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
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "failed to connect to SQLite GateSession storage",
            ) from error
        return cls(connection, owns_connection=True, clock=clock)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_CLOSED",
                "SQLite GateSession repository is closed",
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.Error as error:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_CLOSED",
                "SQLite GateSession repository is closed",
            ) from error

    def _rollback_connection_or_close(
        self,
        primary_error: BaseException,
        *,
        context: str,
    ) -> None:
        for _attempt in range(2):
            try:
                self._connection.rollback()
                return
            except BaseException as cleanup_error:
                primary_error.add_note(
                    f"failed to roll back {context}: {cleanup_error}"
                )
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite GateSession connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_gate_session_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException as error:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                except BaseException as cleanup_error:
                    error.add_note(
                        "failed to clean up SQLite GateSession savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        error,
                        context="the outer SQLite GateSession transaction",
                    )
                raise
            else:
                try:
                    self._connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                except BaseException as error:
                    try:
                        self._connection.execute(
                            f"ROLLBACK TO SAVEPOINT {savepoint}"
                        )
                        self._connection.execute(
                            f"RELEASE SAVEPOINT {savepoint}"
                        )
                    except BaseException as cleanup_error:
                        error.add_note(
                            "failed to clean up unreleased SQLite "
                            f"GateSession savepoint: {cleanup_error}"
                        )
                        self._rollback_connection_or_close(
                            error,
                            context=(
                                "the outer SQLite GateSession transaction"
                            ),
                        )
                    raise
            return

        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the top-level SQLite GateSession transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level SQLite GateSession transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                "SQLite GateSession foreign keys are disabled",
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                "SQLite GateSession recursive triggers are disabled",
            )
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_gate_session_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                "SQLite GateSession schema metadata must contain one row",
            )
        if rows[0] != (
            SQLITE_GATE_SESSION_SCHEMA_VERSION,
            GATE_SESSION_CONTRACT_VERSION,
        ):
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                "SQLite GateSession schema metadata does not match "
                "the supported contract",
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                "SQLite GateSession schema definitions do not match "
                "the canonical version",
            )

    def _trusted_now(self) -> str:
        try:
            return canonical_rfc3339(self._clock())
        except (TypeError, ValueError) as error:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_CLOCK",
                "trusted GateSession clock returned an invalid timestamp",
            ) from error

    def _trusted_after(self, previous: str) -> str:
        now = self._trusted_now()
        parsed_now = parse_rfc3339(now)
        parsed_previous = parse_rfc3339(previous)
        if parsed_now < parsed_previous:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_CLOCK",
                "trusted GateSession clock moved backwards",
            )
        if parsed_now == parsed_previous:
            return aware_datetime_to_rfc3339(
                parsed_previous + timedelta(microseconds=1)
            )
        return now

    @staticmethod
    def _deadline(now: str, seconds: int, *, maximum: int) -> str:
        if type(seconds) is not int or seconds < 1 or seconds > maximum:
            raise ValueError(
                f"seconds must be an integer from 1 through {maximum}"
            )
        return aware_datetime_to_rfc3339(
            parse_rfc3339(now) + timedelta(seconds=seconds)
        )

    @staticmethod
    def _revision_row(session: GateSession) -> tuple[object, ...]:
        payload = dumps_gate_session(session)
        if len(payload.encode("utf-8")) > GATE_SESSION_MAX_BYTES:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "GateSession payload exceeds the storage limit",
            )
        return (
            session.session_id,
            session.version,
            session.status,
            canonical_rfc3339(session.updated_at),
            canonical_rfc3339(session.expires_at),
            (
                canonical_rfc3339(session.lease_expires_at)
                if session.lease_expires_at is not None
                else None
            ),
            payload,
        )

    @staticmethod
    def _session_from_row(row: tuple[object, ...]) -> GateSession:
        if len(row) != 7 or type(row[6]) is not str:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "GateSession revision row has an invalid shape",
            )
        payload = row[6]
        try:
            session = loads_gate_session(payload)
        except GateSessionContractError as error:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "stored GateSession payload failed contract validation",
            ) from error
        if row != SQLiteGateSessionRepository._revision_row(session):
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "GateSession revision columns do not match payload",
            )
        return session

    @staticmethod
    def _session_from_joined_row(row: tuple[object, ...]) -> GateSession:
        if len(row) != 15:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "GateSession joined row has an invalid shape",
            )
        session = SQLiteGateSessionRepository._session_from_row(row[:7])
        head_values = row[7:]
        expected = (
            session.tenant_id,
            session.repository_id,
            session.principal_id,
            session.agent_client_id,
            session.trace_id,
            session.run_id,
            session.request_fingerprint,
            session.idempotency_key,
        )
        if head_values != expected:
            raise SQLiteGateSessionPersistenceError(
                "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
                "GateSession head identity does not match revision payload",
            )
        return session

    @staticmethod
    def _insert_revision(
        cursor: sqlite3.Cursor,
        session: GateSession,
    ) -> None:
        cursor.execute(
            "INSERT INTO gate_session_revisions ("
            "session_id, version, status, updated_at, expires_at, "
            "lease_expires_at, payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            SQLiteGateSessionRepository._revision_row(session),
        )

    @staticmethod
    def _select_current(
        cursor: sqlite3.Cursor,
        session_id: str,
    ) -> GateSession:
        cursor.execute(
            "SELECT revision.session_id, revision.version, revision.status, "
            "revision.updated_at, revision.expires_at, "
            "revision.lease_expires_at, revision.payload, "
            "head.tenant_id, head.repository_id, head.principal_id, "
            "head.agent_client_id, head.trace_id, head.run_id, "
            "head.request_fingerprint, head.idempotency_key "
            "FROM gate_session_heads AS head "
            "JOIN gate_session_revisions AS revision "
            "ON revision.session_id = head.session_id "
            "AND revision.version = head.current_version "
            "WHERE head.session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteGateSessionNotFoundError(
                "TBM_SQLITE_GATE_SESSION_NOT_FOUND",
                "GateSession was not found",
            )
        return SQLiteGateSessionRepository._session_from_joined_row(row)

    @staticmethod
    def _same_idempotent_request(
        existing: GateSession,
        proposed: GateSession,
    ) -> bool:
        return all(
            getattr(existing, field_name) == getattr(proposed, field_name)
            for field_name in (
                "tenant_id",
                "repository_id",
                "principal_id",
                "agent_client_id",
                "trace_id",
                "run_id",
                "request_fingerprint",
                "idempotency_key",
            )
        )

    @_synchronized
    def create_or_get(
        self,
        *,
        session_id: str,
        tenant_id: str,
        repository_id: str,
        principal_id: str,
        agent_client_id: str,
        trace_id: str,
        run_id: str,
        request_fingerprint: str,
        idempotency_key: str,
        expires_in_seconds: int,
    ) -> SQLiteGateSessionCreateResult:
        self._require_open()
        now = self._trusted_now()
        expires_at = self._deadline(
            now,
            expires_in_seconds,
            maximum=GATE_SESSION_MAX_TTL_SECONDS,
        )
        proposed = create_gate_session(
            session_id=session_id,
            tenant_id=tenant_id,
            repository_id=repository_id,
            principal_id=principal_id,
            agent_client_id=agent_client_id,
            trace_id=trace_id,
            run_id=run_id,
            request_fingerprint=request_fingerprint,
            idempotency_key=idempotency_key,
            created_at=now,
            expires_at=expires_at,
        )
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT session_id FROM gate_session_heads "
                        "WHERE tenant_id = ? AND repository_id = ? "
                        "AND principal_id = ? AND agent_client_id = ? "
                        "AND idempotency_key = ?",
                        (
                            tenant_id,
                            repository_id,
                            principal_id,
                            agent_client_id,
                            idempotency_key,
                        ),
                    )
                    idempotent_row = cursor.fetchone()
                    if idempotent_row is not None:
                        existing = self._select_current(
                            cursor,
                            idempotent_row[0],
                        )
                        if not self._same_idempotent_request(
                            existing,
                            proposed,
                        ):
                            raise SQLiteGateSessionConflictError(
                                "TBM_SQLITE_GATE_SESSION_IDEMPOTENCY_CONFLICT",
                                "idempotency key is bound to another request",
                            )
                        return SQLiteGateSessionCreateResult(
                            session=existing,
                            inserted=False,
                        )
                    cursor.execute(
                        "SELECT 1 FROM gate_session_heads "
                        "WHERE session_id = ?",
                        (session_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise SQLiteGateSessionConflictError(
                            "TBM_SQLITE_GATE_SESSION_ID_CONFLICT",
                            "session_id is already bound to another request",
                        )
                    cursor.execute(
                        "INSERT INTO gate_session_heads ("
                        "session_id, tenant_id, repository_id, principal_id, "
                        "agent_client_id, trace_id, run_id, "
                        "request_fingerprint, idempotency_key, "
                        "current_version"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                        (
                            session_id,
                            tenant_id,
                            repository_id,
                            principal_id,
                            agent_client_id,
                            trace_id,
                            run_id,
                            request_fingerprint,
                            idempotency_key,
                        ),
                    )
                    self._insert_revision(cursor, proposed)
            return SQLiteGateSessionCreateResult(
                session=proposed,
                inserted=True,
            )
        except (
            SQLiteGateSessionConflictError,
            SQLiteGateSessionSchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to create SQLite GateSession",
            )

    @_synchronized
    def get(self, session_id: str) -> GateSession:
        self._require_open()
        if type(session_id) is not str:
            raise ValueError("session_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._select_current(cursor, session_id)
        except (
            SQLiteGateSessionNotFoundError,
            SQLiteGateSessionSchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite GateSession",
            )

    @_synchronized
    def history(self, session_id: str) -> tuple[GateSession, ...]:
        self._require_open()
        if type(session_id) is not str:
            raise ValueError("session_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT revision.session_id, revision.version, "
                        "revision.status, revision.updated_at, "
                        "revision.expires_at, revision.lease_expires_at, "
                        "revision.payload, head.tenant_id, "
                        "head.repository_id, head.principal_id, "
                        "head.agent_client_id, head.trace_id, head.run_id, "
                        "head.request_fingerprint, head.idempotency_key "
                        "FROM gate_session_revisions AS revision "
                        "JOIN gate_session_heads AS head "
                        "ON head.session_id = revision.session_id "
                        "WHERE revision.session_id = ? ORDER BY version",
                        (session_id,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise SQLiteGateSessionNotFoundError(
                            "TBM_SQLITE_GATE_SESSION_NOT_FOUND",
                            "GateSession was not found",
                        )
                    return tuple(
                        self._session_from_joined_row(row) for row in rows
                    )
        except (
            SQLiteGateSessionNotFoundError,
            SQLiteGateSessionSchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite GateSession history",
            )

    @_synchronized
    def transition(
        self,
        session_id: str,
        target_status: GateSessionStatus,
        *,
        expected_version: int,
        lease_seconds: int | None = None,
        retrieval_snapshot_id: str | None = None,
        system_gate_evaluation_id: str | None = None,
        semantic_gate_attempt_ids: tuple[str, ...] | None = None,
        decision_id: str | None = None,
        final_memory_revision_ids: tuple[str, ...] | None = None,
        injection_artifact_id: str | None = None,
        usage_decision_id: str | None = None,
        run_outcome_id: str | None = None,
        terminal_reason: str | None = None,
    ) -> GateSession:
        self._require_open()
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    current = self._select_current(cursor, session_id)
                    now = self._trusted_after(current.updated_at)
                    self._require_live_transition(
                        current,
                        now,
                        target_status,
                    )
                    lease_expires_at = None
                    if lease_seconds is not None:
                        lease_expires_at = self._deadline(
                            now,
                            lease_seconds,
                            maximum=GATE_SESSION_MAX_LEASE_SECONDS,
                        )
                    next_session = transition_gate_session(
                        current,
                        target_status,
                        expected_version=expected_version,
                        updated_at=now,
                        lease_expires_at=lease_expires_at,
                        retrieval_snapshot_id=retrieval_snapshot_id,
                        system_gate_evaluation_id=(
                            system_gate_evaluation_id
                        ),
                        semantic_gate_attempt_ids=(
                            semantic_gate_attempt_ids
                        ),
                        decision_id=decision_id,
                        final_memory_revision_ids=(
                            final_memory_revision_ids
                        ),
                        injection_artifact_id=injection_artifact_id,
                        usage_decision_id=usage_decision_id,
                        run_outcome_id=run_outcome_id,
                        terminal_reason=terminal_reason,
                    )
                    self._append_revision(
                        cursor,
                        current,
                        next_session,
                        expected_version,
                    )
                    return next_session
        except (
            GateSessionContractError,
            SQLiteGateSessionNotFoundError,
            SQLiteGateSessionSchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to transition SQLite GateSession",
            )

    @_synchronized
    def renew_lease(
        self,
        session_id: str,
        *,
        expected_version: int,
        lease_seconds: int,
    ) -> GateSession:
        self._require_open()
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    current = self._select_current(cursor, session_id)
                    now = self._trusted_after(current.updated_at)
                    lease_expires_at = self._deadline(
                        now,
                        lease_seconds,
                        maximum=GATE_SESSION_MAX_LEASE_SECONDS,
                    )
                    next_session = renew_gate_session_lease(
                        current,
                        expected_version=expected_version,
                        updated_at=now,
                        lease_expires_at=lease_expires_at,
                    )
                    self._append_revision(
                        cursor,
                        current,
                        next_session,
                        expected_version,
                    )
                    return next_session
        except (
            GateSessionContractError,
            SQLiteGateSessionNotFoundError,
            SQLiteGateSessionSchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to renew SQLite GateSession lease",
            )

    @staticmethod
    def _require_live_transition(
        current: GateSession,
        now: str,
        target_status: GateSessionStatus,
    ) -> None:
        parsed_now = parse_rfc3339(now)
        if target_status == "expired":
            return
        if parsed_now >= parse_rfc3339(current.expires_at):
            raise SQLiteGateSessionConflictError(
                "TBM_SQLITE_GATE_SESSION_EXPIRED",
                "GateSession expiry has passed",
            )
        if (
            current.lease_expires_at is not None
            and parsed_now >= parse_rfc3339(current.lease_expires_at)
        ):
            raise SQLiteGateSessionConflictError(
                "TBM_SQLITE_GATE_SESSION_LEASE_EXPIRED",
                "GateSession lease has expired",
            )

    @staticmethod
    def _append_revision(
        cursor: sqlite3.Cursor,
        current: GateSession,
        next_session: GateSession,
        expected_version: int,
    ) -> None:
        SQLiteGateSessionRepository._insert_revision(cursor, next_session)
        cursor.execute(
            "UPDATE gate_session_heads SET current_version = ? "
            "WHERE session_id = ? AND current_version = ?",
            (
                next_session.version,
                current.session_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise GateSessionContractError(
                "TBM_GATE_SESSION_STALE_VERSION",
                "expected_version does not match the current session "
                "revision",
            )

    @_synchronized
    def list_due(self, *, limit: int = 100) -> tuple[GateSession, ...]:
        self._require_open()
        if type(limit) is not int or limit < 1 or limit > 10_000:
            raise ValueError("limit must be an integer from 1 through 10000")
        now = self._trusted_now()
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT revision.session_id, revision.version, "
                        "revision.status, revision.updated_at, "
                        "revision.expires_at, revision.lease_expires_at, "
                        "revision.payload, head.tenant_id, "
                        "head.repository_id, head.principal_id, "
                        "head.agent_client_id, head.trace_id, head.run_id, "
                        "head.request_fingerprint, head.idempotency_key "
                        "FROM gate_session_heads AS head "
                        "JOIN gate_session_revisions AS revision "
                        "ON revision.session_id = head.session_id "
                        "AND revision.version = head.current_version "
                        "WHERE revision.status IN ("
                        "'prepared', 'awaiting_decision', 'decided', "
                        "'finalized', 'executing'"
                        ") AND (revision.expires_at <= ? "
                        "OR revision.lease_expires_at <= ?) "
                        "ORDER BY revision.expires_at, "
                        "revision.lease_expires_at, revision.session_id "
                        "LIMIT ?",
                        (now, now, limit),
                    )
                    return tuple(
                        self._session_from_joined_row(row)
                        for row in cursor.fetchall()
                    )
        except SQLiteGateSessionSchemaError:
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to list due SQLite GateSessions",
            )

    @staticmethod
    def _raise_database_error(
        error: sqlite3.DatabaseError,
        message: str,
    ) -> NoReturn:
        if _is_schema_error(error):
            raise SQLiteGateSessionSchemaError(
                "TBM_SQLITE_GATE_SESSION_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise SQLiteGateSessionPersistenceError(
            "TBM_SQLITE_GATE_SESSION_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    @_synchronized
    def __enter__(self) -> "SQLiteGateSessionRepository":
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "GATE_SESSION_MAX_LEASE_SECONDS",
    "GATE_SESSION_MAX_TTL_SECONDS",
    "SQLITE_GATE_SESSION_SCHEMA_VERSION",
    "SQLiteGateSessionConflictError",
    "SQLiteGateSessionCreateResult",
    "SQLiteGateSessionError",
    "SQLiteGateSessionNotFoundError",
    "SQLiteGateSessionPersistenceError",
    "SQLiteGateSessionRepository",
    "SQLiteGateSessionSchemaError",
]
