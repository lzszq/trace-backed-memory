from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache, wraps
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Literal, ParamSpec, TypeVar

from ._ingestion import parse_bounded_json
from .authorization_v3 import (
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    dumps_authorization_decision,
    dumps_authorization_policy,
    loads_authorization_decision,
    loads_authorization_policy,
)
from .evidence_v3 import (
    StructuredRegressionEvidence,
    dumps_structured_regression_evidence,
)
from .fix_evidence_v3 import FixEvidence, dumps_fix_evidence
from .memory_publication_v3 import (
    MemoryRevisionActivation,
    MemoryRevisionApproval,
    StoredMemoryRevisionActivationPublication,
    StoredMemoryRevisionApprovalPublication,
    activate_memory_revision,
    approve_memory_revision,
    dumps_memory_revision_activation,
    dumps_memory_revision_approval,
    loads_memory_revision_activation,
    loads_memory_revision_approval,
)
from .memory_revision_v3 import MemoryRevision, dumps_memory_revision
from .resources import PackagedResourceError, read_packaged_resource
from .sqlite_memory_revision_v3 import (
    SQLiteMemoryRevisionV3SchemaError,
    _canonical_schema_definitions as _revision_schema_definitions,
    _read_schema_definitions as _read_revision_schema_definitions,
)


