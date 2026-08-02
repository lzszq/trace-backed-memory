from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache, wraps
import hashlib
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from .event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
)
from .gate_evaluation_v3 import SemanticGateAttempt
from .ledger_port_v1 import (
    EventLedgerPortError,
    LedgerAccessContext,
    LedgerAppendRequest,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from .resources import PackagedResourceError, read_packaged_resource
from .semantic_gate_artifact_v3 import (
    SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION,
    SemanticGateArtifactBinding,
    StoredSemanticGateAttemptArtifacts,
    StoredSemanticGateArtifact,
    dumps_semantic_gate_artifact_binding,
    loads_semantic_gate_artifact_binding,
    verify_semantic_gate_artifact_binding,
)
from .semantic_gate_attempt_event_v1 import (
    SemanticGateAttemptEventRef,
    SemanticGateAttemptEventV1Error,
    build_semantic_gate_attempt_event,
    parse_semantic_gate_attempt_event,
    semantic_gate_attempt_event_id,
    semantic_gate_attempt_event_ref,
    semantic_gate_attempt_stream_id,
    verify_semantic_gate_event_scope,
    verify_semantic_gate_system_parent,
)
from .sqlite_semantic_gate_v3 import (
    SQLiteSemanticGateV3Error,
    SQLiteSemanticGateV3NotFoundError,
    SQLiteSemanticGateV3Repository,
    SQLiteSemanticGateV3StoreResult,
)


SQLITE_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION = 1
_MISSING_SCHEMA_MESSAGE = (
    "SQLite semantic Gate artifact v3 schema is missing or incomplete"
)
_ATTEMPT_ID_RE = re.compile(r"semantic_attempt_sha256_[0-9a-f]{64}")
_ARTIFACT_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_semantic_gate_artifacts_schema",
    "v3_semantic_gate_artifact_bindings",
    "v3_semantic_gate_artifact_bindings_artifact",
    "v3_semantic_gate_artifact_bindings_immutable_delete",
    "v3_semantic_gate_artifact_bindings_immutable_insert_conflict",
    "v3_semantic_gate_artifact_bindings_immutable_update",
    "v3_semantic_gate_artifact_bindings_match_attempt",
    "v3_semantic_gate_artifacts",
    "v3_semantic_gate_artifacts_immutable_delete",
    "v3_semantic_gate_artifacts_immutable_insert_conflict",
    "v3_semantic_gate_artifacts_immutable_update",
    "v3_semantic_gate_artifacts_verify_content",
    "v3_semantic_gate_artifacts_schema_immutable_delete",
    "v3_semantic_gate_artifacts_schema_immutable_insert_conflict",
    "v3_semantic_gate_artifacts_schema_immutable_update",
    "v3_semantic_gate_artifacts_schema_requires_attempts",
)
_ARTIFACT_SCHEMA_TABLE_NAMES = (
    "trace_backed_memory_v3_semantic_gate_artifacts_schema",
    "v3_semantic_gate_artifact_bindings",
    "v3_semantic_gate_artifacts",
)
_TEMP_FORBIDDEN_NAMES = (
    *_ARTIFACT_SCHEMA_OBJECT_NAMES,
    "trace_backed_memory_v3_gate_evidence_schema",
    "trace_backed_memory_v3_semantic_gate_schema",
    "v3_retrieval_snapshots",
    "v3_semantic_gate_attempt_heads",
    "v3_semantic_gate_attempts",
    "v3_system_gate_evaluations",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteSemanticGateArtifactV3Error(RuntimeError):
    pass


class SQLiteSemanticGateArtifactV3SchemaError(
    SQLiteSemanticGateArtifactV3Error
):
    pass


class SQLiteSemanticGateArtifactV3ConflictError(
    SQLiteSemanticGateArtifactV3Error
):
    pass


class SQLiteSemanticGateArtifactV3NotFoundError(
    SQLiteSemanticGateArtifactV3Error
):
    pass


class SQLiteSemanticGateArtifactV3PersistenceError(
    SQLiteSemanticGateArtifactV3Error
):
    pass


@dataclass(frozen=True)
class SQLiteSemanticGateArtifactV3StoreResult:
    attempt: SQLiteSemanticGateV3StoreResult
    prompt_artifact_inserted: bool
    prompt_binding_inserted: bool
    response_artifact_inserted: bool
    response_binding_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteSemanticGateArtifactV3SchemaError(
            "SQLite semantic Gate artifact schema has an invalid definition"
        )
    return "".join(value.split()).casefold()


def _sqlite_sha256(value: object) -> str:
    if type(value) is not bytes:
        raise ValueError("tbm_sha256 requires a BLOB")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_artifact_schema_definitions(
    cursor: sqlite3.Cursor,
) -> tuple[tuple[str, str, str, str], ...]:
    placeholders = ", ".join(
        "?" for _ in _ARTIFACT_SCHEMA_OBJECT_NAMES
    )
    cursor.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        f"WHERE name IN ({placeholders}) ORDER BY name",
        _ARTIFACT_SCHEMA_OBJECT_NAMES,
    )
    rows = cursor.fetchall()
    if len(rows) != len(_ARTIFACT_SCHEMA_OBJECT_NAMES):
        raise SQLiteSemanticGateArtifactV3SchemaError(
            _MISSING_SCHEMA_MESSAGE
        )
    table_placeholders = ", ".join(
        "?" for _ in _ARTIFACT_SCHEMA_TABLE_NAMES
    )
    object_placeholders = ", ".join(
        "?" for _ in _ARTIFACT_SCHEMA_OBJECT_NAMES
    )
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('index', 'trigger') "
        f"AND tbl_name IN ({table_placeholders}) "
        "AND sql IS NOT NULL "
        f"AND name NOT IN ({object_placeholders}) LIMIT 1",
        (
            *_ARTIFACT_SCHEMA_TABLE_NAMES,
            *_ARTIFACT_SCHEMA_OBJECT_NAMES,
        ),
    )
    if cursor.fetchone() is not None:
        raise SQLiteSemanticGateArtifactV3SchemaError(
            "SQLite semantic Gate artifact schema contains an "
            "unexpected managed object"
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
        raise SQLiteSemanticGateArtifactV3SchemaError(
            "SQLite semantic Gate artifact schema contains a temporary "
            "shadow or managed object"
        )
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteSemanticGateArtifactV3SchemaError(
                "SQLite semantic Gate artifact schema definition "
                "has invalid shape"
            )
        definitions.append(
            (row[0], row[1], row[2], _normalized_schema_sql(row[3]))
        )
    return tuple(definitions)


@lru_cache(maxsize=1)
def _canonical_artifact_schema_definitions() -> tuple[
    tuple[str, str, str, str],
    ...,
]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            for resource in (
                "schemas/sqlite-v3-gate-evidence.sql",
                "schemas/sqlite-v3-semantic-gate.sql",
                "schemas/sqlite-v3-semantic-gate-artifacts.sql",
            ):
                connection.executescript(
                    read_packaged_resource(resource).decode("utf-8")
                )
            with closing(connection.cursor()) as cursor:
                return _read_artifact_schema_definitions(cursor)
        finally:
            connection.close()
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        raise SQLiteSemanticGateArtifactV3SchemaError(
            "could not validate canonical SQLite semantic Gate "
            "artifact schema"
        ) from error


class SQLiteSemanticGateArtifactV3Repository:
    """Atomic SemanticGateAttempt and exact artifact-byte SQLite store."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        owns_connection: bool = False,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a sqlite3.Connection")
        try:
            connection.create_function(
                "tbm_sha256",
                1,
                _sqlite_sha256,
                deterministic=True,
            )
        except sqlite3.Error as error:
            raise ValueError(
                "connection cannot register SQLite artifact verifier"
            ) from error
        self._connection = connection
        self._owns_connection = owns_connection
        self._semantic_repository = SQLiteSemanticGateV3Repository(connection)
        self._lock = RLock()
        self._closed = False
        self._savepoint_number = 0
        self._event_first = False
        self._event_context: ContextVar[EventTrustedContext | None] = ContextVar(
            f"tbm_sqlite_semantic_gate_event_context_{id(self)}",
            default=None,
        )

    @_synchronized
    def enable_event_first(self) -> None:
        self._require_open()
        if self._connection.in_transaction:
            raise SQLiteSemanticGateArtifactV3ConflictError(
                "event-first mode cannot be enabled during a transaction"
            )
        self._event_first = True

    @contextmanager
    def bind_event_context(
        self,
        trusted_context: EventTrustedContext,
    ) -> Iterator[None]:
        if type(trusted_context) is not EventTrustedContext:
            raise ValueError("trusted_context must be exactly EventTrustedContext")
        token = self._event_context.set(trusted_context)
        try:
            yield
        finally:
            self._event_context.reset(token)

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        **kwargs: object,
    ) -> SQLiteSemanticGateArtifactV3Repository:
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
                    "schemas/sqlite-v3-semantic-gate-artifacts.sql",
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
            raise SQLiteSemanticGateArtifactV3PersistenceError(
                "failed to connect to SQLite semantic Gate artifact storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteSemanticGateArtifactV3Error(
                "SQLite semantic Gate artifact repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteSemanticGateArtifactV3Error(
                "SQLite semantic Gate artifact repository is closed"
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
                "failed to close unusable SQLite semantic Gate artifact "
                f"connection: {close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = (
                "tbm_sqlite_semantic_gate_artifact_v3_"
                f"{self._savepoint_number}"
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
                        "failed to clean up SQLite semantic Gate artifact "
                        f"savepoint {savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context="the outer SQLite transaction",
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
                context="the top-level SQLite semantic Gate artifact transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context=(
                        "the top-level SQLite semantic Gate artifact transaction"
                    ),
                )
                raise

    def _require_artifact_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteSemanticGateArtifactV3SchemaError(
                "SQLite semantic Gate artifacts require foreign keys"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteSemanticGateArtifactV3SchemaError(
                "SQLite semantic Gate artifacts require recursive triggers"
            )
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_semantic_gate_artifacts_schema "
            "WHERE singleton = 1"
        )
        if cursor.fetchall() != [
            (1, SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION)
        ]:
            raise SQLiteSemanticGateArtifactV3SchemaError(
                "SQLite semantic Gate artifact metadata mismatch"
            )
        if (
            _read_artifact_schema_definitions(cursor)
            != _canonical_artifact_schema_definitions()
        ):
            raise SQLiteSemanticGateArtifactV3SchemaError(
                "SQLite semantic Gate artifact schema definitions "
                "do not match the canonical version"
            )

    @staticmethod
    def _artifact_row(
        stored: StoredSemanticGateArtifact,
    ) -> tuple[object, ...]:
        artifact = stored.binding.artifact
        return (
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.size_bytes,
            artifact.media_type,
            artifact.classification,
            artifact.created_at,
            artifact.encryption_key_id,
            artifact.redaction_policy_id,
            stored.content,
        )

    @staticmethod
    def _binding_row(
        binding: SemanticGateArtifactBinding,
    ) -> tuple[object, ...]:
        descriptor = dumps_semantic_gate_artifact_binding(binding)
        return (
            binding.attempt_id,
            binding.artifact_role,
            binding.artifact.artifact_id,
            binding.artifact.content_sha256,
            descriptor,
        )

    @staticmethod
    def _validate_stored(
        attempt: SemanticGateAttempt,
        stored: StoredSemanticGateArtifact,
        *,
        expected_role: str,
    ) -> None:
        if type(stored) is not StoredSemanticGateArtifact:
            raise ValueError(
                f"{expected_role} must be exactly StoredSemanticGateArtifact"
            )
        if stored.binding.artifact_role != expected_role:
            raise ValueError(
                f"{expected_role} artifact has the wrong role"
            )
        if stored.binding.artifact.classification not in {
            "public",
            "internal",
        }:
            raise ValueError(
                "SQLite semantic Gate artifact storage does not provide "
                "encryption at rest"
            )
        if not verify_semantic_gate_artifact_binding(
            stored.binding,
            attempt,
            stored.content,
        ):
            raise ValueError(
                f"{expected_role} artifact does not match Semantic Gate attempt"
            )

    @classmethod
    def _validate_bundle(
        cls,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> None:
        if type(attempt) is not SemanticGateAttempt:
            raise ValueError("attempt must be exactly SemanticGateAttempt")
        cls._validate_stored(attempt, prompt, expected_role="prompt")
        if attempt.status == "succeeded":
            if response is None:
                raise ValueError(
                    "succeeded Semantic Gate attempt requires response artifact"
                )
            cls._validate_stored(
                attempt,
                response,
                expected_role="response",
            )
        elif response is not None:
            raise ValueError(
                "failed Semantic Gate attempt forbids response artifact"
            )

    def _put_artifact(
        self,
        cursor: sqlite3.Cursor,
        stored: StoredSemanticGateArtifact,
    ) -> bool:
        artifact = stored.binding.artifact
        cursor.execute(
            "SELECT artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content "
            "FROM v3_semantic_gate_artifacts "
            "WHERE artifact_id = ? OR content_sha256 = ?",
            (artifact.artifact_id, artifact.content_sha256),
        )
        rows = cursor.fetchall()
        expected = self._artifact_row(stored)
        if rows:
            if rows != [expected]:
                raise SQLiteSemanticGateArtifactV3ConflictError(
                    "Semantic Gate artifact identity has conflicting content"
                )
            return False
        cursor.execute(
            "INSERT INTO v3_semantic_gate_artifacts ("
            "artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            expected,
        )
        return True

    def _put_binding(
        self,
        cursor: sqlite3.Cursor,
        binding: SemanticGateArtifactBinding,
    ) -> bool:
        cursor.execute(
            "SELECT attempt_id, artifact_role, artifact_id, "
            "content_sha256, descriptor "
            "FROM v3_semantic_gate_artifact_bindings "
            "WHERE attempt_id = ? AND artifact_role = ?",
            (binding.attempt_id, binding.artifact_role),
        )
        row = cursor.fetchone()
        expected = self._binding_row(binding)
        if row is not None:
            if row != expected:
                raise SQLiteSemanticGateArtifactV3ConflictError(
                    "Semantic Gate artifact binding has conflicting content"
                )
            return False
        cursor.execute(
            "INSERT INTO v3_semantic_gate_artifact_bindings ("
            "attempt_id, artifact_role, artifact_id, content_sha256, descriptor"
            ") VALUES (?, ?, ?, ?, ?)",
            expected,
        )
        return True

    def _load_stored(
        self,
        cursor: sqlite3.Cursor,
        attempt: SemanticGateAttempt,
        role: str,
    ) -> StoredSemanticGateArtifact | None:
        cursor.execute(
            "SELECT binding.attempt_id, binding.artifact_role, "
            "binding.artifact_id, binding.content_sha256, "
            "binding.descriptor, artifact.size_bytes, artifact.media_type, "
            "artifact.classification, artifact.created_at, "
            "artifact.encryption_key_id, artifact.redaction_policy_id, "
            "artifact.content "
            "FROM v3_semantic_gate_artifact_bindings AS binding "
            "JOIN v3_semantic_gate_artifacts AS artifact "
            "ON artifact.artifact_id = binding.artifact_id "
            "AND artifact.content_sha256 = binding.content_sha256 "
            "WHERE binding.attempt_id = ? AND binding.artifact_role = ?",
            (attempt.attempt_id, role),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if len(row) != 12 or type(row[4]) is not str or type(row[11]) is not bytes:
            raise SQLiteSemanticGateArtifactV3PersistenceError(
                "stored Semantic Gate artifact row has invalid shape"
            )
        try:
            binding = loads_semantic_gate_artifact_binding(row[4])
            stored = StoredSemanticGateArtifact(binding, row[11])
        except ValueError as error:
            raise SQLiteSemanticGateArtifactV3PersistenceError(
                "stored Semantic Gate artifact failed validation"
            ) from error
        expected_binding = self._binding_row(binding)
        expected_artifact = self._artifact_row(stored)
        if row[:5] != expected_binding or row[5:] != expected_artifact[2:]:
            raise SQLiteSemanticGateArtifactV3PersistenceError(
                "stored Semantic Gate artifact columns do not match descriptor"
            )
        if not verify_semantic_gate_artifact_binding(
            binding,
            attempt,
            stored.content,
        ):
            raise SQLiteSemanticGateArtifactV3PersistenceError(
                "stored Semantic Gate artifact does not match attempt"
            )
        return stored

    def _event_access(
        self,
    ) -> tuple[EventTrustedContext, LedgerAccessContext]:
        trusted_context = self._event_context.get()
        if trusted_context is None:
            raise SQLiteSemanticGateArtifactV3ConflictError(
                "event-first Semantic Gate mutation requires trusted event context"
            )
        return trusted_context, LedgerAccessContext(
            partition=LedgerTenantPartition(
                trusted_context.organization_id,
                trusted_context.tenant_id,
                trusted_context.repository_id,
                trusted_context.environment_id,
            ),
            principal_id=trusted_context.principal_id,
            agent_client_id=trusted_context.agent_client_id,
            actor_type=trusted_context.actor_type,
            actor_id=trusted_context.actor_id,
            authorization_decision_id=(
                trusted_context.authorization_decision_id
            ),
            classification_filter=LedgerClassificationFilter(
                ("public", "internal", "confidential", "restricted")
            ),
        )

    @staticmethod
    def _select_event_by_id(
        cursor: sqlite3.Cursor,
        ledger: object,
        event_id: str,
    ) -> CanonicalEvent | None:
        cursor.execute(
            f"SELECT {ledger._event_select()} FROM v3_event_ledger_events "
            "WHERE event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        return None if row is None else ledger._event_from_row(cursor, row)

    @staticmethod
    def _verify_retained_event(
        event: CanonicalEvent,
        expected_ref: SemanticGateAttemptEventRef,
        trusted_context: EventTrustedContext,
    ) -> None:
        try:
            verify_semantic_gate_event_scope(event, trusted_context)
            retained_ref = parse_semantic_gate_attempt_event(event)
        except Exception as error:
            raise SQLiteSemanticGateArtifactV3ConflictError(
                "retained Semantic Gate attempt event failed validation"
            ) from error
        if retained_ref != expected_ref:
            raise SQLiteSemanticGateArtifactV3ConflictError(
                "Semantic Gate attempt event has conflicting immutable content"
            )

    def _append_attempt_event(
        self,
        cursor: sqlite3.Cursor,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> None:
        if not self._event_first:
            return
        from .sqlite_event_ledger_v1 import SQLiteEventLedgerV1

        trusted_context, access = self._event_access()
        ledger = SQLiteEventLedgerV1(self._connection, access)
        try:
            ledger._require_schema(cursor)
            expected_ref = semantic_gate_attempt_event_ref(
                attempt,
                prompt,
                response,
            )
            system_gate_event = None
            if attempt.sequence == 1:
                system_gate_event = self._select_event_by_id(
                    cursor,
                    ledger,
                    expected_ref.causation_event_id,
                )
                verify_semantic_gate_system_parent(
                    attempt,
                    system_gate_event,
                    trusted_context,
                )
            retained = self._select_event_by_id(
                cursor,
                ledger,
                semantic_gate_attempt_event_id(attempt.attempt_id),
            )
            if retained is not None:
                self._verify_retained_event(
                    retained,
                    expected_ref,
                    trusted_context,
                )
                return
            stream_id = semantic_gate_attempt_stream_id(
                attempt.system_gate_evaluation_id
            )
            previous_event = ledger._select_head_event(cursor, stream_id)
            if attempt.sequence == 1:
                if previous_event is not None:
                    raise SQLiteSemanticGateArtifactV3ConflictError(
                        "Semantic Gate attempt stream already has a head"
                    )
            elif (
                previous_event is None
                or previous_event.event_id
                != semantic_gate_attempt_event_id(
                    attempt.previous_attempt_id or ""
                )
                or previous_event.stream_version != attempt.sequence - 1
            ):
                raise SQLiteSemanticGateArtifactV3ConflictError(
                    "Semantic Gate retry does not extend the event stream"
                )
            event = build_semantic_gate_attempt_event(
                attempt,
                prompt,
                response,
                system_gate_event=system_gate_event,
                previous_event=previous_event,
                global_position=ledger._select_global_position(cursor) + 1,
                trusted_context=trusted_context,
            )
            ledger._append_in_transaction(
                cursor,
                LedgerAppendRequest(
                    access=access,
                    stream_id=stream_id,
                    expected_stream_version=attempt.sequence - 1,
                    events=(event,),
                    idempotency=LedgerIdempotency(
                        event.idempotency_key_sha256,
                        event.request_sha256,
                    ),
                ),
            )
        finally:
            ledger.close()

    @_synchronized
    def store_attempt_with_artifacts(
        self,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> SQLiteSemanticGateArtifactV3StoreResult:
        self._require_open()
        self._validate_bundle(attempt, prompt, response)
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as event_cursor:
                    self._append_attempt_event(
                        event_cursor,
                        attempt,
                        prompt,
                        response,
                    )
                attempt_result = self._semantic_repository.store_attempt(
                    attempt
                )
                with closing(self._connection.cursor()) as cursor:
                    self._require_artifact_schema(cursor)
                    prompt_artifact_inserted = self._put_artifact(
                        cursor,
                        prompt,
                    )
                    prompt_binding_inserted = self._put_binding(
                        cursor,
                        prompt.binding,
                    )
                    response_artifact_inserted = False
                    response_binding_inserted = False
                    if response is not None:
                        response_artifact_inserted = self._put_artifact(
                            cursor,
                            response,
                        )
                        response_binding_inserted = self._put_binding(
                            cursor,
                            response.binding,
                        )
                    loaded_prompt = self._load_stored(
                        cursor,
                        attempt,
                        "prompt",
                    )
                    loaded_response = self._load_stored(
                        cursor,
                        attempt,
                        "response",
                    )
                    if loaded_prompt != prompt or loaded_response != response:
                        raise SQLiteSemanticGateArtifactV3PersistenceError(
                            "Semantic Gate artifact read-back does not match"
                        )
            return SQLiteSemanticGateArtifactV3StoreResult(
                attempt=attempt_result,
                prompt_artifact_inserted=prompt_artifact_inserted,
                prompt_binding_inserted=prompt_binding_inserted,
                response_artifact_inserted=response_artifact_inserted,
                response_binding_inserted=response_binding_inserted,
            )
        except (
            SemanticGateAttemptEventV1Error,
            EventLedgerPortError,
        ) as error:
            raise SQLiteSemanticGateArtifactV3ConflictError(
                "Semantic Gate attempt event conflicts with immutable storage"
            ) from error
        except (
            SQLiteSemanticGateArtifactV3Error,
            SQLiteSemanticGateV3Error,
            ValueError,
        ):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteSemanticGateArtifactV3ConflictError(
                "Semantic Gate artifact conflicts with immutable storage"
            ) from error
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def load_attempt_with_artifacts(
        self,
        attempt_id: str,
    ) -> StoredSemanticGateAttemptArtifacts:
        self._require_open()
        if (
            type(attempt_id) is not str
            or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None
        ):
            raise ValueError("attempt_id must be a v3 Semantic Gate attempt ID")
        try:
            with self._transaction(write=False):
                attempt = self._semantic_repository.load_attempt(attempt_id)
                with closing(self._connection.cursor()) as cursor:
                    self._require_artifact_schema(cursor)
                    prompt = self._load_stored(cursor, attempt, "prompt")
                    response = self._load_stored(cursor, attempt, "response")
                    if prompt is None:
                        raise SQLiteSemanticGateArtifactV3NotFoundError(
                            "Semantic Gate prompt artifact was not found"
                        )
                    if (
                        attempt.status == "succeeded"
                        and response is None
                    ):
                        raise SQLiteSemanticGateArtifactV3PersistenceError(
                            "succeeded attempt is missing response artifact"
                        )
                    if attempt.status == "failed" and response is not None:
                        raise SQLiteSemanticGateArtifactV3PersistenceError(
                            "failed attempt has a response artifact"
                        )
                    return StoredSemanticGateAttemptArtifacts(
                        attempt,
                        prompt,
                        response,
                    )
        except (
            SQLiteSemanticGateArtifactV3Error,
            SQLiteSemanticGateV3Error,
            ValueError,
        ):
            raise
        except sqlite3.Error as error:
            self._raise_sqlite(error)

    @_synchronized
    def load_attempt_chain(
        self,
        evaluation_id: str,
    ) -> tuple[SemanticGateAttempt, ...]:
        """Load the exact chain, or an empty tuple before its first attempt."""

        self._require_open()
        try:
            return self._semantic_repository.load_chain(evaluation_id)
        except SQLiteSemanticGateV3NotFoundError:
            return ()

    def _raise_sqlite(self, error: sqlite3.Error) -> NoReturn:
        message = str(error).casefold()
        if (
            "no such table" in message
            or "no such trigger" in message
            or "schema" in message
        ):
            raise SQLiteSemanticGateArtifactV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        raise SQLiteSemanticGateArtifactV3PersistenceError(
            "SQLite semantic Gate artifact operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteSemanticGateArtifactV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "SQLITE_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION",
    "SQLiteSemanticGateArtifactV3ConflictError",
    "SQLiteSemanticGateArtifactV3Error",
    "SQLiteSemanticGateArtifactV3NotFoundError",
    "SQLiteSemanticGateArtifactV3PersistenceError",
    "SQLiteSemanticGateArtifactV3Repository",
    "SQLiteSemanticGateArtifactV3SchemaError",
    "SQLiteSemanticGateArtifactV3StoreResult",
]
