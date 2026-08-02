from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from ._timestamps import (
    aware_datetime_to_rfc3339,
    canonical_rfc3339,
    parse_rfc3339,
)
from .contracts_v3 import V3ContractError
from .gate_completion_v3 import (
    GateCompletionRequest,
    GateCompletionResult,
)
from .gate_session_v3 import (
    GateSession,
    GateSessionContractError,
    transition_gate_session,
)
from .outcome_v3 import (
    RUN_OUTCOME_CONTRACT_VERSION,
    OutcomeContractError,
    RunOutcome,
    build_run_outcome,
    dumps_run_outcome,
    loads_run_outcome,
    verify_run_outcome,
)
from .outcome_effect_event_v1 import (
    OutcomeCompletionEventSink,
    OutcomeEffectViews,
)
from .resources import PackagedResourceError, read_packaged_resource
from .sqlite_gate_session_v3 import (
    SQLITE_GATE_SESSION_SCHEMA_VERSION,
    SQLiteGateSessionConflictError,
    SQLiteGateSessionNotFoundError,
    SQLiteGateSessionPersistenceError,
    SQLiteGateSessionRepository,
    SQLiteGateSessionSchemaError,
)


SQLITE_OUTCOME_V3_SCHEMA_VERSION = 1
_GATE_SESSION_SCHEMA_RESOURCE = "schemas/sqlite-v3-gate-session.sql"
_SCHEMA_RESOURCE = "schemas/sqlite-v3-outcome.sql"
_MISSING_SCHEMA_MESSAGE = "SQLite RunOutcome v3 schema is missing or incomplete"
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_outcome_schema",
    "v3_run_outcomes",
    "v3_run_outcomes_immutable_delete",
    "v3_run_outcomes_immutable_update",
    "v3_run_outcomes_validate_insert",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteOutcomeV3Error(V3ContractError):
    """Stable base failure for SQLite outcome/session completion."""


class SQLiteOutcomeV3SchemaError(SQLiteOutcomeV3Error):
    pass


class SQLiteOutcomeV3ConflictError(SQLiteOutcomeV3Error):
    pass


class SQLiteOutcomeV3NotFoundError(SQLiteOutcomeV3Error):
    pass