SQLITE_MEMORY_PUBLICATION_V3_SCHEMA_VERSION = 1
_SCHEMA_RESOURCE = "schemas/sqlite-v3-memory-publication.sql"
_MISSING_SCHEMA_MESSAGE = (
    "SQLite memory publication v3 schema is missing or incomplete"
)
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_memory_publication_schema",
    "v3_memory_revision_activation_heads",
    "v3_memory_revision_activations",
    "v3_memory_revision_activations_immutable_delete",
    "v3_memory_revision_activations_immutable_update",
    "v3_memory_revision_activations_validate_insert",
    "v3_memory_revision_approvals",
    "v3_memory_revision_approvals_immutable_delete",
    "v3_memory_revision_approvals_immutable_update",
    "v3_memory_revision_approvals_validate_insert",
    "v3_memory_revision_heads_no_delete",
    "v3_memory_revision_heads_validate_advance",
    "v3_memory_revision_heads_validate_insert",
)
_CONTROLLED_TABLES = (
    "trace_backed_memory_v3_memory_publication_schema",
    "v3_memory_revision_approvals",
    "v3_memory_revision_activations",
    "v3_memory_revision_activation_heads",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")
AttestationKind = Literal["approval", "activation"]
AttestationVerifier = Callable[[AttestationKind, str, str, str], bool]
_CONNECTION_LOCKS_GUARD = RLock()
_CONNECTION_LOCKS: dict[sqlite3.Connection, tuple[Any, int]] = {}


class SQLiteMemoryPublicationV3Error(RuntimeError):
    pass


class SQLiteMemoryPublicationV3SchemaError(
    SQLiteMemoryPublicationV3Error
):
    pass


class SQLiteMemoryPublicationV3ConflictError(
    SQLiteMemoryPublicationV3Error
):
    pass


class SQLiteMemoryPublicationV3NotFoundError(
    SQLiteMemoryPublicationV3Error
):
    pass


class SQLiteMemoryPublicationV3PersistenceError(
    SQLiteMemoryPublicationV3Error
):
    pass


class SQLiteMemoryPublicationV3AttestationError(
    SQLiteMemoryPublicationV3Error
):
    pass


@dataclass(frozen=True)
class SQLiteMemoryPublicationV3ApprovalResult:
    approval: MemoryRevisionApproval
    inserted: bool
    attestation_verified_by: str


@dataclass(frozen=True)
class SQLiteMemoryPublicationV3ActivationResult:
    activation: MemoryRevisionActivation
    inserted: bool
    attestation_verified_by: str


@dataclass(frozen=True)
class SQLiteMemoryPublicationV3Head:
    tenant_id: str
    repository_id: str | None
    memory_id: str
    current_revision_number: int
    current_revision_id: str
    current_activation_id: str


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


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteMemoryPublicationV3SchemaError(
            "SQLite memory publication schema has an invalid definition"
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
        raise SQLiteMemoryPublicationV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteMemoryPublicationV3SchemaError(
                "SQLite memory publication schema definition has invalid shape"
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
        raise SQLiteMemoryPublicationV3SchemaError(
            "SQLite memory publication schema contains unexpected objects"
        )
    cursor.execute(
        "SELECT name FROM sqlite_temp_master "
        f"WHERE tbl_name IN ({controlled}) OR name IN ({controlled}) LIMIT 1",
        (*_CONTROLLED_TABLES, *_CONTROLLED_TABLES),
    )
    if cursor.fetchone() is not None:
        raise SQLiteMemoryPublicationV3SchemaError(
            "SQLite memory publication schema forbids temporary shadows"
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
        raise SQLiteMemoryPublicationV3SchemaError(
            "could not validate canonical SQLite memory publication schema"
        ) from error


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _request_descriptor(request: AuthorizationRequest) -> str:
    if type(request) is not AuthorizationRequest:
        raise ValueError("request must be exactly AuthorizationRequest")
    return _canonical_json(request.to_dict())


def _loads_request(document: str) -> AuthorizationRequest:
    try:
        value = parse_bounded_json(
            document,
            description="stored authorization request",
            max_nodes=64,
            max_depth=4,
        )
        if type(value) is not dict or set(value) != {
            "request_id",
            "principal_id",
            "agent_client_id",
            "tenant_id",
            "repository_reference",
            "permission",
            "requested_at",
        }:
            raise ValueError
        return AuthorizationRequest(**value)
    except (TypeError, ValueError) as error:
        raise SQLiteMemoryPublicationV3PersistenceError(
            "stored authorization request is invalid"
        ) from error


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a non-empty bounded identifier")
    return value


def _approval_provenance_matches(
    stored: tuple[
        MemoryRevisionApproval,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ],
    approval: MemoryRevisionApproval,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    verifier_id: str,
) -> bool:
    return (
        stored[0] == approval
        and dumps_authorization_policy(stored[1])
        == dumps_authorization_policy(policy)
        and _request_descriptor(stored[2]) == _request_descriptor(request)
        and dumps_authorization_decision(stored[3])
        == dumps_authorization_decision(decision)
        and stored[4] == verifier_id
    )


def _activation_provenance_matches(
    stored: tuple[
        MemoryRevisionActivation,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ],
    activation: MemoryRevisionActivation,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    verifier_id: str,
) -> bool:
    return (
        stored[0] == activation
        and dumps_authorization_policy(stored[1])
        == dumps_authorization_policy(policy)
        and _request_descriptor(stored[2]) == _request_descriptor(request)
        and dumps_authorization_decision(stored[3])
        == dumps_authorization_decision(decision)
        and stored[4] == verifier_id
    )


class SQLiteMemoryPublicationV3Repository:
    """Durable approval/activation authority over proposal-only v3 storage."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        attestation_verifier: AttestationVerifier,
        attestation_verifier_id: str,
        owns_connection: bool = False,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("connection must be a sqlite3.Connection")
        if not callable(attestation_verifier):
            raise ValueError("attestation_verifier must be callable")
        self._connection = connection
        self._attestation_verifier = attestation_verifier
        self._attestation_verifier_id = _identifier(
            attestation_verifier_id,
            "attestation_verifier_id",
        )
        self._owns_connection = owns_connection
        self._lock = _acquire_connection_lock(connection)
        self._closed = False
        self._savepoint_number = 0

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        attestation_verifier: AttestationVerifier,
        attestation_verifier_id: str,
        initialize: bool = False,
        **kwargs: object,
    ) -> SQLiteMemoryPublicationV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                for resource in (
                    "schemas/sqlite-v3-memory-revision.sql",
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
            raise SQLiteMemoryPublicationV3PersistenceError(
                "failed to connect to SQLite memory publication v3 storage"
            ) from error
        return cls(
            connection,
            attestation_verifier=attestation_verifier,
            attestation_verifier_id=attestation_verifier_id,
            owns_connection=True,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteMemoryPublicationV3Error(
                "SQLite memory publication v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteMemoryPublicationV3Error(
                "SQLite memory publication v3 repository is closed"
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
            primary_error.add_note(f"rollback attempt left {context} active")
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite publication connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = (
                f"tbm_sqlite_memory_publication_v3_"
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
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    error.add_note(
                        "failed to clean up SQLite publication savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        error,
                        context="outer transaction after savepoint failure",
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
                            "failed to clean up SQLite publication savepoint: "
                            f"{cleanup_error}"
                        )
                        self._rollback_connection_or_close(
                            error,
                            context=(
                                "outer transaction after release failure"
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
                context="top-level SQLite memory publication transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="top-level SQLite memory publication transaction",
                )
                raise

    def _require_schema(self, cursor: sqlite3.Cursor) -> None:
        try:
            cursor.execute("PRAGMA foreign_keys")
            if cursor.fetchone() != (1,):
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory publication requires foreign keys"
                )
            cursor.execute("PRAGMA recursive_triggers")
            if cursor.fetchone() != (1,):
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory publication requires recursive triggers"
                )
            cursor.execute(
                "SELECT schema_version FROM "
                "main.trace_backed_memory_v3_memory_revision_schema "
                "WHERE singleton = 1"
            )
            if cursor.fetchall() != [(1,)]:
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory revision dependency mismatch"
                )
            cursor.execute(
                "SELECT schema_version FROM "
                "main.trace_backed_memory_v3_memory_publication_schema "
                "WHERE singleton = 1"
            )
            if cursor.fetchall() != [
                (SQLITE_MEMORY_PUBLICATION_V3_SCHEMA_VERSION,)
            ]:
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory publication schema metadata mismatch"
                )
            try:
                revision_definitions = _read_revision_schema_definitions(
                    cursor
                )
                canonical_revision_definitions = (
                    _revision_schema_definitions()
                )
            except SQLiteMemoryRevisionV3SchemaError as error:
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory publication proposal dependency "
                    "schema does not match"
                ) from error
            if revision_definitions != canonical_revision_definitions:
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory publication proposal dependency "
                    "schema does not match"
                )
            if (
                _read_schema_definitions(cursor)
                != _canonical_schema_definitions()
            ):
                raise SQLiteMemoryPublicationV3SchemaError(
                    "SQLite memory publication schema definitions mismatch"
                )
        except SQLiteMemoryPublicationV3SchemaError:
            raise
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryPublicationV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error

    def _verify_attestation(
        self,
        kind: AttestationKind,
        actor_id: str,
        client_id: str,
        digest: str,
    ) -> None:
        try:
            verified = self._attestation_verifier(
                kind,
                actor_id,
                client_id,
                digest,
            )
        except BaseException as error:
            raise SQLiteMemoryPublicationV3AttestationError(
                f"{kind} attestation verification failed"
            ) from error
        if verified is not True:
            raise SQLiteMemoryPublicationV3AttestationError(
                f"{kind} attestation was not verified"
            )

    @staticmethod
    def _require_proposal_bundle(
        cursor: sqlite3.Cursor,
        revision: MemoryRevision,
        previous_revision: MemoryRevision | None,
        fix_evidence_by_id: Mapping[str, FixEvidence],
        regression_evidence_by_id: Mapping[
            str,
            StructuredRegressionEvidence,
        ],
    ) -> None:
        cursor.execute(
            "SELECT descriptor FROM v3_memory_revision_proposals "
            "WHERE revision_id = ?",
            (revision.revision_id,),
        )
        if cursor.fetchone() != (dumps_memory_revision(revision),):
            raise SQLiteMemoryPublicationV3NotFoundError(
                "exact memory revision proposal is not stored"
            )
        if previous_revision is not None:
            cursor.execute(
                "SELECT descriptor FROM v3_memory_revision_proposals "
                "WHERE revision_id = ?",
                (previous_revision.revision_id,),
            )
            if cursor.fetchone() != (
                dumps_memory_revision(previous_revision),
            ):
                raise SQLiteMemoryPublicationV3NotFoundError(
                    "exact previous memory revision proposal is not stored"
                )
        if revision.fix_evidence_id is not None:
            fix = fix_evidence_by_id.get(revision.fix_evidence_id)
            if type(fix) is not FixEvidence:
                raise SQLiteMemoryPublicationV3NotFoundError(
                    "exact fix evidence is not supplied"
                )
            cursor.execute(
                "SELECT descriptor FROM v3_fix_evidence "
                "WHERE evidence_id = ?",
                (revision.fix_evidence_id,),
            )
            if cursor.fetchone() != (dumps_fix_evidence(fix),):
                raise SQLiteMemoryPublicationV3NotFoundError(
                    "exact fix evidence is not stored"
                )
        cursor.execute(
            "SELECT evidence_id FROM "
            "v3_memory_revision_regression_evidence "
            "WHERE revision_id = ? ORDER BY ordinal",
            (revision.revision_id,),
        )
        stored_ids = tuple(row[0] for row in cursor.fetchall())
        if stored_ids != revision.regression_evidence_ids:
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored regression evidence links do not match proposal"
            )
        for evidence_id in stored_ids:
            evidence = regression_evidence_by_id.get(evidence_id)
            if type(evidence) is not StructuredRegressionEvidence:
                raise SQLiteMemoryPublicationV3NotFoundError(
                    "exact regression evidence is not supplied"
                )
            cursor.execute(
                "SELECT descriptor FROM v3_regression_evidence "
                "WHERE evidence_id = ?",
                (evidence_id,),
            )
            if cursor.fetchone() != (
                dumps_structured_regression_evidence(evidence),
            ):
                raise SQLiteMemoryPublicationV3NotFoundError(
                    "exact regression evidence is not stored"
                )

    @staticmethod
    def _approval_values(
        approval: MemoryRevisionApproval,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        attestation_verified_by: str,
    ) -> tuple[object, ...]:
        return (
            approval.approval_id,
            approval.revision_id,
            approval.tenant_id,
            approval.repository_id,
            approval.repository_id or "",
            approval.memory_id,
            approval.revision_number,
            dumps_memory_revision_approval(approval),
            dumps_authorization_policy(policy),
            _request_descriptor(request),
            dumps_authorization_decision(decision),
            attestation_verified_by,
        )

    @staticmethod
    def _activation_values(
        activation: MemoryRevisionActivation,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        attestation_verified_by: str,
    ) -> tuple[object, ...]:
        return (
            activation.activation_id,
            activation.approval_id,
            activation.revision_id,
            activation.tenant_id,
            activation.repository_id,
            activation.repository_id or "",
            activation.memory_id,
            activation.revision_number,
            activation.previous_activation_id,
            dumps_memory_revision_activation(activation),
            dumps_authorization_policy(policy),
            _request_descriptor(request),
            dumps_authorization_decision(decision),
            attestation_verified_by,
        )

    @staticmethod
    def _put_exact(
        cursor: sqlite3.Cursor,
        *,
        table: str,
        id_column: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        conflict_message: str,
    ) -> bool:
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE {id_column} = ?",
            (values[0],),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if existing != values:
                raise SQLiteMemoryPublicationV3ConflictError(
                    conflict_message
                )
            return False
        placeholders = ", ".join("?" for _ in values)
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            values,
        )
        return True

    @staticmethod
    def _load_approval_row(
        cursor: sqlite3.Cursor,
        *,
        approval_id: str | None = None,
        revision_id: str | None = None,
    ) -> tuple[
        MemoryRevisionApproval,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ]:
        if (approval_id is None) == (revision_id is None):
            raise ValueError("select approval by exactly one identity")
        column = "approval_id" if approval_id is not None else "revision_id"
        identity = approval_id if approval_id is not None else revision_id
        cursor.execute(
            "SELECT descriptor, authorization_policy_descriptor, "
            "authorization_request_descriptor, "
            "authorization_decision_descriptor, attestation_verified_by "
            f"FROM v3_memory_revision_approvals WHERE {column} = ?",
            (identity,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteMemoryPublicationV3NotFoundError(
                "memory revision approval was not found"
            )
        if len(row) != 5 or any(type(value) is not str for value in row):
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored memory revision approval has invalid shape"
            )
        try:
            approval = loads_memory_revision_approval(row[0])
            policy = loads_authorization_policy(row[1])
            request = _loads_request(row[2])
            decision = loads_authorization_decision(row[3])
            verifier = _identifier(row[4], "attestation_verified_by")
        except SQLiteMemoryPublicationV3Error:
            raise
        except ValueError as error:
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored memory revision approval is invalid"
            ) from error
        return approval, policy, request, decision, verifier

    @staticmethod
    def _load_activation_row(
        cursor: sqlite3.Cursor,
        *,
        activation_id: str | None = None,
        revision_id: str | None = None,
    ) -> tuple[
        MemoryRevisionActivation,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ]:
        if (activation_id is None) == (revision_id is None):
            raise ValueError("select activation by exactly one identity")
        column = (
            "activation_id" if activation_id is not None else "revision_id"
        )
        identity = activation_id if activation_id is not None else revision_id
        cursor.execute(
            "SELECT descriptor, authorization_policy_descriptor, "
            "authorization_request_descriptor, "
            "authorization_decision_descriptor, attestation_verified_by "
            f"FROM v3_memory_revision_activations WHERE {column} = ?",
            (identity,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SQLiteMemoryPublicationV3NotFoundError(
                "memory revision activation was not found"
            )
        if len(row) != 5 or any(type(value) is not str for value in row):
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored memory revision activation has invalid shape"
            )
        try:
            activation = loads_memory_revision_activation(row[0])
            policy = loads_authorization_policy(row[1])
            request = _loads_request(row[2])
            decision = loads_authorization_decision(row[3])
            verifier = _identifier(row[4], "attestation_verified_by")
        except SQLiteMemoryPublicationV3Error:
            raise
        except ValueError as error:
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored memory revision activation is invalid"
            ) from error
        return activation, policy, request, decision, verifier

    @staticmethod
    def _select_head(
        cursor: sqlite3.Cursor,
        *,
        tenant_id: str,
        repository_id: str | None,
        memory_id: str,
    ) -> SQLiteMemoryPublicationV3Head | None:
        cursor.execute(
            "SELECT tenant_id, repository_id, memory_id, "
            "current_revision_number, current_revision_id, "
            "current_activation_id "
            "FROM v3_memory_revision_activation_heads "
            "WHERE tenant_id = ? AND repository_id_key = ? "
            "AND memory_id = ?",
            (tenant_id, repository_id or "", memory_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if (
            len(row) != 6
            or type(row[0]) is not str
            or (row[1] is not None and type(row[1]) is not str)
            or type(row[2]) is not str
            or type(row[3]) is not int
        ):
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored activation head has invalid shape"
            )
        if row[3] == 0:
            if row[4] is not None or row[5] is not None:
                raise SQLiteMemoryPublicationV3PersistenceError(
                    "empty activation head is inconsistent"
                )
            return None
        if type(row[4]) is not str or type(row[5]) is not str:
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored activation head is incomplete"
            )
        head = SQLiteMemoryPublicationV3Head(
            tenant_id=row[0],
            repository_id=row[1],
            memory_id=row[2],
            current_revision_number=row[3],
            current_revision_id=row[4],
            current_activation_id=row[5],
        )
        activation, *_ = (
            SQLiteMemoryPublicationV3Repository._load_activation_row(
                cursor,
                activation_id=head.current_activation_id,
            )
        )
        if (
            activation.revision_id != head.current_revision_id
            or activation.revision_number != head.current_revision_number
            or activation.tenant_id != head.tenant_id
            or activation.repository_id != head.repository_id
            or activation.memory_id != head.memory_id
        ):
            raise SQLiteMemoryPublicationV3PersistenceError(
                "stored activation head does not match activation"
            )
        return head

    @_synchronized
    def append_approval(
        self,
        *,
        revision: MemoryRevision,
        previous_revision: MemoryRevision | None,
        content: bytes,
        fix_evidence_by_id: Mapping[str, FixEvidence],
        regression_evidence_by_id: Mapping[
            str,
            StructuredRegressionEvidence,
        ],
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        approved_by: str,
        approved_via_client_id: str,
        approved_at: str,
        approval_attestation_sha256: str,
    ) -> SQLiteMemoryPublicationV3ApprovalResult:
        self._require_open()
        verifier_id = self._attestation_verifier_id
        approval = approve_memory_revision(
            revision=revision,
            previous_revision=previous_revision,
            content=content,
            fix_evidence_by_id=fix_evidence_by_id,
            regression_evidence_by_id=regression_evidence_by_id,
            policy=policy,
            request=request,
            decision=decision,
            approved_by=approved_by,
            approved_via_client_id=approved_via_client_id,
            approved_at=approved_at,
            approval_attestation_sha256=approval_attestation_sha256,
        )
        self._verify_attestation(
            "approval",
            approval.approved_by,
            approval.approved_via_client_id,
            approval.approval_attestation_sha256,
        )
        columns = (
            "approval_id",
            "revision_id",
            "tenant_id",
            "repository_id",
            "repository_id_key",
            "memory_id",
            "revision_number",
            "descriptor",
            "authorization_policy_descriptor",
            "authorization_request_descriptor",
            "authorization_decision_descriptor",
            "attestation_verified_by",
        )
        values = self._approval_values(
            approval,
            policy,
            request,
            decision,
            verifier_id,
        )
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    self._require_proposal_bundle(
                        cursor,
                        revision,
                        previous_revision,
                        fix_evidence_by_id,
                        regression_evidence_by_id,
                    )
                    inserted = self._put_exact(
                        cursor,
                        table="v3_memory_revision_approvals",
                        id_column="approval_id",
                        columns=columns,
                        values=values,
                        conflict_message="approval identity conflict",
                    )
                    retained = self._load_approval_row(
                        cursor,
                        approval_id=approval.approval_id,
                    )
                    if not _approval_provenance_matches(
                        retained,
                        approval,
                        policy,
                        request,
                        decision,
                        verifier_id,
                    ):
                        raise SQLiteMemoryPublicationV3PersistenceError(
                            "approval read-back mismatch"
                        )
                    self._require_schema(cursor)
        except (
            SQLiteMemoryPublicationV3Error,
            ValueError,
        ):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteMemoryPublicationV3ConflictError(
                "approval conflicts with durable publication state"
            ) from error
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryPublicationV3PersistenceError(
                "failed to append memory revision approval"
            ) from error
        return SQLiteMemoryPublicationV3ApprovalResult(
            approval=approval,
            inserted=inserted,
            attestation_verified_by=verifier_id,
        )

    @_synchronized
    def append_activation(
        self,
        *,
        revision: MemoryRevision,
        previous_revision: MemoryRevision | None,
        content: bytes,
        fix_evidence_by_id: Mapping[str, FixEvidence],
        regression_evidence_by_id: Mapping[
            str,
            StructuredRegressionEvidence,
        ],
        approval: MemoryRevisionApproval,
        approval_policy: AuthorizationPolicyBundle,
        approval_request: AuthorizationRequest,
        approval_decision: AuthorizationDecision,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        activated_by: str,
        activated_via_client_id: str,
        activated_at: str,
        activation_attestation_sha256: str,
    ) -> SQLiteMemoryPublicationV3ActivationResult:
        self._require_open()
        verifier_id = self._attestation_verifier_id
        self._verify_attestation(
            "activation",
            activated_by,
            activated_via_client_id,
            activation_attestation_sha256,
        )
        try:
            with self._transaction(write=True):
                with closing(self._connection.cursor()) as cursor:
                    self._require_schema(cursor)
                    self._require_proposal_bundle(
                        cursor,
                        revision,
                        previous_revision,
                        fix_evidence_by_id,
                        regression_evidence_by_id,
                    )
                    stored_approval = self._load_approval_row(
                        cursor,
                        approval_id=approval.approval_id,
                    )
                    if not _approval_provenance_matches(
                        stored_approval,
                        approval,
                        approval_policy,
                        approval_request,
                        approval_decision,
                        stored_approval[4],
                    ):
                        raise SQLiteMemoryPublicationV3ConflictError(
                            "activation approval provenance mismatch"
                        )
                    cursor.execute(
                        "SELECT activation_id FROM "
                        "v3_memory_revision_activations "
                        "WHERE revision_id = ?",
                        (revision.revision_id,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        if (
                            len(existing_row) != 1
                            or type(existing_row[0]) is not str
                        ):
                            raise SQLiteMemoryPublicationV3PersistenceError(
                                "stored activation identity has invalid shape"
                            )
                        existing, *_ = self._load_activation_row(
                            cursor,
                            activation_id=existing_row[0],
                        )
                        if existing.previous_activation_id is None:
                            previous_activation = None
                        else:
                            previous_activation, *_ = (
                                self._load_activation_row(
                                    cursor,
                                    activation_id=(
                                        existing.previous_activation_id
                                    ),
                                )
                            )
                    else:
                        head = self._select_head(
                            cursor,
                            tenant_id=approval.tenant_id,
                            repository_id=approval.repository_id,
                            memory_id=approval.memory_id,
                        )
                        if head is None:
                            if revision.revision_number != 1:
                                raise SQLiteMemoryPublicationV3ConflictError(
                                    "activation has no durable predecessor"
                                )
                            previous_activation = None
                            cursor.execute(
                                "INSERT OR IGNORE INTO "
                                "v3_memory_revision_activation_heads "
                                "(tenant_id, repository_id, "
                                "repository_id_key, memory_id, "
                                "current_revision_number, "
                                "current_revision_id, "
                                "current_activation_id) "
                                "VALUES (?, ?, ?, ?, 0, NULL, NULL)",
                                (
                                    approval.tenant_id,
                                    approval.repository_id,
                                    approval.repository_id or "",
                                    approval.memory_id,
                                ),
                            )
                        else:
                            if (
                                head.current_revision_number
                                != revision.revision_number - 1
                                or head.current_revision_id
                                != revision.previous_revision_id
                            ):
                                raise (
                                    SQLiteMemoryPublicationV3ConflictError(
                                        "activation durable head is stale"
                                    )
                                )
                            previous_activation, *_ = (
                                self._load_activation_row(
                                    cursor,
                                    activation_id=(
                                        head.current_activation_id
                                    ),
                                )
                            )
                    activation = activate_memory_revision(
                        revision=revision,
                        approval=approval,
                        previous_revision=previous_revision,
                        content=content,
                        fix_evidence_by_id=fix_evidence_by_id,
                        regression_evidence_by_id=(
                            regression_evidence_by_id
                        ),
                        approval_policy=approval_policy,
                        approval_request=approval_request,
                        approval_decision=approval_decision,
                        previous_activation=previous_activation,
                        policy=policy,
                        request=request,
                        decision=decision,
                        activated_by=activated_by,
                        activated_via_client_id=activated_via_client_id,
                        activated_at=activated_at,
                        activation_attestation_sha256=(
                            activation_attestation_sha256
                        ),
                    )
                    columns = (
                        "activation_id",
                        "approval_id",
                        "revision_id",
                        "tenant_id",
                        "repository_id",
                        "repository_id_key",
                        "memory_id",
                        "revision_number",
                        "previous_activation_id",
                        "descriptor",
                        "authorization_policy_descriptor",
                        "authorization_request_descriptor",
                        "authorization_decision_descriptor",
                        "attestation_verified_by",
                    )
                    values = self._activation_values(
                        activation,
                        policy,
                        request,
                        decision,
                        verifier_id,
                    )
                    inserted = self._put_exact(
                        cursor,
                        table="v3_memory_revision_activations",
                        id_column="activation_id",
                        columns=columns,
                        values=values,
                        conflict_message="activation identity conflict",
                    )
                    if inserted:
                        expected_sequence = activation.revision_number - 1
                        cursor.execute(
                            "UPDATE "
                            "v3_memory_revision_activation_heads "
                            "SET current_revision_number = ?, "
                            "current_revision_id = ?, "
                            "current_activation_id = ? "
                            "WHERE tenant_id = ? "
                            "AND repository_id_key = ? "
                            "AND memory_id = ? "
                            "AND current_revision_number = ? "
                            "AND current_activation_id "
                            "IS ?",
                            (
                                activation.revision_number,
                                activation.revision_id,
                                activation.activation_id,
                                activation.tenant_id,
                                activation.repository_id or "",
                                activation.memory_id,
                                expected_sequence,
                                activation.previous_activation_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise SQLiteMemoryPublicationV3ConflictError(
                                "activation lost durable head compare-and-swap"
                            )
                    retained = self._load_activation_row(
                        cursor,
                        activation_id=activation.activation_id,
                    )
                    if not _activation_provenance_matches(
                        retained,
                        activation,
                        policy,
                        request,
                        decision,
                        verifier_id,
                    ):
                        raise SQLiteMemoryPublicationV3PersistenceError(
                            "activation read-back mismatch"
                        )
                    if inserted:
                        head = self._select_head(
                            cursor,
                            tenant_id=activation.tenant_id,
                            repository_id=activation.repository_id,
                            memory_id=activation.memory_id,
                        )
                        if (
                            head is None
                            or head.current_activation_id
                            != activation.activation_id
                            or head.current_revision_id
                            != activation.revision_id
                            or head.current_revision_number
                            != activation.revision_number
                        ):
                            raise SQLiteMemoryPublicationV3ConflictError(
                                "activation is not the durable current head"
                            )
                    self._require_schema(cursor)
        except (
            SQLiteMemoryPublicationV3Error,
            ValueError,
        ):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteMemoryPublicationV3ConflictError(
                "activation conflicts with durable publication state"
            ) from error
        except sqlite3.DatabaseError as error:
            raise SQLiteMemoryPublicationV3PersistenceError(
                "failed to append memory revision activation"
            ) from error
        return SQLiteMemoryPublicationV3ActivationResult(
            activation=activation,
            inserted=inserted,
            attestation_verified_by=verifier_id,
        )

    @_synchronized
    def load_approval(
        self,
        approval_id: str,
    ) -> SQLiteMemoryPublicationV3ApprovalResult:
        self._require_open()
        _identifier(approval_id, "approval_id")
        with self._transaction(write=False):
            with closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                approval, _policy, _request, _decision, verifier = (
                    self._load_approval_row(
                        cursor,
                        approval_id=approval_id,
                    )
                )
        return SQLiteMemoryPublicationV3ApprovalResult(
            approval=approval,
            inserted=False,
            attestation_verified_by=verifier,
        )

    @_synchronized
    def load_approval_bundle(
        self,
        approval_id: str,
    ) -> StoredMemoryRevisionApprovalPublication:
        self._require_open()
        _identifier(approval_id, "approval_id")
        with self._transaction(write=False):
            with closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                approval, policy, request, decision, verifier = (
                    self._load_approval_row(cursor, approval_id=approval_id)
                )
        return StoredMemoryRevisionApprovalPublication(
            approval=approval,
            policy=policy,
            request=request,
            decision=decision,
            attestation_verified_by=verifier,
        )

    @_synchronized
    def load_activation(
        self,
        activation_id: str,
    ) -> SQLiteMemoryPublicationV3ActivationResult:
        self._require_open()
        _identifier(activation_id, "activation_id")
        with self._transaction(write=False):
            with closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                activation, _policy, _request, _decision, verifier = (
                    self._load_activation_row(
                        cursor,
                        activation_id=activation_id,
                    )
                )
        return SQLiteMemoryPublicationV3ActivationResult(
            activation=activation,
            inserted=False,
            attestation_verified_by=verifier,
        )

    @_synchronized
    def load_activation_bundle(
        self,
        activation_id: str,
    ) -> StoredMemoryRevisionActivationPublication:
        self._require_open()
        _identifier(activation_id, "activation_id")
        with self._transaction(write=False):
            with closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                activation, policy, request, decision, verifier = (
                    self._load_activation_row(
                        cursor, activation_id=activation_id
                    )
                )
        return StoredMemoryRevisionActivationPublication(
            activation=activation,
            policy=policy,
            request=request,
            decision=decision,
            attestation_verified_by=verifier,
        )

    @_synchronized
    def load_head(
        self,
        *,
        tenant_id: str,
        repository_id: str | None,
        memory_id: str,
    ) -> SQLiteMemoryPublicationV3Head:
        self._require_open()
        _identifier(tenant_id, "tenant_id")
        if repository_id is not None:
            _identifier(repository_id, "repository_id")
        _identifier(memory_id, "memory_id")
        with self._transaction(write=False):
            with closing(self._connection.cursor()) as cursor:
                self._require_schema(cursor)
                head = self._select_head(
                    cursor,
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    memory_id=memory_id,
                )
        if head is None:
            raise SQLiteMemoryPublicationV3NotFoundError(
                "memory revision activation head was not found"
            )
        return head

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _release_connection_lock(self._connection)
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteMemoryPublicationV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
