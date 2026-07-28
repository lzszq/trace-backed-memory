from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
import sqlite3
from threading import RLock, local
from typing import Any, NoReturn, ParamSpec, TypeVar

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .completion_outbox_v3 import (
    COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION,
    COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION,
    CompletionOutboxContractError,
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    acknowledge_completion_outbox_delivery,
    build_completion_outbox_event,
    build_initial_completion_outbox_delivery,
    claim_completion_outbox_delivery,
    dumps_completion_outbox_delivery,
    dumps_completion_outbox_event,
    fail_completion_outbox_delivery,
    loads_completion_outbox_delivery,
    loads_completion_outbox_event,
    _validate_completion_outbox_claim,
    verify_completion_outbox_delivery_transition,
    verify_completion_outbox_event,
)
from .contracts_v3 import V3ContractError
from .gate_completion_v3 import GateCompletionRequest, GateCompletionResult
from .resources import PackagedResourceError, read_packaged_resource
from .sqlite_outcome_v3 import (
    SQLiteOutcomeV3ConflictError,
    SQLiteOutcomeV3NotFoundError,
    SQLiteOutcomeV3PersistenceError,
    SQLiteOutcomeV3Repository,
    SQLiteOutcomeV3SchemaError,
)


SQLITE_COMPLETION_OUTBOX_V3_SCHEMA_VERSION = 1
SQLITE_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE = 1000
_GATE_SCHEMA_RESOURCE = "schemas/sqlite-v3-gate-session.sql"
_OUTCOME_SCHEMA_RESOURCE = "schemas/sqlite-v3-outcome.sql"
_SCHEMA_RESOURCE = "schemas/sqlite-v3-completion-outbox.sql"
_MISSING_SCHEMA_MESSAGE = (
    "SQLite completion outbox v3 schema is missing or incomplete"
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_completion_outbox_schema",
    "v3_completion_outbox_delivery_heads",
    "v3_completion_outbox_delivery_heads_advance",
    "v3_completion_outbox_delivery_heads_no_delete",
    "v3_completion_outbox_delivery_heads_validate_insert",
    "v3_completion_outbox_delivery_revisions",
    "v3_completion_outbox_delivery_revisions_immutable_delete",
    "v3_completion_outbox_delivery_revisions_immutable_update",
    "v3_completion_outbox_delivery_revisions_validate_insert",
    "v3_completion_outbox_due",
    "v3_completion_outbox_events",
    "v3_completion_outbox_events_immutable_delete",
    "v3_completion_outbox_events_immutable_update",
    "v3_completion_outbox_events_validate_insert",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_MUTATION_CONTEXT = local()
_CONNECTION_LOCKS_GUARD = RLock()
_CONNECTION_LOCKS: dict[sqlite3.Connection, tuple[Any, int]] = {}


class SQLiteCompletionOutboxV3Error(V3ContractError):
    """Stable base failure for SQLite completion outbox operations."""


class SQLiteCompletionOutboxV3SchemaError(SQLiteCompletionOutboxV3Error):
    pass


class SQLiteCompletionOutboxV3ConflictError(SQLiteCompletionOutboxV3Error):
    pass


class SQLiteCompletionOutboxV3NotFoundError(SQLiteCompletionOutboxV3Error):
    pass


class SQLiteCompletionOutboxV3PersistenceError(
    SQLiteCompletionOutboxV3Error
):
    pass


@dataclass(frozen=True)
class SQLiteCompletionOutboxWrite:
    completion: GateCompletionResult
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery
    event_inserted: bool


@dataclass(frozen=True)
class SQLiteCompletionOutboxClaim:
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery


def _service_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mutation_depths() -> dict[int, int]:
    depths = getattr(_MUTATION_CONTEXT, "depths", None)
    if depths is None:
        depths = {}
        _MUTATION_CONTEXT.depths = depths
    return depths


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


def _synchronized(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _timestamp_microseconds(value: str) -> int:
    parsed = parse_rfc3339(value)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _event_row(event: CompletionOutboxEvent) -> tuple[object, ...]:
    return (
        event.event_id,
        event.event_type,
        event.tenant_id,
        event.repository_id,
        event.session_id,
        event.trace_id,
        event.run_id,
        event.usage_decision_id,
        event.run_outcome_id,
        event.outcome_descriptor_sha256,
        event.occurred_at,
        _timestamp_microseconds(event.occurred_at),
        dumps_completion_outbox_event(event),
    )


def _delivery_row(delivery: CompletionOutboxDelivery) -> tuple[object, ...]:
    return (
        delivery.event_id,
        delivery.version,
        delivery.delivery_revision_id,
        delivery.status,
        delivery.attempt_count,
        delivery.updated_at,
        _timestamp_microseconds(delivery.updated_at),
        delivery.available_at,
        (
            None
            if delivery.available_at is None
            else _timestamp_microseconds(delivery.available_at)
        ),
        delivery.worker_id,
        delivery.lease_expires_at,
        (
            None
            if delivery.lease_expires_at is None
            else _timestamp_microseconds(delivery.lease_expires_at)
        ),
        delivery.delivered_at,
        (
            None
            if delivery.delivered_at is None
            else _timestamp_microseconds(delivery.delivered_at)
        ),
        delivery.last_error_code,
        delivery.response_sha256,
        dumps_completion_outbox_delivery(delivery),
    )


def _event_is_canonical(*values: object) -> int:
    if len(values) != 13 or type(values[-1]) is not str:
        return 0
    try:
        event = loads_completion_outbox_event(values[-1])
        return int(values == _event_row(event))
    except (CompletionOutboxContractError, TypeError, ValueError):
        return 0


def _delivery_is_canonical(*values: object) -> int:
    if len(values) != 17 or type(values[-1]) is not str:
        return 0
    try:
        delivery = loads_completion_outbox_delivery(values[-1])
        return int(values == _delivery_row(delivery))
    except (CompletionOutboxContractError, TypeError, ValueError):
        return 0


def _transition_is_valid(
    previous_descriptor: object,
    current_descriptor: object,
) -> int:
    if (
        type(previous_descriptor) is not str
        or type(current_descriptor) is not str
    ):
        return 0
    try:
        previous = loads_completion_outbox_delivery(previous_descriptor)
        current = loads_completion_outbox_delivery(current_descriptor)
        verify_completion_outbox_delivery_transition(previous, current)
        return 1
    except (CompletionOutboxContractError, TypeError, ValueError):
        return 0


def _is_schema_error(error: sqlite3.DatabaseError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "no such table",
            "no such column",
            "no such function",
            "malformed database schema",
        )
    )


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteCompletionOutboxV3SchemaError(
            "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
            "SQLite completion outbox schema has an invalid definition",
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
        raise SQLiteCompletionOutboxV3SchemaError(
            "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
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
            raise SQLiteCompletionOutboxV3SchemaError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
                "SQLite completion outbox definition has invalid shape",
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE sql IS NOT NULL AND ("
        "tbl_name IN ("
        "'trace_backed_memory_v3_completion_outbox_schema', "
        "'v3_completion_outbox_events', "
        "'v3_completion_outbox_delivery_revisions', "
        "'v3_completion_outbox_delivery_heads'"
        ") OR name = 'trace_backed_memory_v3_completion_outbox_schema'"
        ") AND name NOT IN ("
        + placeholders
        + ") ORDER BY name",
        _SCHEMA_OBJECT_NAMES,
    )
    if cursor.fetchone() is not None:
        raise SQLiteCompletionOutboxV3SchemaError(
            "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
            "SQLite completion outbox schema contains an unexpected object",
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
                read_packaged_resource(_GATE_SCHEMA_RESOURCE).decode("utf-8")
            )
            connection.executescript(
                read_packaged_resource(_OUTCOME_SCHEMA_RESOURCE).decode(
                    "utf-8"
                )
            )
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
        raise SQLiteCompletionOutboxV3SchemaError(
            "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
            "could not validate canonical SQLite completion outbox schema",
        ) from error


class SQLiteCompletionOutboxV3Repository:
    """Atomic completion emission and durable SQLite delivery authority."""

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
        self._connection_identity = id(connection)
        try:
            if not self._connection.in_transaction:
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA recursive_triggers = ON")
            self._connection.create_function(
                "tbm_v3_completion_outbox_event_is_canonical",
                13,
                _event_is_canonical,
                deterministic=True,
            )
            self._connection.create_function(
                "tbm_v3_completion_outbox_delivery_is_canonical",
                17,
                _delivery_is_canonical,
                deterministic=True,
            )
            self._connection.create_function(
                "tbm_v3_completion_outbox_transition_is_valid",
                2,
                _transition_is_valid,
                deterministic=True,
            )
            self._connection.create_function(
                "tbm_v3_completion_outbox_mutation_allowed",
                0,
                lambda: int(
                    _mutation_depths().get(
                        self._connection_identity,
                        0,
                    )
                    > 0
                ),
            )
            foreign_keys = self._connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()
            recursive_triggers = self._connection.execute(
                "PRAGMA recursive_triggers"
            ).fetchone()
        except sqlite3.Error as error:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "could not initialize SQLite completion outbox connection",
            ) from error
        if foreign_keys != (1,):
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_FOREIGN_KEYS",
                "SQLite completion outbox requires foreign keys",
            )
        if recursive_triggers != (1,):
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_RECURSIVE_TRIGGERS",
                "SQLite completion outbox requires recursive triggers",
            )
        self._outcomes = SQLiteOutcomeV3Repository(
            connection,
            clock=clock,
        )
        self._lock = _acquire_connection_lock(connection)
        self._outcomes._lock = self._lock
        self._outcomes._gate_sessions._lock = self._lock

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        clock: Callable[[], str] = _service_timestamp,
        **kwargs: object,
    ) -> SQLiteCompletionOutboxV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                for resource in (
                    _GATE_SCHEMA_RESOURCE,
                    _OUTCOME_SCHEMA_RESOURCE,
                    _SCHEMA_RESOURCE,
                ):
                    connection.executescript(
                        read_packaged_resource(resource).decode("utf-8")
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
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "failed to connect to SQLite completion outbox storage",
            ) from error
        return cls(
            connection,
            owns_connection=True,
            clock=clock,
        )

    @property
    def outcomes(self) -> SQLiteOutcomeV3Repository:
        self._require_open()
        return self._outcomes

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_CLOSED",
                "SQLite completion outbox repository is closed",
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.Error as error:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_CLOSED",
                "SQLite completion outbox repository is closed",
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
                "failed to close unusable SQLite completion outbox "
                f"connection: {close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = (
                f"tbm_sqlite_completion_outbox_{self._savepoint_number}"
            )
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
                        "failed to clean up SQLite completion outbox "
                        f"savepoint {savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        error,
                        context="the outer completion outbox transaction",
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
                            "failed to clean up unreleased completion "
                            f"outbox savepoint: {cleanup_error}"
                        )
                        self._rollback_connection_or_close(
                            error,
                            context="the outer completion outbox transaction",
                        )
                    raise
            return
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the top-level completion outbox transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level completion outbox transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        try:
            self._outcomes._require_schema(cursor)
        except SQLiteOutcomeV3SchemaError as error:
            raise SQLiteCompletionOutboxV3SchemaError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
                "SQLite RunOutcome dependency failed schema validation",
            ) from error
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_completion_outbox_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or rows[0] != (
            SQLITE_COMPLETION_OUTBOX_V3_SCHEMA_VERSION,
            "tbm.completion-outbox.v3",
        ):
            raise SQLiteCompletionOutboxV3SchemaError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
                "SQLite completion outbox metadata does not match",
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteCompletionOutboxV3SchemaError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
                "SQLite completion outbox definitions do not match",
            )

    def _trusted_now(self, *, not_before: str | None = None) -> str:
        try:
            now = canonical_rfc3339(self._clock())
        except (TypeError, ValueError) as error:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_CLOCK",
                "trusted outbox clock returned an invalid timestamp",
            ) from error
        if (
            not_before is not None
            and parse_rfc3339(now) < parse_rfc3339(not_before)
        ):
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_CLOCK",
                "trusted outbox clock moved backwards",
            )
        return now

    @contextmanager
    def _allow_mutation(self) -> Iterator[None]:
        depths = _mutation_depths()
        identity = self._connection_identity
        depths[identity] = depths.get(identity, 0) + 1
        try:
            yield
        finally:
            remaining = depths[identity] - 1
            if remaining:
                depths[identity] = remaining
            else:
                del depths[identity]

    @staticmethod
    def _event_from_row(row: tuple[object, ...]) -> CompletionOutboxEvent:
        if len(row) != 13 or type(row[-1]) is not str:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "completion outbox event row has invalid shape",
            )
        try:
            event = loads_completion_outbox_event(row[-1])
        except CompletionOutboxContractError as error:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "stored completion outbox event failed validation",
            ) from error
        if row != _event_row(event):
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "completion outbox event columns do not match descriptor",
            )
        return event

    @staticmethod
    def _delivery_from_row(
        row: tuple[object, ...],
    ) -> CompletionOutboxDelivery:
        if len(row) != 17 or type(row[-1]) is not str:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "completion outbox delivery row has invalid shape",
            )
        try:
            delivery = loads_completion_outbox_delivery(row[-1])
        except CompletionOutboxContractError as error:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "stored completion outbox delivery failed validation",
            ) from error
        if row != _delivery_row(delivery):
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
                "completion outbox delivery columns do not match descriptor",
            )
        return delivery

    @staticmethod
    def _event_select() -> str:
        return (
            "SELECT event_id, event_type, tenant_id, repository_id, "
            "session_id, trace_id, run_id, usage_decision_id, "
            "run_outcome_id, outcome_descriptor_sha256, occurred_at, "
            "occurred_at_us, descriptor "
            "FROM v3_completion_outbox_events "
        )

    @staticmethod
    def _delivery_select() -> str:
        table = "v3_completion_outbox_delivery_revisions"
        return (
            f"SELECT {table}.event_id, {table}.version, "
            f"{table}.delivery_revision_id, {table}.status, "
            f"{table}.attempt_count, {table}.updated_at, "
            f"{table}.updated_at_us, {table}.available_at, "
            f"{table}.available_at_us, {table}.worker_id, "
            f"{table}.lease_expires_at, {table}.lease_expires_at_us, "
            f"{table}.delivered_at, {table}.delivered_at_us, "
            f"{table}.last_error_code, {table}.response_sha256, "
            f"{table}.descriptor FROM {table} "
        )

    @classmethod
    def _select_event(
        cls,
        cursor: sqlite3.Cursor,
        event_id: str,
    ) -> CompletionOutboxEvent:
        cursor.execute(cls._event_select() + "WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        if row is None:
            raise SQLiteCompletionOutboxV3NotFoundError(
                "TBM_SQLITE_COMPLETION_OUTBOX_NOT_FOUND",
                "completion outbox event was not found",
            )
        return cls._event_from_row(row)

    @classmethod
    def _select_event_by_outcome(
        cls,
        cursor: sqlite3.Cursor,
        run_outcome_id: str,
    ) -> CompletionOutboxEvent:
        cursor.execute(
            cls._event_select() + "WHERE run_outcome_id = ?",
            (run_outcome_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteCompletionOutboxV3NotFoundError(
                "TBM_SQLITE_COMPLETION_OUTBOX_NOT_FOUND",
                "completion outbox event was not found",
            )
        return cls._event_from_row(row)

    @classmethod
    def _select_current_delivery(
        cls,
        cursor: sqlite3.Cursor,
        event_id: str,
    ) -> CompletionOutboxDelivery:
        cursor.execute(
            cls._delivery_select()
            + "JOIN v3_completion_outbox_delivery_heads AS head "
            "ON head.event_id = "
            "v3_completion_outbox_delivery_revisions.event_id "
            "AND head.current_version = "
            "v3_completion_outbox_delivery_revisions.version "
            "WHERE v3_completion_outbox_delivery_revisions.event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteCompletionOutboxV3NotFoundError(
                "TBM_SQLITE_COMPLETION_OUTBOX_DELIVERY_NOT_FOUND",
                "completion outbox delivery head was not found",
            )
        return cls._delivery_from_row(row)

    @staticmethod
    def _insert_event(
        cursor: sqlite3.Cursor,
        event: CompletionOutboxEvent,
    ) -> None:
        cursor.execute(
            "INSERT INTO v3_completion_outbox_events ("
            "event_id, event_type, tenant_id, repository_id, session_id, "
            "trace_id, run_id, usage_decision_id, run_outcome_id, "
            "outcome_descriptor_sha256, occurred_at, occurred_at_us, "
            "descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _event_row(event),
        )

    @staticmethod
    def _insert_delivery(
        cursor: sqlite3.Cursor,
        delivery: CompletionOutboxDelivery,
    ) -> None:
        cursor.execute(
            "INSERT INTO v3_completion_outbox_delivery_revisions ("
            "event_id, version, delivery_revision_id, status, "
            "attempt_count, updated_at, updated_at_us, available_at, "
            "available_at_us, worker_id, lease_expires_at, "
            "lease_expires_at_us, delivered_at, delivered_at_us, "
            "last_error_code, response_sha256, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _delivery_row(delivery),
        )

    @classmethod
    def _insert_bundle(
        cls,
        cursor: sqlite3.Cursor,
        event: CompletionOutboxEvent,
        delivery: CompletionOutboxDelivery,
    ) -> None:
        cls._insert_event(cursor, event)
        cls._insert_delivery(cursor, delivery)
        cursor.execute(
            "INSERT INTO v3_completion_outbox_delivery_heads "
            "(event_id, current_version) VALUES (?, 1)",
            (event.event_id,),
        )

    @classmethod
    def _append_delivery(
        cls,
        cursor: sqlite3.Cursor,
        previous: CompletionOutboxDelivery,
        current: CompletionOutboxDelivery,
    ) -> None:
        verify_completion_outbox_delivery_transition(previous, current)
        cls._insert_delivery(cursor, current)
        cursor.execute(
            "UPDATE v3_completion_outbox_delivery_heads "
            "SET current_version = ? "
            "WHERE event_id = ? AND current_version = ?",
            (current.version, current.event_id, previous.version),
        )
        if cursor.rowcount != 1:
            raise SQLiteCompletionOutboxV3ConflictError(
                "TBM_SQLITE_COMPLETION_OUTBOX_STALE",
                "completion outbox delivery version is stale",
            )

    @_synchronized
    def complete_session(
        self,
        request: GateCompletionRequest,
    ) -> SQLiteCompletionOutboxWrite:
        self._require_open()
        if type(request) is not GateCompletionRequest:
            raise TypeError("request must be exactly GateCompletionRequest")
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    completion = self._outcomes.complete_session(request)
                    event = build_completion_outbox_event(
                        completion.outcome,
                        completion.session,
                    )
                    initial = build_initial_completion_outbox_delivery(event)
                    if completion.inserted:
                        with self._allow_mutation():
                            self._insert_bundle(cursor, event, initial)
                        event_inserted = True
                    else:
                        try:
                            retained_event = self._select_event_by_outcome(
                                cursor,
                                completion.outcome.run_outcome_id,
                            )
                        except SQLiteCompletionOutboxV3NotFoundError as error:
                            raise SQLiteCompletionOutboxV3PersistenceError(
                                "TBM_SQLITE_COMPLETION_OUTBOX_ORPHANED_OUTCOME",
                                "completed outcome has no outbox event",
                            ) from error
                        if retained_event != event:
                            raise SQLiteCompletionOutboxV3ConflictError(
                                "TBM_SQLITE_COMPLETION_OUTBOX_CONFLICT",
                                "outcome is linked to another outbox event",
                            )
                        event_inserted = False
                    retained_event = self._select_event(
                        cursor,
                        event.event_id,
                    )
                    retained_delivery = self._select_current_delivery(
                        cursor,
                        event.event_id,
                    )
                    if retained_event != event:
                        raise SQLiteCompletionOutboxV3PersistenceError(
                            "TBM_SQLITE_COMPLETION_OUTBOX_READBACK",
                            "completion outbox event read-back changed",
                        )
                    if event_inserted and retained_delivery != initial:
                        raise SQLiteCompletionOutboxV3PersistenceError(
                            "TBM_SQLITE_COMPLETION_OUTBOX_READBACK",
                            "initial outbox delivery read-back changed",
                        )
                    verify_completion_outbox_event(
                        retained_event,
                        completion.outcome,
                        completion.session,
                    )
                    self._require_schema(cursor)
                    return SQLiteCompletionOutboxWrite(
                        completion=completion,
                        event=retained_event,
                        delivery=retained_delivery,
                        event_inserted=event_inserted,
                    )
        except (
            CompletionOutboxContractError,
            SQLiteCompletionOutboxV3ConflictError,
            SQLiteCompletionOutboxV3NotFoundError,
            SQLiteCompletionOutboxV3PersistenceError,
            SQLiteCompletionOutboxV3SchemaError,
        ):
            raise
        except SQLiteOutcomeV3SchemaError as error:
            raise SQLiteCompletionOutboxV3SchemaError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
                "SQLite outcome dependency failed schema validation",
            ) from error
        except SQLiteOutcomeV3NotFoundError as error:
            raise SQLiteCompletionOutboxV3NotFoundError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SESSION_NOT_FOUND",
                "GateSession or RunOutcome was not found",
            ) from error
        except SQLiteOutcomeV3ConflictError as error:
            raise SQLiteCompletionOutboxV3ConflictError(
                "TBM_SQLITE_COMPLETION_OUTBOX_COMPLETION_CONFLICT",
                "GateSession completion conflicts with retained state",
            ) from error
        except SQLiteOutcomeV3PersistenceError as error:
            raise SQLiteCompletionOutboxV3PersistenceError(
                "TBM_SQLITE_COMPLETION_OUTBOX_DEPENDENCY",
                "SQLite outcome dependency failed during completion",
            ) from error
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to complete GateSession with an outbox event",
            )

    @_synchronized
    def get_event(self, event_id: str) -> CompletionOutboxEvent:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._select_event(cursor, event_id)
        except (
            SQLiteCompletionOutboxV3NotFoundError,
            SQLiteCompletionOutboxV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load completion outbox event",
            )

    @_synchronized
    def get_delivery(self, event_id: str) -> CompletionOutboxDelivery:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._select_current_delivery(cursor, event_id)
        except (
            SQLiteCompletionOutboxV3NotFoundError,
            SQLiteCompletionOutboxV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load completion outbox delivery",
            )

    @_synchronized
    def list_delivery_history(
        self,
        event_id: str,
    ) -> tuple[CompletionOutboxDelivery, ...]:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    self._select_event(cursor, event_id)
                    cursor.execute(
                        self._delivery_select()
                        + "WHERE event_id = ? ORDER BY version",
                        (event_id,),
                    )
                    rows = cursor.fetchall()
                    deliveries = tuple(
                        self._delivery_from_row(row) for row in rows
                    )
                    for previous, current in zip(
                        deliveries,
                        deliveries[1:],
                        strict=False,
                    ):
                        verify_completion_outbox_delivery_transition(
                            previous,
                            current,
                        )
                    return deliveries
        except (
            CompletionOutboxContractError,
            SQLiteCompletionOutboxV3NotFoundError,
            SQLiteCompletionOutboxV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to list completion outbox delivery history",
            )

    @_synchronized
    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int = 100,
    ) -> tuple[SQLiteCompletionOutboxClaim, ...]:
        self._require_open()
        _validate_completion_outbox_claim(worker_id, lease_seconds)
        if (
            type(limit) is not int
            or not 1 <= limit <= SQLITE_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE
        ):
            raise ValueError("limit is outside the supported range")
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    now = self._trusted_now()
                    now_us = _timestamp_microseconds(now)
                    cursor.execute(
                        self._delivery_select()
                        + "JOIN v3_completion_outbox_delivery_heads AS head "
                        "ON head.event_id = "
                        "v3_completion_outbox_delivery_revisions.event_id "
                        "AND head.current_version = "
                        "v3_completion_outbox_delivery_revisions.version "
                        "WHERE ("
                        "status IN ('pending', 'retry_wait') "
                        "AND available_at_us <= ?"
                        ") OR ("
                        "status = 'leased' AND lease_expires_at_us <= ?"
                        ") ORDER BY "
                        "COALESCE(available_at_us, lease_expires_at_us), "
                        "v3_completion_outbox_delivery_revisions.event_id "
                        "LIMIT ?",
                        (now_us, now_us, limit),
                    )
                    due = tuple(
                        self._delivery_from_row(row)
                        for row in cursor.fetchall()
                    )
                    claimed = tuple(
                        claim_completion_outbox_delivery(
                            delivery,
                            worker_id=worker_id,
                            claimed_at=now,
                            lease_seconds=lease_seconds,
                        )
                        for delivery in due
                    )
                    events = tuple(
                        self._select_event(cursor, delivery.event_id)
                        for delivery in claimed
                    )
                    with self._allow_mutation():
                        for previous, current in zip(
                            due,
                            claimed,
                            strict=True,
                        ):
                            self._append_delivery(cursor, previous, current)
                    retained = tuple(
                        self._select_current_delivery(
                            cursor,
                            delivery.event_id,
                        )
                        for delivery in claimed
                    )
                    if retained != claimed:
                        raise SQLiteCompletionOutboxV3PersistenceError(
                            "TBM_SQLITE_COMPLETION_OUTBOX_READBACK",
                            "claimed delivery read-back changed",
                        )
                    self._require_schema(cursor)
                    return tuple(
                        SQLiteCompletionOutboxClaim(event, delivery)
                        for event, delivery in zip(
                            events,
                            retained,
                            strict=True,
                        )
                    )
        except (
            CompletionOutboxContractError,
            SQLiteCompletionOutboxV3ConflictError,
            SQLiteCompletionOutboxV3PersistenceError,
            SQLiteCompletionOutboxV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to claim completion outbox deliveries",
            )

    @_synchronized
    def acknowledge(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        response_sha256: str | None = None,
    ) -> CompletionOutboxDelivery:
        return self._finish_delivery(
            event_id,
            expected_version=expected_version,
            operation=lambda current, now: (
                acknowledge_completion_outbox_delivery(
                    current,
                    worker_id=worker_id,
                    acknowledged_at=now,
                    response_sha256=response_sha256,
                )
            ),
            message="failed to acknowledge completion outbox delivery",
        )

    @_synchronized
    def fail_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> CompletionOutboxDelivery:
        return self._finish_delivery(
            event_id,
            expected_version=expected_version,
            operation=lambda current, now: fail_completion_outbox_delivery(
                current,
                worker_id=worker_id,
                failed_at=now,
                error_code=error_code,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            ),
            message="failed to record completion outbox delivery failure",
        )

    def _finish_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        operation: Callable[
            [CompletionOutboxDelivery, str],
            CompletionOutboxDelivery,
        ],
        message: str,
    ) -> CompletionOutboxDelivery:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    current = self._select_current_delivery(cursor, event_id)
                    if current.version != expected_version:
                        raise SQLiteCompletionOutboxV3ConflictError(
                            "TBM_SQLITE_COMPLETION_OUTBOX_STALE",
                            "completion outbox delivery version is stale",
                        )
                    now = self._trusted_now(not_before=current.updated_at)
                    updated = operation(current, now)
                    with self._allow_mutation():
                        self._append_delivery(cursor, current, updated)
                    retained = self._select_current_delivery(
                        cursor,
                        event_id,
                    )
                    if retained != updated:
                        raise SQLiteCompletionOutboxV3PersistenceError(
                            "TBM_SQLITE_COMPLETION_OUTBOX_READBACK",
                            "completion outbox delivery read-back changed",
                        )
                    self._require_schema(cursor)
                    return retained
        except (
            CompletionOutboxContractError,
            SQLiteCompletionOutboxV3ConflictError,
            SQLiteCompletionOutboxV3NotFoundError,
            SQLiteCompletionOutboxV3PersistenceError,
            SQLiteCompletionOutboxV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, message)

    @staticmethod
    def _raise_database_error(
        error: sqlite3.DatabaseError,
        message: str,
    ) -> NoReturn:
        if _is_schema_error(error):
            raise SQLiteCompletionOutboxV3SchemaError(
                "TBM_SQLITE_COMPLETION_OUTBOX_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise SQLiteCompletionOutboxV3PersistenceError(
            "TBM_SQLITE_COMPLETION_OUTBOX_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outcomes.close()
        _release_connection_lock(self._connection)
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteCompletionOutboxV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "COMPLETION_OUTBOX_DELIVERY_CONTRACT_VERSION",
    "COMPLETION_OUTBOX_EVENT_CONTRACT_VERSION",
    "SQLITE_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE",
    "SQLITE_COMPLETION_OUTBOX_V3_SCHEMA_VERSION",
    "SQLiteCompletionOutboxClaim",
    "SQLiteCompletionOutboxV3ConflictError",
    "SQLiteCompletionOutboxV3Error",
    "SQLiteCompletionOutboxV3NotFoundError",
    "SQLiteCompletionOutboxV3PersistenceError",
    "SQLiteCompletionOutboxV3Repository",
    "SQLiteCompletionOutboxV3SchemaError",
    "SQLiteCompletionOutboxWrite",
]
