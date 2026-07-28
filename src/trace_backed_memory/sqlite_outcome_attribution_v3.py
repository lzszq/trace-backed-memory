from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from ._timestamps import parse_rfc3339
from .contracts_v3 import V3ContractError
from .outcome_v3 import (
    OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
    OutcomeAttribution,
    OutcomeContractError,
    dumps_outcome_attribution,
    loads_outcome_attribution,
    verify_outcome_attribution,
)
from .resources import PackagedResourceError, read_packaged_resource
from .sqlite_gate_session_v3 import (
    SQLiteGateSessionNotFoundError,
    SQLiteGateSessionPersistenceError,
    SQLiteGateSessionSchemaError,
)
from .sqlite_outcome_v3 import (
    SQLiteOutcomeV3NotFoundError,
    SQLiteOutcomeV3PersistenceError,
    SQLiteOutcomeV3Repository,
    SQLiteOutcomeV3SchemaError,
)


SQLITE_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION = 1
_GATE_SCHEMA_RESOURCE = "schemas/sqlite-v3-gate-session.sql"
_OUTCOME_SCHEMA_RESOURCE = "schemas/sqlite-v3-outcome.sql"
_SCHEMA_RESOURCE = "schemas/sqlite-v3-outcome-attribution.sql"
_MISSING_SCHEMA_MESSAGE = (
    "SQLite OutcomeAttribution v3 schema is missing or incomplete"
)
_SCHEMA_TABLE_NAMES = (
    "trace_backed_memory_v3_outcome_attribution_schema",
    "v3_outcome_attributions",
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_outcome_attribution_schema",
    "v3_outcome_attributions",
    "v3_outcome_attributions_by_outcome",
    "v3_outcome_attribution_schema_immutable_delete",
    "v3_outcome_attribution_schema_immutable_insert",
    "v3_outcome_attribution_schema_immutable_update",
    "v3_outcome_attributions_immutable_delete",
    "v3_outcome_attributions_immutable_insert",
    "v3_outcome_attributions_immutable_update",
    "v3_outcome_attributions_validate_insert",
)
_TEMP_FORBIDDEN_NAMES = (
    *_SCHEMA_TABLE_NAMES,
    "v3_run_outcomes",
    "gate_session_heads",
    "gate_session_revisions",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteOutcomeAttributionV3Error(V3ContractError):
    """Stable base error for the SQLite OutcomeAttribution ledger."""


class SQLiteOutcomeAttributionV3SchemaError(
    SQLiteOutcomeAttributionV3Error
):
    pass


class SQLiteOutcomeAttributionV3ConflictError(
    SQLiteOutcomeAttributionV3Error
):
    pass


class SQLiteOutcomeAttributionV3NotFoundError(
    SQLiteOutcomeAttributionV3Error
):
    pass


class SQLiteOutcomeAttributionV3PersistenceError(
    SQLiteOutcomeAttributionV3Error
):
    pass


@dataclass(frozen=True)
class SQLiteOutcomeAttributionWrite:
    attribution: OutcomeAttribution
    inserted: bool


def _synchronized(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _validate_attribution_descriptor(value: object) -> int:
    if type(value) is not str:
        return 0
    try:
        loads_outcome_attribution(value)
    except (OutcomeContractError, TypeError, ValueError):
        return 0
    return 1


def _attribution_row_values(
    attribution: OutcomeAttribution,
) -> tuple[object, ...]:
    return (
        attribution.attribution_id,
        attribution.run_outcome_id,
        attribution.usage_decision_id,
        json.dumps(
            list(attribution.memory_revision_ids),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        attribution.claim_strength,
        attribution.effect,
        attribution.method,
        attribution.evaluator_id,
        attribution.evaluator_version,
        attribution.verifier_id,
        json.dumps(
            list(attribution.evidence_artifact_sha256s),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        json.dumps(
            attribution.confidence,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        attribution.reason,
        attribution.recorded_at,
        dumps_outcome_attribution(attribution),
    )


def _validate_attribution_row(*values: object) -> int:
    if len(values) != 15 or type(values[14]) is not str:
        return 0
    try:
        attribution = loads_outcome_attribution(values[14])
    except (OutcomeContractError, TypeError, ValueError):
        return 0
    return int(values == _attribution_row_values(attribution))


def _validate_run_outcome_row(*values: object) -> int:
    try:
        SQLiteOutcomeV3Repository._outcome_from_row(values)
    except (
        SQLiteOutcomeV3PersistenceError,
        TypeError,
        ValueError,
    ):
        return 0
    return 1


def _time_not_before(
    recorded_at: object,
    measured_at: object,
) -> int:
    try:
        return int(
            parse_rfc3339(recorded_at) >= parse_rfc3339(measured_at)
        )
    except (TypeError, ValueError):
        return 0


def _register_validation_functions(
    connection: sqlite3.Connection,
) -> None:
    connection.create_function(
        "tbm_validate_outcome_attribution",
        1,
        _validate_attribution_descriptor,
        deterministic=True,
    )
    connection.create_function(
        "tbm_validate_outcome_attribution_row",
        15,
        _validate_attribution_row,
        deterministic=True,
    )
    connection.create_function(
        "tbm_validate_run_outcome_row",
        16,
        _validate_run_outcome_row,
        deterministic=True,
    )
    connection.create_function(
        "tbm_outcome_attribution_time_not_before",
        2,
        _time_not_before,
        deterministic=True,
    )


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
        raise SQLiteOutcomeAttributionV3SchemaError(
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
            "SQLite OutcomeAttribution schema has an invalid definition",
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
        raise SQLiteOutcomeAttributionV3SchemaError(
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
            _MISSING_SCHEMA_MESSAGE,
        )
    table_placeholders = ", ".join("?" for _ in _SCHEMA_TABLE_NAMES)
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL "
        f"AND tbl_name IN ({table_placeholders}) "
        f"AND name NOT IN ({placeholders}) LIMIT 1",
        (*_SCHEMA_TABLE_NAMES, *_SCHEMA_OBJECT_NAMES),
    )
    if cursor.fetchone() is not None:
        raise SQLiteOutcomeAttributionV3SchemaError(
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
            "SQLite OutcomeAttribution schema has an unexpected object",
        )
    forbidden_placeholders = ", ".join(
        "?" for _ in _TEMP_FORBIDDEN_NAMES
    )
    cursor.execute(
        "SELECT name FROM sqlite_temp_master "
        f"WHERE name IN ({forbidden_placeholders}) "
        f"OR tbl_name IN ({forbidden_placeholders}) LIMIT 1",
        (*_TEMP_FORBIDDEN_NAMES, *_TEMP_FORBIDDEN_NAMES),
    )
    if cursor.fetchone() is not None:
        raise SQLiteOutcomeAttributionV3SchemaError(
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
            "SQLite OutcomeAttribution schema has a temporary shadow",
        )
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                "SQLite OutcomeAttribution schema has an invalid shape",
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
            _register_validation_functions(connection)
            for resource in (
                _GATE_SCHEMA_RESOURCE,
                _OUTCOME_SCHEMA_RESOURCE,
                _SCHEMA_RESOURCE,
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
        raise SQLiteOutcomeAttributionV3SchemaError(
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
            "could not validate canonical SQLite OutcomeAttribution schema",
        ) from error


class SQLiteOutcomeAttributionV3Repository:
    """Immutable SQLite OutcomeAttribution ledger over durable outcomes."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        owns_connection: bool = False,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a sqlite3.Connection")
        try:
            _register_validation_functions(connection)
            if not connection.in_transaction:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA recursive_triggers = ON")
        except sqlite3.Error as error:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "could not configure SQLite OutcomeAttribution storage",
            ) from error
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False
        self._lock = RLock()
        self._savepoint_number = 0
        self._outcomes = SQLiteOutcomeV3Repository(connection)
        self._outcomes._lock = self._lock
        self._outcomes._gate_sessions._lock = self._lock

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        **kwargs: object,
    ) -> SQLiteOutcomeAttributionV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            _register_validation_functions(connection)
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
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "failed to connect to SQLite OutcomeAttribution storage",
            ) from error
        return cls(connection, owns_connection=True)

    @property
    def outcomes(self) -> SQLiteOutcomeV3Repository:
        self._require_open()
        return self._outcomes

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_CLOSED",
                "SQLite OutcomeAttribution repository is closed",
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.Error as error:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_CLOSED",
                "SQLite OutcomeAttribution repository is closed",
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
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite OutcomeAttribution "
                f"connection: {close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = (
                "tbm_sqlite_outcome_attribution_"
                f"{self._savepoint_number}"
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
                        "failed to clean up SQLite OutcomeAttribution "
                        f"savepoint: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        error,
                        context="the outer SQLite transaction",
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
                            "OutcomeAttribution savepoint: "
                            f"{cleanup_error}"
                        )
                        self._rollback_connection_or_close(
                            error,
                            context="the outer SQLite transaction",
                        )
                    raise
            return
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the SQLite OutcomeAttribution transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the SQLite OutcomeAttribution transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                "SQLite OutcomeAttribution requires foreign keys",
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                "SQLite OutcomeAttribution requires recursive triggers",
            )
        try:
            self._outcomes._require_schema(cursor)
        except (
            SQLiteGateSessionSchemaError,
            SQLiteOutcomeV3SchemaError,
        ) as error:
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                "SQLite outcome dependency failed schema validation",
            ) from error
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_outcome_attribution_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [
            (
                SQLITE_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION,
                OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
            )
        ]:
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                "SQLite OutcomeAttribution metadata mismatch",
            )
        if _read_schema_definitions(
            cursor
        ) != _canonical_schema_definitions():
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                "SQLite OutcomeAttribution schema definitions do not "
                "match the canonical version",
            )

    @staticmethod
    def _attribution_row(
        attribution: OutcomeAttribution,
    ) -> tuple[object, ...]:
        return _attribution_row_values(attribution)

    @classmethod
    def _attribution_from_row(
        cls,
        row: tuple[object, ...],
    ) -> OutcomeAttribution:
        if len(row) != 15 or type(row[14]) is not str:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "OutcomeAttribution row has an invalid shape",
            )
        try:
            attribution = loads_outcome_attribution(row[14])
        except OutcomeContractError as error:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "stored OutcomeAttribution failed contract validation",
            ) from error
        if row != cls._attribution_row(attribution):
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "OutcomeAttribution columns do not match its descriptor",
            )
        return attribution

    @classmethod
    def _select_optional(
        cls,
        cursor: sqlite3.Cursor,
        attribution_id: str,
    ) -> OutcomeAttribution | None:
        cursor.execute(
            "SELECT attribution_id, run_outcome_id, usage_decision_id, "
            "memory_revision_ids_json, claim_strength, effect, method, "
            "evaluator_id, evaluator_version, verifier_id, "
            "evidence_artifact_sha256s_json, confidence_json, reason, "
            "recorded_at, descriptor FROM v3_outcome_attributions "
            "WHERE attribution_id = ?",
            (attribution_id,),
        )
        row = cursor.fetchone()
        return None if row is None else cls._attribution_from_row(row)

    @classmethod
    def _select(
        cls,
        cursor: sqlite3.Cursor,
        attribution_id: str,
    ) -> OutcomeAttribution:
        attribution = cls._select_optional(cursor, attribution_id)
        if attribution is None:
            raise SQLiteOutcomeAttributionV3NotFoundError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_NOT_FOUND",
                "OutcomeAttribution was not found",
            )
        return attribution

    def _verify_linkage(
        self,
        cursor: sqlite3.Cursor,
        attribution: OutcomeAttribution,
    ) -> None:
        try:
            outcome = self._outcomes._select_outcome(
                cursor,
                attribution.run_outcome_id,
            )
            session = self._outcomes._gate_sessions._select_current(
                cursor,
                outcome.session_id,
            )
            verify_outcome_attribution(attribution, outcome, session)
        except (
            OutcomeContractError,
            SQLiteGateSessionNotFoundError,
            SQLiteOutcomeV3NotFoundError,
        ) as error:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_LINKAGE",
                "OutcomeAttribution durable linkage is invalid",
            ) from error
        except (
            SQLiteGateSessionPersistenceError,
            SQLiteOutcomeV3PersistenceError,
        ) as error:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_DEPENDENCY",
                "SQLite outcome dependency failed validation",
            ) from error

    @_synchronized
    def put_attribution(
        self,
        attribution: OutcomeAttribution,
    ) -> SQLiteOutcomeAttributionWrite:
        self._require_open()
        if type(attribution) is not OutcomeAttribution:
            raise TypeError("attribution must be exactly OutcomeAttribution")
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    existing = self._select_optional(
                        cursor,
                        attribution.attribution_id,
                    )
                    if existing is not None:
                        if existing != attribution:
                            raise SQLiteOutcomeAttributionV3ConflictError(
                                "TBM_SQLITE_OUTCOME_ATTRIBUTION_CONFLICT",
                                "OutcomeAttribution ID has different content",
                            )
                        self._verify_linkage(cursor, existing)
                        return SQLiteOutcomeAttributionWrite(existing, False)
                    self._verify_linkage(cursor, attribution)
                    cursor.execute(
                        "INSERT INTO v3_outcome_attributions ("
                        "attribution_id, run_outcome_id, usage_decision_id, "
                        "memory_revision_ids_json, claim_strength, effect, "
                        "method, evaluator_id, evaluator_version, verifier_id, "
                        "evidence_artifact_sha256s_json, confidence_json, "
                        "reason, recorded_at, descriptor"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._attribution_row(attribution),
                    )
                    retained = self._select(
                        cursor,
                        attribution.attribution_id,
                    )
                    if retained != attribution:
                        raise SQLiteOutcomeAttributionV3PersistenceError(
                            "TBM_SQLITE_OUTCOME_ATTRIBUTION_READBACK",
                            "OutcomeAttribution read-back changed",
                        )
                    self._verify_linkage(cursor, retained)
                    return SQLiteOutcomeAttributionWrite(retained, True)
        except (
            SQLiteOutcomeAttributionV3ConflictError,
            SQLiteOutcomeAttributionV3NotFoundError,
            SQLiteOutcomeAttributionV3PersistenceError,
            SQLiteOutcomeAttributionV3SchemaError,
        ):
            raise
        except (
            SQLiteGateSessionPersistenceError,
            SQLiteOutcomeV3PersistenceError,
        ) as error:
            raise SQLiteOutcomeAttributionV3PersistenceError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_DEPENDENCY",
                "SQLite outcome dependency failed",
            ) from error
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to store SQLite OutcomeAttribution",
            )

    @_synchronized
    def get_attribution(
        self,
        attribution_id: str,
    ) -> OutcomeAttribution:
        self._require_open()
        if type(attribution_id) is not str:
            raise ValueError("attribution_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    attribution = self._select(cursor, attribution_id)
                    self._verify_linkage(cursor, attribution)
                    return attribution
        except (
            SQLiteOutcomeAttributionV3NotFoundError,
            SQLiteOutcomeAttributionV3PersistenceError,
            SQLiteOutcomeAttributionV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to load SQLite OutcomeAttribution",
            )

    @_synchronized
    def list_attributions(
        self,
        run_outcome_id: str,
    ) -> tuple[OutcomeAttribution, ...]:
        self._require_open()
        if type(run_outcome_id) is not str:
            raise ValueError("run_outcome_id must be a string")
        try:
            with self._transaction(write=False):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    try:
                        self._outcomes._select_outcome(
                            cursor,
                            run_outcome_id,
                        )
                    except SQLiteOutcomeV3NotFoundError as error:
                        raise SQLiteOutcomeAttributionV3NotFoundError(
                            "TBM_SQLITE_OUTCOME_ATTRIBUTION_OUTCOME_NOT_FOUND",
                            "RunOutcome was not found",
                        ) from error
                    cursor.execute(
                        "SELECT attribution_id, run_outcome_id, "
                        "usage_decision_id, memory_revision_ids_json, "
                        "claim_strength, effect, method, evaluator_id, "
                        "evaluator_version, verifier_id, "
                        "evidence_artifact_sha256s_json, confidence_json, "
                        "reason, recorded_at, descriptor "
                        "FROM v3_outcome_attributions "
                        "WHERE run_outcome_id = ? "
                        "ORDER BY recorded_at, attribution_id",
                        (run_outcome_id,),
                    )
                    values = tuple(
                        self._attribution_from_row(row)
                        for row in cursor.fetchall()
                    )
                    for attribution in values:
                        self._verify_linkage(cursor, attribution)
                    return values
        except (
            SQLiteOutcomeAttributionV3NotFoundError,
            SQLiteOutcomeAttributionV3PersistenceError,
            SQLiteOutcomeAttributionV3SchemaError,
        ):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(
                error,
                "failed to list SQLite OutcomeAttributions",
            )

    @staticmethod
    def _raise_database_error(
        error: sqlite3.DatabaseError,
        message: str,
    ) -> NoReturn:
        if _is_schema_error(error):
            raise SQLiteOutcomeAttributionV3SchemaError(
                "TBM_SQLITE_OUTCOME_ATTRIBUTION_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise SQLiteOutcomeAttributionV3PersistenceError(
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outcomes.close()
        if self._owns_connection:
            self._connection.close()

    @_synchronized
    def __enter__(self) -> SQLiteOutcomeAttributionV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "SQLITE_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION",
    "SQLiteOutcomeAttributionV3ConflictError",
    "SQLiteOutcomeAttributionV3Error",
    "SQLiteOutcomeAttributionV3NotFoundError",
    "SQLiteOutcomeAttributionV3PersistenceError",
    "SQLiteOutcomeAttributionV3Repository",
    "SQLiteOutcomeAttributionV3SchemaError",
    "SQLiteOutcomeAttributionWrite",
]
