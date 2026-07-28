from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from .gate_evaluation_v3 import (
    GATE_EVALUATION_JSON_MAX_BYTES,
    GATE_EVALUATION_MAX_DECISIONS,
    GateEvaluationContractError,
    SemanticGateAttempt,
    SystemGateEvaluation,
    dumps_semantic_gate_attempt,
    loads_semantic_gate_attempt,
    loads_system_gate_evaluation,
    verify_semantic_gate_attempt,
    verify_semantic_gate_attempt_chain,
    verify_semantic_gate_attempt_parent,
    verify_system_gate_evaluation,
)
from .resources import PackagedResourceError, read_packaged_resource
from .retrieval_v3 import RetrievalSnapshot, loads_retrieval_snapshot


SQLITE_SEMANTIC_GATE_V3_SCHEMA_VERSION = 1
_MISSING_SCHEMA_MESSAGE = (
    "SQLite semantic Gate v3 schema is missing or incomplete"
)
_ATTEMPT_ID_RE = re.compile(r"semantic_attempt_sha256_[0-9a-f]{64}")
_SYSTEM_ID_RE = re.compile(r"system_gate_sha256_[0-9a-f]{64}")
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_gate_evidence_schema",
    "trace_backed_memory_v3_semantic_gate_schema",
    "v3_retrieval_snapshots",
    "v3_retrieval_snapshots_immutable_delete",
    "v3_retrieval_snapshots_immutable_update",
    "v3_retrieval_snapshots_session",
    "v3_semantic_gate_attempt_heads",
    "v3_semantic_gate_attempts",
    "v3_semantic_gate_attempts_extend_head",
    "v3_semantic_gate_attempts_immutable_delete",
    "v3_semantic_gate_attempts_immutable_insert_conflict",
    "v3_semantic_gate_attempts_immutable_update",
    "v3_semantic_gate_attempts_parent_scope",
    "v3_semantic_gate_attempts_session",
    "v3_semantic_gate_heads_advance",
    "v3_semantic_gate_heads_identity_immutable",
    "v3_semantic_gate_heads_immutable_delete",
    "v3_semantic_gate_heads_immutable_insert_conflict",
    "v3_semantic_gate_heads_parent_scope",
    "v3_semantic_gate_schema_requires_evidence",
    "v3_system_gate_evaluations",
    "v3_system_gate_evaluations_immutable_delete",
    "v3_system_gate_evaluations_immutable_update",
    "v3_system_gate_evaluations_parent_match",
    "v3_system_gate_evaluations_session",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteSemanticGateV3Error(RuntimeError):
    pass


class SQLiteSemanticGateV3SchemaError(SQLiteSemanticGateV3Error):
    pass


class SQLiteSemanticGateV3ConflictError(SQLiteSemanticGateV3Error):
    pass


class SQLiteSemanticGateV3NotFoundError(SQLiteSemanticGateV3Error):
    pass


class SQLiteSemanticGateV3PersistenceError(SQLiteSemanticGateV3Error):
    pass


@dataclass(frozen=True)
class SQLiteSemanticGateV3StoreResult:
    attempt_id: str
    sequence: int
    inserted: bool


