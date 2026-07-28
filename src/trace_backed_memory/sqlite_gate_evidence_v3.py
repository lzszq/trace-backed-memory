from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path
import sqlite3
from threading import RLock
from typing import ParamSpec, TypeVar

from .gate_evaluation_v3 import (
    SystemGateEvaluation,
    dumps_system_gate_evaluation,
    loads_system_gate_evaluation,
    verify_system_gate_evaluation,
)
from .resources import PackagedResourceError, read_packaged_resource
from .retrieval_v3 import (
    RetrievalSnapshot,
    dumps_retrieval_snapshot,
    loads_retrieval_snapshot,
)


SQLITE_GATE_EVIDENCE_V3_SCHEMA_VERSION = 1
_MISSING_SCHEMA_MESSAGE = (
    "SQLite gate evidence v3 schema is missing or incomplete"
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_gate_evidence_schema",
    "v3_retrieval_snapshots",
    "v3_retrieval_snapshots_immutable_delete",
    "v3_retrieval_snapshots_immutable_update",
    "v3_retrieval_snapshots_session",
    "v3_system_gate_evaluations",
    "v3_system_gate_evaluations_immutable_delete",
    "v3_system_gate_evaluations_immutable_update",
    "v3_system_gate_evaluations_session",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteGateEvidenceV3Error(RuntimeError):
    pass


class SQLiteGateEvidenceV3SchemaError(SQLiteGateEvidenceV3Error):
    pass


class SQLiteGateEvidenceV3ConflictError(SQLiteGateEvidenceV3Error):
    pass


class SQLiteGateEvidenceV3NotFoundError(SQLiteGateEvidenceV3Error):
    pass


class SQLiteGateEvidenceV3PersistenceError(SQLiteGateEvidenceV3Error):
    pass


@dataclass(frozen=True)
class SQLiteGateEvidenceV3StoreResult:
    snapshot_id: str
    snapshot_inserted: bool
    evaluation_id: str
    evaluation_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteGateEvidenceV3SchemaError(
            "SQLite gate evidence v3 schema contains an invalid definition"
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
        raise SQLiteGateEvidenceV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteGateEvidenceV3SchemaError(
                "SQLite gate evidence v3 schema definition has an invalid shape"
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
                    "schemas/sqlite-v3-gate-evidence.sql"
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
        raise SQLiteGateEvidenceV3SchemaError(
            "could not validate canonical SQLite gate evidence v3 schema"
        ) from error


class SQLiteGateEvidenceV3Repository:
    """Opt-in immutable SQLite ledger for retrieval and System Gate evidence."""

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
    ) -> SQLiteGateEvidenceV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        "schemas/sqlite-v3-gate-evidence.sql"
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
            raise SQLiteGateEvidenceV3PersistenceError(
                "failed to connect to SQLite gate evidence v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteGateEvidenceV3Error(
                "SQLite gate evidence v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteGateEvidenceV3Error(
                "SQLite gate evidence v3 repository is closed"
            ) from error

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_gate_evidence_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
            except BaseException:
                self._connection.execute(
                    f"ROLLBACK TO SAVEPOINT {savepoint}"
                )
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteGateEvidenceV3SchemaError(
                "SQLite gate evidence v3 requires foreign keys"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteGateEvidenceV3SchemaError(
                "SQLite gate evidence v3 requires recursive triggers"
            )
        cursor.execute(
            "SELECT schema_version "
            "FROM trace_backed_memory_v3_gate_evidence_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if rows != [(SQLITE_GATE_EVIDENCE_V3_SCHEMA_VERSION,)]:
            raise SQLiteGateEvidenceV3SchemaError(
                "SQLite gate evidence v3 schema metadata mismatch"
            )
        if _read_schema_definitions(cursor) != _canonical_schema_definitions():
            raise SQLiteGateEvidenceV3SchemaError(
                "SQLite gate evidence v3 schema definitions do not match"
            )

    @staticmethod
    def _snapshot_row(snapshot: RetrievalSnapshot) -> tuple[str, str, str, str]:
        return (
            snapshot.snapshot_id,
            snapshot.session_id,
            snapshot.authorization_event_id,
            dumps_retrieval_snapshot(snapshot),
        )

    @staticmethod
    def _evaluation_row(
        evaluation: SystemGateEvaluation,
    ) -> tuple[str, str, str, str, str]:
        return (
            evaluation.evaluation_id,
            evaluation.session_id,
            evaluation.retrieval_snapshot_id,
            evaluation.authorization_event_id,
            dumps_system_gate_evaluation(evaluation),
        )

    @staticmethod
    def _snapshot_from_row(row: tuple[object, ...]) -> RetrievalSnapshot:
        if len(row) != 4 or type(row[3]) is not str:
            raise SQLiteGateEvidenceV3PersistenceError(
                "SQLite retrieval snapshot row has an invalid shape"
            )
        try:
            snapshot = loads_retrieval_snapshot(row[3])
        except ValueError as error:
            raise SQLiteGateEvidenceV3PersistenceError(
                "stored retrieval snapshot failed validation"
            ) from error
        if row != SQLiteGateEvidenceV3Repository._snapshot_row(snapshot):
            raise SQLiteGateEvidenceV3PersistenceError(
                "retrieval snapshot columns do not match descriptor"
            )
        return snapshot

    @staticmethod
    def _evaluation_from_row(
        row: tuple[object, ...],
    ) -> SystemGateEvaluation:
        if len(row) != 5 or type(row[4]) is not str:
            raise SQLiteGateEvidenceV3PersistenceError(
                "SQLite System Gate row has an invalid shape"
            )
        try:
            evaluation = loads_system_gate_evaluation(row[4])
        except ValueError as error:
            raise SQLiteGateEvidenceV3PersistenceError(
                "stored System Gate evaluation failed validation"
            ) from error
        if row != SQLiteGateEvidenceV3Repository._evaluation_row(evaluation):
            raise SQLiteGateEvidenceV3PersistenceError(
                "System Gate columns do not match descriptor"
            )
        return evaluation

    def _put_snapshot(
        self,
        cursor: sqlite3.Cursor,
        snapshot: RetrievalSnapshot,
    ) -> bool:
        expected = self._snapshot_row(snapshot)
        cursor.execute(
            "SELECT snapshot_id, session_id, authorization_event_id, "
            "descriptor FROM v3_retrieval_snapshots WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        )
        stored = cursor.fetchone()
        inserted = stored is None
        if inserted:
            cursor.execute(
                "INSERT INTO v3_retrieval_snapshots ("
                "snapshot_id, session_id, authorization_event_id, descriptor"
                ") VALUES (?, ?, ?, ?)",
                expected,
            )
            stored = expected
        if stored != expected:
            raise SQLiteGateEvidenceV3ConflictError(
                "retrieval snapshot ID has conflicting immutable content"
            )
        self._snapshot_from_row(stored)
        return inserted

    def _put_evaluation(
        self,
        cursor: sqlite3.Cursor,
        evaluation: SystemGateEvaluation,
    ) -> bool:
        expected = self._evaluation_row(evaluation)
        cursor.execute(
            "SELECT evaluation_id, session_id, retrieval_snapshot_id, "
            "authorization_event_id, descriptor "
            "FROM v3_system_gate_evaluations WHERE evaluation_id = ?",
            (evaluation.evaluation_id,),
        )
        stored = cursor.fetchone()
        inserted = stored is None
        if inserted:
            cursor.execute(
                "INSERT INTO v3_system_gate_evaluations ("
                "evaluation_id, session_id, retrieval_snapshot_id, "
                "authorization_event_id, descriptor"
                ") VALUES (?, ?, ?, ?, ?)",
                expected,
            )
            stored = expected
        if stored != expected:
            raise SQLiteGateEvidenceV3ConflictError(
                "System Gate evaluation ID has conflicting immutable content"
            )
        self._evaluation_from_row(stored)
        return inserted

    @_synchronized
    def store_bundle(
        self,
        snapshot: RetrievalSnapshot,
        evaluation: SystemGateEvaluation,
    ) -> SQLiteGateEvidenceV3StoreResult:
        self._require_open()
        if (
            type(snapshot) is not RetrievalSnapshot
            or type(evaluation) is not SystemGateEvaluation
        ):
            raise ValueError(
                "snapshot and evaluation must be exact v3 records"
            )
        verify_system_gate_evaluation(evaluation, snapshot)
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    snapshot_inserted = self._put_snapshot(cursor, snapshot)
                    evaluation_inserted = self._put_evaluation(
                        cursor,
                        evaluation,
                    )
            return SQLiteGateEvidenceV3StoreResult(
                snapshot_id=snapshot.snapshot_id,
                snapshot_inserted=snapshot_inserted,
                evaluation_id=evaluation.evaluation_id,
                evaluation_inserted=evaluation_inserted,
            )
        except (
            SQLiteGateEvidenceV3ConflictError,
            SQLiteGateEvidenceV3SchemaError,
            ValueError,
        ):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteGateEvidenceV3ConflictError(
                "gate evidence conflicts with immutable storage"
            ) from error
        except sqlite3.DatabaseError as error:
            raise SQLiteGateEvidenceV3PersistenceError(
                "failed to store gate evidence bundle"
            ) from error

    @_synchronized
    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot:
        self._require_open()
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT snapshot_id, session_id, "
                        "authorization_event_id, descriptor "
                        "FROM v3_retrieval_snapshots WHERE snapshot_id = ?",
                        (snapshot_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise SQLiteGateEvidenceV3NotFoundError(
                            "retrieval snapshot was not found"
                        )
                    return self._snapshot_from_row(row)
        except (
            SQLiteGateEvidenceV3NotFoundError,
            SQLiteGateEvidenceV3SchemaError,
            SQLiteGateEvidenceV3PersistenceError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            raise SQLiteGateEvidenceV3PersistenceError(
                "failed to load retrieval snapshot"
            ) from error

    @_synchronized
    def load_evaluation(
        self,
        evaluation_id: str,
    ) -> SystemGateEvaluation:
        self._require_open()
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT evaluation_id, session_id, "
                        "retrieval_snapshot_id, authorization_event_id, "
                        "descriptor FROM v3_system_gate_evaluations "
                        "WHERE evaluation_id = ?",
                        (evaluation_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise SQLiteGateEvidenceV3NotFoundError(
                            "System Gate evaluation was not found"
                        )
                    return self._evaluation_from_row(row)
        except (
            SQLiteGateEvidenceV3NotFoundError,
            SQLiteGateEvidenceV3SchemaError,
            SQLiteGateEvidenceV3PersistenceError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            raise SQLiteGateEvidenceV3PersistenceError(
                "failed to load System Gate evaluation"
            ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        if self._owns_connection:
            self._connection.close()
        self._closed = True

    def __enter__(self) -> SQLiteGateEvidenceV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
