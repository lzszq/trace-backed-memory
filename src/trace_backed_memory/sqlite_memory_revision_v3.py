from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
from pathlib import Path
import sqlite3
from threading import RLock
from typing import ParamSpec, TypeVar

from .evidence_v3 import (
    StructuredRegressionEvidence,
    dumps_structured_regression_evidence,
    loads_structured_regression_evidence,
)
from .fix_evidence_v3 import FixEvidence, dumps_fix_evidence, loads_fix_evidence
from .memory_revision_v3 import (
    MemoryRevision,
    dumps_memory_revision,
    loads_memory_revision,
    verify_memory_revision_evidence_bundle,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_MEMORY_REVISION_V3_SCHEMA_VERSION = 1
_SCHEMA_RESOURCE = "schemas/sqlite-v3-memory-revision.sql"
_MISSING_SCHEMA_MESSAGE = (
    "SQLite memory revision v3 schema is missing or incomplete"
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_memory_revision_schema",
    "v3_fix_evidence",
    "v3_fix_evidence_immutable_delete",
    "v3_fix_evidence_immutable_update",
    "v3_memory_revision_links_immutable_delete",
    "v3_memory_revision_links_immutable_update",
    "v3_memory_revision_parent_continuity",
    "v3_memory_revision_proposals",
    "v3_memory_revision_proposals_immutable_delete",
    "v3_memory_revision_proposals_immutable_update",
    "v3_memory_revision_regression_evidence",
    "v3_regression_evidence",
    "v3_regression_evidence_immutable_delete",
    "v3_regression_evidence_immutable_update",
)
_CONTROLLED_TABLES = (
    "trace_backed_memory_v3_memory_revision_schema",
    "v3_fix_evidence",
    "v3_regression_evidence",
    "v3_memory_revision_proposals",
    "v3_memory_revision_regression_evidence",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteMemoryRevisionV3Error(RuntimeError):
    pass


class SQLiteMemoryRevisionV3SchemaError(SQLiteMemoryRevisionV3Error):
    pass


class SQLiteMemoryRevisionV3ConflictError(SQLiteMemoryRevisionV3Error):
    pass


class SQLiteMemoryRevisionV3NotFoundError(SQLiteMemoryRevisionV3Error):
    pass


class SQLiteMemoryRevisionV3PersistenceError(SQLiteMemoryRevisionV3Error):
    pass


@dataclass(frozen=True)
class SQLiteMemoryRevisionV3StoreResult:
    revision_id: str
    revision_inserted: bool
    fix_evidence_inserted: bool
    regression_evidence_inserted: int


@dataclass(frozen=True)
class StoredMemoryRevisionProposal:
    revision: MemoryRevision
    fix_evidence: FixEvidence | None
    regression_evidence: tuple[StructuredRegressionEvidence, ...]


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteMemoryRevisionV3SchemaError(
            "SQLite memory revision v3 schema contains an invalid definition"
        )
    return value.strip().replace("\r\n", "\n")


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
        raise SQLiteMemoryRevisionV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteMemoryRevisionV3SchemaError(
                "SQLite memory revision schema definition has invalid shape"
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    controlled = ", ".join("?" for _ in _CONTROLLED_TABLES)
    cursor.execute(
        "SELECT name FROM main.sqlite_master "
        f"WHERE tbl_name IN ({controlled}) "
        "AND name NOT LIKE 'sqlite_autoindex_%' ORDER BY name",
        _CONTROLLED_TABLES,
    )
    if tuple(row[0] for row in cursor.fetchall()) != tuple(
        sorted(_SCHEMA_OBJECT_NAMES)
    ):
        raise SQLiteMemoryRevisionV3SchemaError(
            "SQLite memory revision schema contains unexpected objects"
        )
    cursor.execute(
        "SELECT name FROM sqlite_temp_master "
        f"WHERE tbl_name IN ({controlled}) OR name IN ({controlled}) LIMIT 1",
        (*_CONTROLLED_TABLES, *_CONTROLLED_TABLES),
    )
    if cursor.fetchone() is not None:
        raise SQLiteMemoryRevisionV3SchemaError(
            "SQLite memory revision schema forbids temporary shadow objects"
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
        raise SQLiteMemoryRevisionV3SchemaError(
            "could not validate canonical SQLite memory revision v3 schema"
        ) from error


class SQLiteMemoryRevisionV3Repository:
    """Isolated immutable proposal ledger; it never approves or activates."""

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
    ) -> SQLiteMemoryRevisionV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
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
            raise SQLiteMemoryRevisionV3PersistenceError(
                "failed to connect to SQLite memory revision v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteMemoryRevisionV3Error(
                "SQLite memory revision v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteMemoryRevisionV3Error(
                "SQLite memory revision v3 repository is closed"
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
                "failed to close unusable SQLite memory revision v3 "
                f"connection: {close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_sqlite_memory_revision_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to clean up SQLite memory revision v3 "
                        f"savepoint {savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after memory "
                            "revision v3 savepoint cleanup failed"
                        ),
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
                context=(
                    "the top-level SQLite memory revision v3 transaction"
                ),
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context=(
                        "the top-level SQLite memory revision v3 transaction"
                    ),
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        try:
            cursor.execute("PRAGMA foreign_keys")
            if cursor.fetchone() != (1,):
                raise SQLiteMemoryRevisionV3SchemaError(
                    "SQLite memory revision v3 requires foreign keys"
                )
            cursor.execute("PRAGMA recursive_triggers")
            if cursor.fetchone() != (1,):
                raise SQLiteMemoryRevisionV3SchemaError(
                    "SQLite memory revision v3 requires recursive triggers"
                )
            cursor.execute(
                "SELECT schema_version "
                "FROM main.trace_backed_memory_v3_memory_revision_schema "
                "WHERE singleton = 1"
            )
            if cursor.fetchall() != [
                (SQLITE_MEMORY_REVISION_V3_SCHEMA_VERSION,)
            ]:
                raise SQLiteMemoryRevisionV3SchemaError(
                    "SQLite memory revision v3 schema metadata mismatch"
                )
            if (
                _read_schema_definitions(cursor)
                != _canonical_schema_definitions()
            ):
                raise SQLiteMemoryRevisionV3SchemaError(
                    "SQLite memory revision v3 schema definitions do not match"
                )
        except SQLiteMemoryRevisionV3SchemaError:
            raise
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryRevisionV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error

    @staticmethod
    def _put_exact(
        cursor: sqlite3.Cursor,
        *,
        table: str,
        id_column: str,
        identity: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        conflict_message: str,
    ) -> bool:
        column_list = ", ".join(columns)
        cursor.execute(
            f"SELECT {column_list} FROM {table} WHERE {id_column} = ?",
            (identity,),
        )
        stored = cursor.fetchone()
        inserted = stored is None
        if inserted:
            placeholders = ", ".join("?" for _ in values)
            cursor.execute(
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                values,
            )
            stored = values
        if stored != values:
            raise SQLiteMemoryRevisionV3ConflictError(conflict_message)
        return inserted

    @staticmethod
    def _put_fix(cursor: sqlite3.Cursor, evidence: FixEvidence) -> bool:
        values = (
            evidence.evidence_id,
            evidence.case_id,
            evidence.source_trace_id,
            evidence.source_commit_sha,
            evidence.fix_commit_sha,
            dumps_fix_evidence(evidence),
        )
        return SQLiteMemoryRevisionV3Repository._put_exact(
            cursor,
            table="main.v3_fix_evidence",
            id_column="evidence_id",
            identity=evidence.evidence_id,
            columns=(
                "evidence_id",
                "case_id",
                "source_trace_id",
                "source_commit_sha",
                "fix_commit_sha",
                "descriptor",
            ),
            values=values,
            conflict_message="fix evidence ID has conflicting immutable content",
        )

    @staticmethod
    def _put_regression(
        cursor: sqlite3.Cursor,
        evidence: StructuredRegressionEvidence,
    ) -> bool:
        values = (
            evidence.evidence_id,
            evidence.case_id,
            evidence.source_trace_id,
            evidence.source_commit_sha,
            evidence.fix_commit_sha,
            dumps_structured_regression_evidence(evidence),
        )
        return SQLiteMemoryRevisionV3Repository._put_exact(
            cursor,
            table="main.v3_regression_evidence",
            id_column="evidence_id",
            identity=evidence.evidence_id,
            columns=(
                "evidence_id",
                "case_id",
                "source_trace_id",
                "source_commit_sha",
                "fix_commit_sha",
                "descriptor",
            ),
            values=values,
            conflict_message=(
                "regression evidence ID has conflicting immutable content"
            ),
        )

    @staticmethod
    def _revision_values(revision: MemoryRevision) -> tuple[object, ...]:
        return (
            revision.revision_id,
            revision.memory_id,
            revision.revision_number,
            revision.previous_revision_id,
            revision.fix_evidence_id,
            dumps_memory_revision(revision),
        )

    @staticmethod
    def _load_row(
        cursor: sqlite3.Cursor,
        table: str,
        id_column: str,
        identity: str,
        columns: tuple[str, ...],
    ) -> tuple[object, ...]:
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE {id_column} = ?",
            (identity,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteMemoryRevisionV3NotFoundError(
                "memory revision proposal was not found"
            )
        if len(row) != len(columns):
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored proposal row has an invalid shape"
            )
        return row

    @_synchronized
    def store_proposal(
        self,
        revision: MemoryRevision,
        fix_evidence: FixEvidence | None,
        regression_evidence: tuple[StructuredRegressionEvidence, ...],
    ) -> SQLiteMemoryRevisionV3StoreResult:
        if type(revision) is not MemoryRevision:
            raise ValueError("revision must be exactly MemoryRevision")
        if fix_evidence is not None and type(fix_evidence) is not FixEvidence:
            raise ValueError("fix_evidence must be exactly FixEvidence or None")
        if type(regression_evidence) is not tuple or any(
            type(item) is not StructuredRegressionEvidence
            for item in regression_evidence
        ):
            raise ValueError("regression_evidence must be an exact tuple")
        regression_by_id = {
            evidence.evidence_id: evidence for evidence in regression_evidence
        }
        if len(regression_by_id) != len(regression_evidence):
            raise ValueError("regression_evidence must not contain duplicates")
        if tuple(sorted(regression_by_id)) != revision.regression_evidence_ids:
            raise ValueError("regression_evidence must exactly match revision")
        ordered_regression = tuple(
            regression_by_id[evidence_id]
            for evidence_id in revision.regression_evidence_ids
        )
        fix_by_id = (
            {} if fix_evidence is None else {fix_evidence.evidence_id: fix_evidence}
        )
        verify_memory_revision_evidence_bundle(
            revision,
            fix_by_id,
            regression_by_id,
        )
        self._require_open()
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    cursor.execute(
                        "SELECT revision_id FROM "
                        "main.v3_memory_revision_proposals "
                        "WHERE revision_id = ?",
                        (revision.revision_id,),
                    )
                    existing_rows = cursor.fetchall()
                    if existing_rows:
                        if existing_rows != [(revision.revision_id,)]:
                            raise SQLiteMemoryRevisionV3PersistenceError(
                                "stored proposal identity has invalid shape"
                            )
                        if self._load_bundle(cursor, revision.revision_id) != (
                            StoredMemoryRevisionProposal(
                                revision,
                                fix_evidence,
                                ordered_regression,
                            )
                        ):
                            raise SQLiteMemoryRevisionV3ConflictError(
                                "stored proposal bundle does not match exact input"
                            )
                        return SQLiteMemoryRevisionV3StoreResult(
                            revision_id=revision.revision_id,
                            revision_inserted=False,
                            fix_evidence_inserted=False,
                            regression_evidence_inserted=0,
                        )
                    fix_inserted = (
                        False
                        if fix_evidence is None
                        else self._put_fix(cursor, fix_evidence)
                    )
                    regression_inserted = sum(
                        self._put_regression(cursor, evidence)
                        for evidence in ordered_regression
                    )
                    values = self._revision_values(revision)
                    revision_inserted = self._put_exact(
                        cursor,
                        table="main.v3_memory_revision_proposals",
                        id_column="revision_id",
                        identity=revision.revision_id,
                        columns=(
                            "revision_id",
                            "memory_id",
                            "revision_number",
                            "previous_revision_id",
                            "fix_evidence_id",
                            "descriptor",
                        ),
                        values=values,
                        conflict_message=(
                            "memory revision ID has conflicting immutable content"
                        ),
                    )
                    for ordinal, evidence_id in enumerate(
                        revision.regression_evidence_ids
                    ):
                        cursor.execute(
                            "INSERT OR IGNORE INTO "
                            "main.v3_memory_revision_regression_evidence "
                            "(revision_id, evidence_id, ordinal) VALUES (?, ?, ?)",
                            (revision.revision_id, evidence_id, ordinal),
                        )
                    stored = self._load_bundle(cursor, revision.revision_id)
                    if stored != StoredMemoryRevisionProposal(
                        revision,
                        fix_evidence,
                        ordered_regression,
                    ):
                        raise SQLiteMemoryRevisionV3ConflictError(
                            "stored proposal bundle does not match exact input"
                        )
            return SQLiteMemoryRevisionV3StoreResult(
                revision_id=revision.revision_id,
                revision_inserted=revision_inserted,
                fix_evidence_inserted=fix_inserted,
                regression_evidence_inserted=regression_inserted,
            )
        except (
            SQLiteMemoryRevisionV3ConflictError,
            SQLiteMemoryRevisionV3SchemaError,
            ValueError,
        ):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteMemoryRevisionV3ConflictError(
                "proposal conflicts with immutable revision history"
            ) from error
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "failed to store memory revision proposal"
            ) from error

    def _load_bundle(
        self,
        cursor: sqlite3.Cursor,
        revision_id: str,
    ) -> StoredMemoryRevisionProposal:
        revision_row = self._load_row(
            cursor,
            "main.v3_memory_revision_proposals",
            "revision_id",
            revision_id,
            (
                "revision_id",
                "memory_id",
                "revision_number",
                "previous_revision_id",
                "fix_evidence_id",
                "descriptor",
            ),
        )
        if type(revision_row[5]) is not str:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored revision descriptor has invalid shape"
            )
        try:
            revision = loads_memory_revision(revision_row[5])
        except ValueError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored revision descriptor failed validation"
            ) from error
        if revision_row != self._revision_values(revision):
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored revision columns do not match descriptor"
            )
        fix_evidence = (
            None
            if revision.fix_evidence_id is None
            else self._load_fix_evidence(cursor, revision.fix_evidence_id)
        )
        cursor.execute(
            "SELECT ordinal, evidence_id FROM "
            "main.v3_memory_revision_regression_evidence "
            "WHERE revision_id = ? ORDER BY ordinal",
            (revision_id,),
        )
        evidence_rows = cursor.fetchall()
        if any(
            len(row) != 2
            or type(row[0]) is not int
            or type(row[1]) is not str
            for row in evidence_rows
        ):
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression evidence links have invalid shape"
            )
        if tuple(row[0] for row in evidence_rows) != tuple(
            range(len(evidence_rows))
        ):
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression evidence link ordinals are not contiguous"
            )
        evidence_ids = tuple(row[1] for row in evidence_rows)
        if evidence_ids != revision.regression_evidence_ids:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression evidence links do not match revision"
            )
        regression = tuple(
            self._load_regression_evidence(cursor, evidence_id)
            for evidence_id in evidence_ids
        )
        try:
            verify_memory_revision_evidence_bundle(
                revision,
                (
                    {}
                    if fix_evidence is None
                    else {fix_evidence.evidence_id: fix_evidence}
                ),
                {evidence.evidence_id: evidence for evidence in regression},
            )
        except ValueError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored proposal bundle failed validation"
            ) from error
        return StoredMemoryRevisionProposal(revision, fix_evidence, regression)

    def _load_fix_evidence(
        self,
        cursor: sqlite3.Cursor,
        evidence_id: str,
    ) -> FixEvidence:
        columns = (
            "evidence_id",
            "case_id",
            "source_trace_id",
            "source_commit_sha",
            "fix_commit_sha",
            "descriptor",
        )
        try:
            row = self._load_row(
                cursor,
                "main.v3_fix_evidence",
                "evidence_id",
                evidence_id,
                columns,
            )
        except SQLiteMemoryRevisionV3NotFoundError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored fix evidence reference is missing"
            ) from error
        if type(row[5]) is not str:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored fix evidence descriptor has invalid shape"
            )
        try:
            evidence = loads_fix_evidence(row[5])
        except ValueError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored fix evidence failed validation"
            ) from error
        expected = (
            evidence.evidence_id,
            evidence.case_id,
            evidence.source_trace_id,
            evidence.source_commit_sha,
            evidence.fix_commit_sha,
            dumps_fix_evidence(evidence),
        )
        if row != expected:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored fix evidence columns do not match descriptor"
            )
        return evidence

    def _load_regression_evidence(
        self,
        cursor: sqlite3.Cursor,
        evidence_id: str,
    ) -> StructuredRegressionEvidence:
        columns = (
            "evidence_id",
            "case_id",
            "source_trace_id",
            "source_commit_sha",
            "fix_commit_sha",
            "descriptor",
        )
        try:
            row = self._load_row(
                cursor,
                "main.v3_regression_evidence",
                "evidence_id",
                evidence_id,
                columns,
            )
        except SQLiteMemoryRevisionV3NotFoundError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression evidence reference is missing"
            ) from error
        if type(row[5]) is not str:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression descriptor has invalid shape"
            )
        try:
            evidence = loads_structured_regression_evidence(row[5])
        except ValueError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression evidence failed validation"
            ) from error
        expected = (
            evidence.evidence_id,
            evidence.case_id,
            evidence.source_trace_id,
            evidence.source_commit_sha,
            evidence.fix_commit_sha,
            dumps_structured_regression_evidence(evidence),
        )
        if row != expected:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "stored regression columns do not match descriptor"
            )
        return evidence

    @_synchronized
    def load_proposal(self, revision_id: str) -> StoredMemoryRevisionProposal:
        self._require_open()
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    return self._load_bundle(cursor, revision_id)
        except (
            SQLiteMemoryRevisionV3NotFoundError,
            SQLiteMemoryRevisionV3SchemaError,
            SQLiteMemoryRevisionV3PersistenceError,
            ValueError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryRevisionV3PersistenceError(
                "failed to load memory revision proposal"
            ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        if self._owns_connection:
            self._connection.close()
        self._closed = True

    def __enter__(self) -> SQLiteMemoryRevisionV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "SQLITE_MEMORY_REVISION_V3_SCHEMA_VERSION",
    "SQLiteMemoryRevisionV3ConflictError",
    "SQLiteMemoryRevisionV3Error",
    "SQLiteMemoryRevisionV3NotFoundError",
    "SQLiteMemoryRevisionV3PersistenceError",
    "SQLiteMemoryRevisionV3Repository",
    "SQLiteMemoryRevisionV3SchemaError",
    "SQLiteMemoryRevisionV3StoreResult",
    "StoredMemoryRevisionProposal",
]