@dataclass(frozen=True)
class _AttemptHead:
    system_gate_evaluation_id: str
    session_id: str
    retrieval_snapshot_id: str
    current_sequence: int
    current_attempt_id: str | None


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteSemanticGateV3SchemaError(
            "SQLite semantic Gate v3 schema contains an invalid definition"
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
        raise SQLiteSemanticGateV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteSemanticGateV3SchemaError(
                "SQLite semantic Gate v3 schema definition has invalid shape"
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
            for resource in (
                "schemas/sqlite-v3-gate-evidence.sql",
                "schemas/sqlite-v3-semantic-gate.sql",
            ):
                connection.executescript(
                    read_packaged_resource(resource).decode("utf-8")
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
        raise SQLiteSemanticGateV3SchemaError(
            "could not validate canonical SQLite semantic Gate v3 schema"
        ) from error


class SQLiteSemanticGateV3Repository:
    """Immutable SQLite SemanticGateAttempt chain beside Gate evidence v3."""

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
    ) -> SQLiteSemanticGateV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                for resource in (
                    "schemas/sqlite-v3-gate-evidence.sql",
                    "schemas/sqlite-v3-semantic-gate.sql",
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
            raise SQLiteSemanticGateV3PersistenceError(
                "failed to connect to SQLite semantic Gate v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteSemanticGateV3Error(
                "SQLite semantic Gate v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteSemanticGateV3Error(
                "SQLite semantic Gate v3 repository is closed"
            ) from error

    def _rollback_connection_or_close(
        self,
        primary_error: BaseException,
        *,
        context: str,
    ) -> None:
        try:
            self._connection.rollback()
        except BaseException as cleanup_error:
            primary_error.add_note(
                f"failed to roll back {context}: {cleanup_error}"
            )
        if not self._connection.in_transaction:
            return
        primary_error.add_note(f"rollback attempt left {context} active")
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite semantic Gate v3 "
                f"connection: {close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = (
                f"tbm_sqlite_semantic_gate_v3_{self._savepoint_number}"
            )
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
                        "failed to clean up SQLite semantic Gate v3 "
                        f"savepoint {savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after semantic "
                            "Gate v3 savepoint cleanup failed"
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
                context="the top-level SQLite semantic Gate v3 transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context=(
                        "the top-level SQLite semantic Gate v3 transaction"
                    ),
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteSemanticGateV3SchemaError(
                "SQLite semantic Gate v3 requires foreign keys"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteSemanticGateV3SchemaError(
                "SQLite semantic Gate v3 requires recursive triggers"
            )
        cursor.execute(
            "SELECT schema_version "
            "FROM trace_backed_memory_v3_gate_evidence_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [(1,)]:
            raise SQLiteSemanticGateV3SchemaError(
                "SQLite gate evidence v3 metadata mismatch"
            )
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_semantic_gate_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [
            (1, "tbm.semantic-gate-attempt.v3")
        ]:
            raise SQLiteSemanticGateV3SchemaError(
                "SQLite semantic Gate v3 metadata mismatch"
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteSemanticGateV3SchemaError(
                "SQLite semantic Gate v3 schema definitions do not match "
                "the canonical version"
            )

    @staticmethod
    def _attempt_row(attempt: SemanticGateAttempt) -> tuple[object, ...]:
        descriptor = dumps_semantic_gate_attempt(attempt)
        if (
            len(descriptor.encode("utf-8"))
            > GATE_EVALUATION_JSON_MAX_BYTES
        ):
            raise ValueError("semantic Gate descriptor exceeds storage limit")
        return (
            attempt.attempt_id,
            attempt.session_id,
            attempt.retrieval_snapshot_id,
            attempt.system_gate_evaluation_id,
            attempt.sequence,
            attempt.previous_attempt_id,
            attempt.status,
            attempt.started_at,
            attempt.finished_at,
            descriptor,
        )

    @staticmethod
    def _attempt_from_row(row: tuple[object, ...]) -> SemanticGateAttempt:
        if len(row) != 10 or type(row[9]) is not str:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate attempt row has invalid shape"
            )
        try:
            attempt = loads_semantic_gate_attempt(row[9])
        except GateEvaluationContractError as error:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate attempt descriptor failed validation"
            ) from error
        if row != SQLiteSemanticGateV3Repository._attempt_row(attempt):
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate columns do not match descriptor"
            )
        return attempt

    @staticmethod
    def _head_from_row(row: tuple[object, ...]) -> _AttemptHead:
        if (
            len(row) != 5
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
            or type(row[3]) is not int
            or (row[4] is not None and type(row[4]) is not str)
        ):
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate head row has invalid shape"
            )
        return _AttemptHead(
            system_gate_evaluation_id=row[0],
            session_id=row[1],
            retrieval_snapshot_id=row[2],
            current_sequence=row[3],
            current_attempt_id=row[4],
        )

    @staticmethod
    def _evaluation_from_row(
        row: tuple[object, ...],
    ) -> SystemGateEvaluation:
        if len(row) != 5 or type(row[4]) is not str:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored System Gate row has invalid shape"
            )
        try:
            evaluation = loads_system_gate_evaluation(row[4])
        except GateEvaluationContractError as error:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored System Gate descriptor failed validation"
            ) from error
        if row != (
            evaluation.evaluation_id,
            evaluation.session_id,
            evaluation.retrieval_snapshot_id,
            evaluation.authorization_event_id,
            row[4],
        ):
            raise SQLiteSemanticGateV3PersistenceError(
                "stored System Gate columns do not match descriptor"
            )
        return evaluation

    @staticmethod
    def _snapshot_from_row(row: tuple[object, ...]) -> RetrievalSnapshot:
        if len(row) != 4 or type(row[3]) is not str:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored retrieval snapshot row has invalid shape"
            )
        try:
            snapshot = loads_retrieval_snapshot(row[3])
        except ValueError as error:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored retrieval snapshot descriptor failed validation"
            ) from error
        if row != (
            snapshot.snapshot_id,
            snapshot.session_id,
            snapshot.authorization_event_id,
            row[3],
        ):
            raise SQLiteSemanticGateV3PersistenceError(
                "stored retrieval snapshot columns do not match descriptor"
            )
        return snapshot

    def _load_gate_records(
        self,
        cursor: sqlite3.Cursor,
        evaluation_id: str,
    ) -> tuple[SystemGateEvaluation, RetrievalSnapshot]:
        cursor.execute(
            "SELECT evaluation_id, session_id, retrieval_snapshot_id, "
            "authorization_event_id, descriptor "
            "FROM v3_system_gate_evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteSemanticGateV3NotFoundError(
                "System Gate evaluation was not found"
            )
        evaluation = self._evaluation_from_row(row)
        cursor.execute(
            "SELECT snapshot_id, session_id, authorization_event_id, "
            "descriptor FROM v3_retrieval_snapshots WHERE snapshot_id = ?",
            (evaluation.retrieval_snapshot_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteSemanticGateV3PersistenceError(
                "System Gate evaluation references a missing snapshot"
            )
        snapshot = self._snapshot_from_row(row)
        try:
            verify_system_gate_evaluation(evaluation, snapshot)
        except GateEvaluationContractError as error:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Gate evidence failed cross-record validation"
            ) from error
        return evaluation, snapshot

    @staticmethod
    def _select_head(
        cursor: sqlite3.Cursor,
        evaluation_id: str,
    ) -> _AttemptHead | None:
        cursor.execute(
            "SELECT system_gate_evaluation_id, session_id, "
            "retrieval_snapshot_id, current_sequence, current_attempt_id "
            "FROM v3_semantic_gate_attempt_heads "
            "WHERE system_gate_evaluation_id = ?",
            (evaluation_id,),
        )
        row = cursor.fetchone()
        return None if row is None else SQLiteSemanticGateV3Repository._head_from_row(row)

    @staticmethod
    def _select_attempt(
        cursor: sqlite3.Cursor,
        attempt_id: str,
    ) -> SemanticGateAttempt | None:
        cursor.execute(
            "SELECT attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor "
            "FROM v3_semantic_gate_attempts WHERE attempt_id = ?",
            (attempt_id,),
        )
        row = cursor.fetchone()
        return (
            None
            if row is None
            else SQLiteSemanticGateV3Repository._attempt_from_row(row)
        )

    def _load_chain(
        self,
        cursor: sqlite3.Cursor,
        evaluation: SystemGateEvaluation,
        snapshot: RetrievalSnapshot,
        head: _AttemptHead,
    ) -> tuple[SemanticGateAttempt, ...]:
        if (
            head.system_gate_evaluation_id != evaluation.evaluation_id
            or head.session_id != evaluation.session_id
            or head.retrieval_snapshot_id != snapshot.snapshot_id
            or head.current_sequence < 1
            or head.current_sequence > GATE_EVALUATION_MAX_DECISIONS
            or head.current_attempt_id is None
        ):
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate head does not match Gate evidence"
            )
        cursor.execute(
            "SELECT attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor "
            "FROM v3_semantic_gate_attempts "
            "WHERE system_gate_evaluation_id = ? ORDER BY sequence",
            (evaluation.evaluation_id,),
        )
        rows = cursor.fetchall()
        attempts = tuple(self._attempt_from_row(row) for row in rows)
        if (
            len(attempts) != head.current_sequence
            or attempts[-1].attempt_id != head.current_attempt_id
        ):
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate head does not match its attempt chain"
            )
        try:
            verify_semantic_gate_attempt_chain(
                attempts,
                evaluation,
                snapshot,
            )
        except GateEvaluationContractError as error:
            raise SQLiteSemanticGateV3PersistenceError(
                "stored Semantic Gate attempt chain failed validation"
            ) from error
        return attempts

    def _append_attempt(
        self,
        cursor: sqlite3.Cursor,
        attempt: SemanticGateAttempt,
        evaluation: SystemGateEvaluation,
        snapshot: RetrievalSnapshot,
    ) -> bool:
        existing = self._select_attempt(cursor, attempt.attempt_id)
        if existing is not None:
            if self._attempt_row(existing) != self._attempt_row(attempt):
                raise SQLiteSemanticGateV3ConflictError(
                    "Semantic Gate attempt ID has conflicting content"
                )
            head = self._select_head(
                cursor,
                attempt.system_gate_evaluation_id,
            )
            if head is None or attempt not in self._load_chain(
                cursor,
                evaluation,
                snapshot,
                head,
            ):
                raise SQLiteSemanticGateV3PersistenceError(
                    "stored Semantic Gate replay is outside its chain"
                )
            return False

        head = self._select_head(
            cursor,
            attempt.system_gate_evaluation_id,
        )
        parent: SemanticGateAttempt | None
        if head is None:
            parent = None
            cursor.execute(
                "INSERT INTO v3_semantic_gate_attempt_heads ("
                "system_gate_evaluation_id, session_id, "
                "retrieval_snapshot_id, current_sequence, "
                "current_attempt_id) VALUES (?, ?, ?, 0, NULL)",
                (
                    evaluation.evaluation_id,
                    evaluation.session_id,
                    snapshot.snapshot_id,
                ),
            )
            previous_sequence = 0
            previous_attempt_id = None
        else:
            chain = self._load_chain(
                cursor,
                evaluation,
                snapshot,
                head,
            )
            parent = chain[-1]
            previous_sequence = head.current_sequence
            previous_attempt_id = head.current_attempt_id
        try:
            verify_semantic_gate_attempt_parent(attempt, parent)
        except GateEvaluationContractError as error:
            raise SQLiteSemanticGateV3ConflictError(
                "Semantic Gate attempt does not extend the current chain"
            ) from error
        cursor.execute(
            "INSERT INTO v3_semantic_gate_attempts ("
            "attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._attempt_row(attempt),
        )
        cursor.execute(
            "UPDATE v3_semantic_gate_attempt_heads "
            "SET current_sequence = ?, current_attempt_id = ? "
            "WHERE system_gate_evaluation_id = ? "
            "AND current_sequence = ? AND current_attempt_id IS ?",
            (
                attempt.sequence,
                attempt.attempt_id,
                attempt.system_gate_evaluation_id,
                previous_sequence,
                previous_attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise SQLiteSemanticGateV3ConflictError(
                "Semantic Gate attempt chain changed during append"
            )
        return True

    @_synchronized
    def store_attempt(
        self,
        attempt: SemanticGateAttempt,
    ) -> SQLiteSemanticGateV3StoreResult:
        if type(attempt) is not SemanticGateAttempt:
            raise ValueError("attempt must be exactly SemanticGateAttempt")
        if attempt.sequence > GATE_EVALUATION_MAX_DECISIONS:
            raise ValueError("semantic Gate attempt sequence exceeds ledger bound")
        self._require_open()
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    evaluation, snapshot = self._load_gate_records(
                        cursor,
                        attempt.system_gate_evaluation_id,
                    )
                    try:
                        verify_semantic_gate_attempt(
                            attempt,
                            evaluation,
                            snapshot,
                        )
                    except GateEvaluationContractError as error:
                        raise SQLiteSemanticGateV3ConflictError(
                            "Semantic Gate attempt does not match Gate evidence"
                        ) from error
                    inserted = self._append_attempt(
                        cursor,
                        attempt,
                        evaluation,
                        snapshot,
                    )
                    head = self._select_head(
                        cursor,
                        attempt.system_gate_evaluation_id,
                    )
                    if head is None:
                        raise SQLiteSemanticGateV3PersistenceError(
                            "Semantic Gate attempt head is missing after append"
                        )
                    chain = self._load_chain(
                        cursor,
                        evaluation,
                        snapshot,
                        head,
                    )
                    if attempt not in chain:
                        raise SQLiteSemanticGateV3PersistenceError(
                            "Semantic Gate attempt read-back does not match"
                        )
            return SQLiteSemanticGateV3StoreResult(
                attempt.attempt_id,
                attempt.sequence,
                inserted,
            )
        except (
            SQLiteSemanticGateV3Error,
            ValueError,
        ):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteSemanticGateV3ConflictError(
                "Semantic Gate attempt conflicts with immutable storage"
            ) from error
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def load_attempt(self, attempt_id: str) -> SemanticGateAttempt:
        self._require_open()
        if (
            type(attempt_id) is not str
            or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None
        ):
            raise ValueError("attempt_id must be a v3 Semantic Gate attempt ID")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    attempt = self._select_attempt(cursor, attempt_id)
                    if attempt is None:
                        raise SQLiteSemanticGateV3NotFoundError(
                            "Semantic Gate attempt was not found"
                        )
                    evaluation, snapshot = self._load_gate_records(
                        cursor,
                        attempt.system_gate_evaluation_id,
                    )
                    head = self._select_head(
                        cursor,
                        attempt.system_gate_evaluation_id,
                    )
                    if head is None or attempt not in self._load_chain(
                        cursor,
                        evaluation,
                        snapshot,
                        head,
                    ):
                        raise SQLiteSemanticGateV3PersistenceError(
                            "stored Semantic Gate attempt is outside its chain"
                        )
                    return attempt
        except SQLiteSemanticGateV3Error:
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def load_chain(
        self,
        evaluation_id: str,
    ) -> tuple[SemanticGateAttempt, ...]:
        self._require_open()
        if (
            type(evaluation_id) is not str
            or _SYSTEM_ID_RE.fullmatch(evaluation_id) is None
        ):
            raise ValueError(
                "evaluation_id must be a v3 System Gate evaluation ID"
            )
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    evaluation, snapshot = self._load_gate_records(
                        cursor,
                        evaluation_id,
                    )
                    head = self._select_head(cursor, evaluation_id)
                    if head is None:
                        raise SQLiteSemanticGateV3NotFoundError(
                            "Semantic Gate attempt chain was not found"
                        )
                    return self._load_chain(
                        cursor,
                        evaluation,
                        snapshot,
                        head,
                    )
        except SQLiteSemanticGateV3Error:
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    def _raise_sqlite(self, error: sqlite3.Error) -> NoReturn:
        message = str(error).casefold()
        if (
            "no such table" in message
            or "no such trigger" in message
            or "schema" in message
        ):
            raise SQLiteSemanticGateV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        raise SQLiteSemanticGateV3PersistenceError(
            "SQLite semantic Gate v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteSemanticGateV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "SQLITE_SEMANTIC_GATE_V3_SCHEMA_VERSION",
    "SQLiteSemanticGateV3ConflictError",
    "SQLiteSemanticGateV3Error",
    "SQLiteSemanticGateV3NotFoundError",
    "SQLiteSemanticGateV3PersistenceError",
    "SQLiteSemanticGateV3Repository",
    "SQLiteSemanticGateV3SchemaError",
    "SQLiteSemanticGateV3StoreResult",
]