class SQLiteOutcomeV3PersistenceError(SQLiteOutcomeV3Error):
    pass


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
        raise SQLiteOutcomeV3SchemaError(
            "TBM_SQLITE_OUTCOME_SCHEMA",
            "SQLite RunOutcome schema contains an invalid definition",
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
        raise SQLiteOutcomeV3SchemaError(
            "TBM_SQLITE_OUTCOME_SCHEMA",
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
            raise SQLiteOutcomeV3SchemaError(
                "TBM_SQLITE_OUTCOME_SCHEMA",
                "SQLite RunOutcome schema definition has an invalid shape",
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE sql IS NOT NULL AND ("
        "tbl_name IN ("
        "'trace_backed_memory_v3_outcome_schema', "
        "'v3_run_outcomes'"
        ") OR name = 'trace_backed_memory_v3_outcome_schema'"
        ") AND name NOT IN ("
        + placeholders
        + ") ORDER BY name",
        _SCHEMA_OBJECT_NAMES,
    )
    if cursor.fetchone() is not None:
        raise SQLiteOutcomeV3SchemaError(
            "TBM_SQLITE_OUTCOME_SCHEMA",
            "SQLite RunOutcome schema contains an unexpected object",
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
                    _GATE_SESSION_SCHEMA_RESOURCE
                ).decode("utf-8")
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
        raise SQLiteOutcomeV3SchemaError(
            "TBM_SQLITE_OUTCOME_SCHEMA",
            "could not validate the canonical SQLite RunOutcome schema",
        ) from error


class SQLiteOutcomeV3Repository:
    """Atomic SQLite RunOutcome append and GateSession completion authority."""

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
        self._completion_event_sink: OutcomeCompletionEventSink | None = None
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
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_PERSISTENCE",
                "could not enforce SQLite RunOutcome foreign keys",
            ) from error
        if foreign_keys != (1,):
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_FOREIGN_KEYS",
                "SQLite RunOutcome repository requires foreign keys",
            )
        if recursive_triggers != (1,):
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_RECURSIVE_TRIGGERS",
                "SQLite RunOutcome repository requires recursive triggers",
            )
        self._gate_sessions = SQLiteGateSessionRepository(
            connection,
            clock=clock,
            allow_direct_completion=False,
        )
        self._gate_sessions._lock = self._lock

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        clock: Callable[[], str] = _service_timestamp,
        **kwargs: object,
    ) -> "SQLiteOutcomeV3Repository":
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        _GATE_SESSION_SCHEMA_RESOURCE
                    ).decode("utf-8")
                )
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
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_PERSISTENCE",
                "failed to connect to SQLite RunOutcome storage",
            ) from error
        return cls(connection, owns_connection=True, clock=clock)

    @property
    def gate_sessions(self) -> SQLiteGateSessionRepository:
        """Return the shared authority for setup and non-completion transitions."""

        self._require_open()
        return self._gate_sessions

    @_synchronized
    def bind_completion_event_sink(
        self,
        sink: OutcomeCompletionEventSink,
    ) -> None:
        """Bind the event-first sink invoked before completion projections."""

        self._require_open()
        if not callable(getattr(sink, "append_completion", None)):
            raise ValueError("completion event sink is invalid")
        if (
            self._completion_event_sink is not None
            and self._completion_event_sink is not sink
        ):
            raise SQLiteOutcomeV3ConflictError(
                "TBM_SQLITE_OUTCOME_EVENT_SINK_BOUND",
                "RunOutcome completion event sink is already bound",
            )
        self._completion_event_sink = sink

    def _project_completion(
        self,
        outcome: RunOutcome,
        session: GateSession,
    ) -> None:
        sink = self._completion_event_sink
        if sink is None:
            return
        views = sink.append_completion(outcome, session)
        if type(views) is not OutcomeEffectViews or views.run_outcome != outcome:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_EVENT_PROJECTION",
                "Outcome event projection did not reproduce RunOutcome",
            )

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_CLOSED",
                "SQLite RunOutcome repository is closed",
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.Error as error:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_CLOSED",
                "SQLite RunOutcome repository is closed",
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
                "failed to close unusable SQLite RunOutcome connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_outcome_{self._savepoint_number}"
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
                        "failed to clean up SQLite RunOutcome savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        error,
                        context="the outer SQLite RunOutcome transaction",
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
                            f"RunOutcome savepoint: {cleanup_error}"
                        )
                        self._rollback_connection_or_close(
                            error,
                            context="the outer SQLite RunOutcome transaction",
                        )
                    raise
            return
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the top-level SQLite RunOutcome transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level SQLite RunOutcome transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        try:
            self._gate_sessions._require_schema(cursor)
        except SQLiteGateSessionSchemaError as error:
            raise SQLiteOutcomeV3SchemaError(
                "TBM_SQLITE_OUTCOME_SCHEMA",
                "SQLite GateSession dependency failed schema validation",
            ) from error
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_outcome_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or rows[0] != (
            SQLITE_OUTCOME_V3_SCHEMA_VERSION,
            RUN_OUTCOME_CONTRACT_VERSION,
        ):
            raise SQLiteOutcomeV3SchemaError(
                "TBM_SQLITE_OUTCOME_SCHEMA",
                "SQLite RunOutcome schema metadata does not match "
                "the supported contract",
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteOutcomeV3SchemaError(
                "TBM_SQLITE_OUTCOME_SCHEMA",
                "SQLite RunOutcome schema definitions do not match "
                "the canonical version",
            )

    def _trusted_after(self, previous: str) -> str:
        try:
            now = canonical_rfc3339(self._clock())
        except (TypeError, ValueError) as error:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_CLOCK",
                "trusted completion clock returned an invalid timestamp",
            ) from error
        parsed_now = parse_rfc3339(now)
        parsed_previous = parse_rfc3339(previous)
        if parsed_now < parsed_previous:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_CLOCK",
                "trusted completion clock moved backwards",
            )
        if parsed_now == parsed_previous:
            try:
                advanced = parsed_previous + timedelta(seconds=1)
            except OverflowError as error:
                raise SQLiteOutcomeV3PersistenceError(
                    "TBM_SQLITE_OUTCOME_CLOCK",
                    "trusted completion clock exceeds the supported range",
                ) from error
            return aware_datetime_to_rfc3339(advanced)
        return now

    @staticmethod
    def _outcome_row(outcome: RunOutcome) -> tuple[object, ...]:
        evidence_json = json.dumps(
            list(outcome.evidence_artifact_sha256s),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        cost_json = json.dumps(
            outcome.cost_usd,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return (
            outcome.run_outcome_id,
            outcome.session_id,
            outcome.trace_id,
            outcome.run_id,
            outcome.usage_decision_id,
            outcome.result,
            outcome.evaluator_id,
            outcome.evaluator_version,
            outcome.output_sha256,
            outcome.tool_outputs_sha256,
            evidence_json,
            outcome.latency_ms,
            cost_json,
            outcome.error_code,
            outcome.measured_at,
            dumps_run_outcome(outcome),
        )

    @staticmethod
    def _outcome_from_row(row: tuple[object, ...]) -> RunOutcome:
        if len(row) != 16 or type(row[15]) is not str:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_PERSISTENCE",
                "RunOutcome row has an invalid shape",
            )
        try:
            outcome = loads_run_outcome(row[15])
        except OutcomeContractError as error:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_PERSISTENCE",
                "stored RunOutcome failed contract validation",
            ) from error
        if row != SQLiteOutcomeV3Repository._outcome_row(outcome):
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_PERSISTENCE",
                "RunOutcome columns do not match the canonical descriptor",
            )
        return outcome

    @staticmethod
    def _select_outcome(
        cursor: sqlite3.Cursor,
        run_outcome_id: str,
    ) -> RunOutcome:
        cursor.execute(
            "SELECT run_outcome_id, session_id, trace_id, run_id, "
            "usage_decision_id, result, evaluator_id, evaluator_version, "
            "output_sha256, tool_outputs_sha256, "
            "evidence_artifact_sha256s_json, latency_ms, cost_usd_json, "
            "error_code, measured_at, descriptor "
            "FROM v3_run_outcomes WHERE run_outcome_id = ?",
            (run_outcome_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteOutcomeV3NotFoundError(
                "TBM_SQLITE_OUTCOME_NOT_FOUND",
                "RunOutcome was not found",
            )
        return SQLiteOutcomeV3Repository._outcome_from_row(row)

    @staticmethod
    def _insert_outcome(
        cursor: sqlite3.Cursor,
        outcome: RunOutcome,
    ) -> None:
        cursor.execute(
            "INSERT INTO v3_run_outcomes ("
            "run_outcome_id, session_id, trace_id, run_id, "
            "usage_decision_id, result, evaluator_id, evaluator_version, "
            "output_sha256, tool_outputs_sha256, "
            "evidence_artifact_sha256s_json, latency_ms, cost_usd_json, "
            "error_code, measured_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            SQLiteOutcomeV3Repository._outcome_row(outcome),
        )

    @staticmethod
    def _matches_request(
        outcome: RunOutcome,
        request: GateCompletionRequest,
    ) -> bool:
        return (
            outcome.result == request.result
            and outcome.evaluator_id == request.evaluator_id
            and outcome.evaluator_version == request.evaluator_version
            and outcome.evidence_artifact_sha256s
            == request.evidence_artifact_sha256s
            and outcome.output_sha256 == request.output_sha256
            and outcome.tool_outputs_sha256 == request.tool_outputs_sha256
            and outcome.latency_ms == request.latency_ms
            and outcome.cost_usd == request.cost_usd
            and outcome.error_code == request.error_code
        )

    @_synchronized
    def complete_session(
        self,
        request: GateCompletionRequest,
    ) -> GateCompletionResult:
        self._require_open()
        if type(request) is not GateCompletionRequest:
            raise TypeError("request must be exactly GateCompletionRequest")
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    current = self._gate_sessions._select_current(
                        cursor,
                        request.session_id,
                    )
                    if current.status == "completed":
                        if current.run_outcome_id is None:
                            raise SQLiteOutcomeV3PersistenceError(
                                "TBM_SQLITE_OUTCOME_PERSISTENCE",
                                "completed GateSession has no RunOutcome",
                            )
                        try:
                            existing = self._select_outcome(
                                cursor,
                                current.run_outcome_id,
                            )
                        except SQLiteOutcomeV3NotFoundError as error:
                            raise SQLiteOutcomeV3PersistenceError(
                                "TBM_SQLITE_OUTCOME_ORPHANED_SESSION",
                                "completed GateSession has no retained RunOutcome",
                            ) from error
                        if not self._matches_request(existing, request):
                            raise SQLiteOutcomeV3ConflictError(
                                "TBM_SQLITE_OUTCOME_COMPLETION_CONFLICT",
                                "GateSession is completed with another outcome",
                            )
                        try:
                            verify_run_outcome(existing, current)
                        except OutcomeContractError as error:
                            raise SQLiteOutcomeV3PersistenceError(
                                "TBM_SQLITE_OUTCOME_ORPHANED_SESSION",
                                "completed GateSession has inconsistent "
                                "RunOutcome linkage",
                            ) from error
                        self._project_completion(existing, current)
                        return GateCompletionResult(
                            session=current,
                            outcome=existing,
                            inserted=False,
                        )
                    if current.status != "executing":
                        raise SQLiteOutcomeV3ConflictError(
                            "TBM_SQLITE_OUTCOME_SESSION_STATE",
                            "RunOutcome requires an executing GateSession",
                        )
                    measured_at = self._trusted_after(current.updated_at)
                    self._gate_sessions._require_live_transition(
                        current,
                        measured_at,
                        "completed",
                    )
                    usage_decision_id = cast(
                        str,
                        current.usage_decision_id,
                    )
                    outcome = build_run_outcome(
                        session_id=current.session_id,
                        trace_id=current.trace_id,
                        run_id=current.run_id,
                        usage_decision_id=usage_decision_id,
                        result=request.result,
                        evaluator_id=request.evaluator_id,
                        evaluator_version=request.evaluator_version,
                        evidence_artifact_sha256s=(
                            request.evidence_artifact_sha256s
                        ),
                        measured_at=measured_at,
                        output_sha256=request.output_sha256,
                        tool_outputs_sha256=request.tool_outputs_sha256,
                        latency_ms=request.latency_ms,
                        cost_usd=request.cost_usd,
                        error_code=request.error_code,
                    )
                    completed = transition_gate_session(
                        current,
                        "completed",
                        expected_version=request.expected_version,
                        updated_at=measured_at,
                        run_outcome_id=outcome.run_outcome_id,
                    )
                    verify_run_outcome(outcome, completed)
                    self._project_completion(outcome, completed)
                    self._gate_sessions._append_revision(
                        cursor,
                        current,
                        completed,
                        request.expected_version,
                    )
                    self._insert_outcome(cursor, outcome)
                    retained_session = self._gate_sessions._select_current(
                        cursor,
                        current.session_id,
                    )
                    retained_outcome = self._select_outcome(
                        cursor,
                        outcome.run_outcome_id,
                    )
                    if (
                        retained_session != completed
                        or retained_outcome != outcome
                    ):
                        raise SQLiteOutcomeV3PersistenceError(
                            "TBM_SQLITE_OUTCOME_READBACK",
                            "RunOutcome completion read-back changed",
                        )
                    verify_run_outcome(retained_outcome, retained_session)
                    return GateCompletionResult(
                        session=retained_session,
                        outcome=retained_outcome,
                        inserted=True,
                    )
        except (
            GateSessionContractError,
            OutcomeContractError,
            SQLiteOutcomeV3ConflictError,
            SQLiteOutcomeV3NotFoundError,
            SQLiteOutcomeV3PersistenceError,
            SQLiteOutcomeV3SchemaError,
        ):
            raise
        except SQLiteGateSessionNotFoundError as error:
            raise SQLiteOutcomeV3NotFoundError(
                "TBM_SQLITE_OUTCOME_SESSION_NOT_FOUND",
                "GateSession was not found",
            ) from error
        except SQLiteGateSessionConflictError as error:
            raise SQLiteOutcomeV3ConflictError(
                "TBM_SQLITE_OUTCOME_SESSION_CONFLICT",
                "GateSession cannot be completed in its current time window",
            ) from error
        except SQLiteGateSessionPersistenceError as error:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_DEPENDENCY",
                "SQLite GateSession dependency failed during completion",
            ) from error
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to complete SQLite GateSession with RunOutcome",
            )

    @_synchronized
    def get_session(self, session_id: str) -> GateSession:
        self._require_open()
        if type(session_id) is not str:
            raise ValueError("session_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._gate_sessions._select_current(
                        cursor,
                        session_id,
                    )
        except SQLiteGateSessionNotFoundError as error:
            raise SQLiteOutcomeV3NotFoundError(
                "TBM_SQLITE_OUTCOME_SESSION_NOT_FOUND",
                "GateSession was not found",
            ) from error
        except SQLiteGateSessionPersistenceError as error:
            raise SQLiteOutcomeV3PersistenceError(
                "TBM_SQLITE_OUTCOME_DEPENDENCY",
                "SQLite GateSession dependency failed during read-back",
            ) from error
        except SQLiteOutcomeV3SchemaError:
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite GateSession for RunOutcome",
            )

    @_synchronized
    def get_outcome(self, run_outcome_id: str) -> RunOutcome:
        self._require_open()
        if type(run_outcome_id) is not str:
            raise ValueError("run_outcome_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._select_outcome(cursor, run_outcome_id)
        except (
            SQLiteOutcomeV3NotFoundError,
            SQLiteOutcomeV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite RunOutcome",
            )

    @staticmethod
    def _raise_database_error(
        error: sqlite3.DatabaseError,
        message: str,
    ) -> NoReturn:
        if _is_schema_error(error):
            raise SQLiteOutcomeV3SchemaError(
                "TBM_SQLITE_OUTCOME_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise SQLiteOutcomeV3PersistenceError(
            "TBM_SQLITE_OUTCOME_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate_sessions.close()
        if self._owns_connection:
            self._connection.close()

    @_synchronized
    def __enter__(self) -> "SQLiteOutcomeV3Repository":
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "SQLITE_GATE_SESSION_SCHEMA_VERSION",
    "SQLITE_OUTCOME_V3_SCHEMA_VERSION",
    "SQLiteOutcomeV3ConflictError",
    "SQLiteOutcomeV3Error",
    "SQLiteOutcomeV3NotFoundError",
    "SQLiteOutcomeV3PersistenceError",
    "SQLiteOutcomeV3Repository",
    "SQLiteOutcomeV3SchemaError",
]
