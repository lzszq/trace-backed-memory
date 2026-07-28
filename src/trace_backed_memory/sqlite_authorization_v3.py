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

from ._timestamps import canonical_rfc3339
from .authorization_v3 import (
    AUTHORIZATION_JSON_MAX_BYTES,
    AuthorizationContractError,
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    authorize,
    dumps_authorization_decision,
    dumps_authorization_policy,
    loads_authorization_decision,
    loads_authorization_policy,
    verify_authorization_decision,
)
from .resources import PackagedResourceError, read_packaged_resource


SQLITE_AUTHORIZATION_V3_SCHEMA_VERSION = 1
SQLITE_AUTHORIZATION_V3_MAX_PAGE_SIZE = 1000
_MISSING_SCHEMA_MESSAGE = "SQLite authorization v3 schema is missing or incomplete"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_DECISION_ID_RE = re.compile(r"authz_sha256_[0-9a-f]{64}")
_SCHEMA_OBJECT_NAMES = (
    "trace_backed_memory_v3_authorization_schema",
    "v3_authorization_decisions",
    "v3_authorization_decisions_immutable_delete",
    "v3_authorization_decisions_immutable_update",
    "v3_authorization_decisions_identity_guard",
    "v3_authorization_decisions_policy",
    "v3_authorization_decisions_principal",
    "v3_authorization_policies",
    "v3_authorization_policies_immutable_delete",
    "v3_authorization_policies_immutable_update",
    "v3_authorization_policies_identity_guard",
    "v3_authorization_schema_immutable_delete",
    "v3_authorization_schema_immutable_update",
    "v3_authorization_schema_identity_guard",
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class SQLiteAuthorizationV3Error(RuntimeError):
    pass


class SQLiteAuthorizationV3SchemaError(SQLiteAuthorizationV3Error):
    pass


class SQLiteAuthorizationV3ConflictError(SQLiteAuthorizationV3Error):
    pass


class SQLiteAuthorizationV3PersistenceError(SQLiteAuthorizationV3Error):
    pass


@dataclass(frozen=True)
class SQLiteAuthorizationV3AppendResult:
    policy_sha256: str
    policy_inserted: bool
    authorization_event_id: str
    decision_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _normalized_schema_sql(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SQLiteAuthorizationV3SchemaError(
            "SQLite authorization v3 schema contains an invalid definition"
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
        raise SQLiteAuthorizationV3SchemaError(_MISSING_SCHEMA_MESSAGE)
    definitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if (
            len(row) != 4
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
        ):
            raise SQLiteAuthorizationV3SchemaError(
                "SQLite authorization v3 schema definition has an invalid shape"
            )
        definitions.append((row[0], row[1], row[2], _normalized_schema_sql(row[3])))
    return tuple(definitions)


@lru_cache(maxsize=1)
def _canonical_schema_definitions() -> tuple[tuple[str, str, str, str], ...]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                read_packaged_resource("schemas/sqlite-v3-authorization.sql").decode(
                    "utf-8"
                )
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
        raise SQLiteAuthorizationV3SchemaError(
            "could not validate the canonical SQLite authorization v3 schema"
        ) from error


class SQLiteAuthorizationV3Repository:
    """Immutable local authority for authorization policies and decisions."""

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
    ) -> SQLiteAuthorizationV3Repository:
        if type(initialize) is not bool:
            raise ValueError("initialize must be a boolean")
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                connection.executescript(
                    read_packaged_resource(
                        "schemas/sqlite-v3-authorization.sql"
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
            raise SQLiteAuthorizationV3PersistenceError(
                "failed to connect to SQLite authorization v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed:
            raise SQLiteAuthorizationV3Error(
                "SQLite authorization v3 repository is closed"
            )
        try:
            self._connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as error:
            raise SQLiteAuthorizationV3Error(
                "SQLite authorization v3 repository is closed"
            ) from error

    def _lock_schema(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_authorization_schema "
            "WHERE singleton = 1"
        )
        rows = cursor.fetchall()
        if rows != [(1, "tbm.authorization.v3")]:
            raise SQLiteAuthorizationV3SchemaError(
                "SQLite authorization v3 schema metadata mismatch"
            )
        if _read_schema_definitions(cursor) != _canonical_schema_definitions():
            raise SQLiteAuthorizationV3SchemaError(
                "SQLite authorization v3 schema definitions do not match"
            )
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone() != (1,):
            raise SQLiteAuthorizationV3SchemaError(
                "SQLite authorization v3 requires foreign keys"
            )
        cursor.execute("PRAGMA recursive_triggers")
        if cursor.fetchone() != (1,):
            raise SQLiteAuthorizationV3SchemaError(
                "SQLite authorization v3 requires recursive triggers"
            )

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
                primary_error.add_note(f"{prefix} {context}: {rollback_error}")
                continue
            if not self._connection.in_transaction:
                return
            primary_error.add_note(f"rollback attempt left {context} active")
        self._closed = True
        try:
            self._connection.close()
        except BaseException as close_error:
            primary_error.add_note(
                "failed to close unusable SQLite authorization v3 connection: "
                f"{close_error}"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        nested = self._connection.in_transaction
        if nested:
            self._savepoint_number += 1
            savepoint = f"tbm_authorization_v3_{self._savepoint_number}"
            self._connection.execute(f"SAVEPOINT {savepoint}")

            def rollback_savepoint(primary_error: BaseException) -> None:
                try:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "failed to clean up SQLite authorization v3 savepoint "
                        f"{savepoint}: {cleanup_error}"
                    )
                    self._rollback_connection_or_close(
                        primary_error,
                        context=(
                            "the outer SQLite transaction after authorization "
                            "v3 savepoint cleanup failed"
                        ),
                    )

            try:
                with closing(self._connection.cursor()) as cursor:
                    self._lock_schema(cursor)
                    yield cursor
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

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            with closing(self._connection.cursor()) as cursor:
                self._lock_schema(cursor)
                yield cursor
        except BaseException as error:
            self._rollback_connection_or_close(
                error,
                context="the top-level SQLite authorization v3 transaction",
            )
            raise
        else:
            try:
                self._connection.commit()
            except BaseException as error:
                self._rollback_connection_or_close(
                    error,
                    context="the top-level SQLite authorization v3 transaction",
                )
                raise

    @staticmethod
    def _policy_values(
        policy: AuthorizationPolicyBundle,
    ) -> tuple[str, str, str]:
        descriptor = dumps_authorization_policy(policy)
        if len(descriptor.encode("utf-8")) > AUTHORIZATION_JSON_MAX_BYTES:
            raise ValueError("authorization policy descriptor exceeds storage limit")
        return policy.policy_sha256, policy.policy_version, descriptor

    @classmethod
    def _stored_policy(cls, row: tuple[object, ...]) -> AuthorizationPolicyBundle:
        if len(row) != 3 or type(row[2]) is not str:
            cls._persistence("SQLite authorization policy row has an invalid shape")
        try:
            policy = loads_authorization_policy(cast(str, row[2]))
        except AuthorizationContractError as error:
            raise SQLiteAuthorizationV3PersistenceError(
                "SQLite authorization policy descriptor failed validation"
            ) from error
        if row != cls._policy_values(policy):
            cls._persistence(
                "SQLite authorization policy columns do not match descriptor"
            )
        return policy

    @staticmethod
    def _decision_values(
        decision: AuthorizationDecision,
    ) -> tuple[object, ...]:
        descriptor = dumps_authorization_decision(decision)
        if len(descriptor.encode("utf-8")) > AUTHORIZATION_JSON_MAX_BYTES:
            raise ValueError("authorization decision descriptor exceeds storage limit")
        return (
            decision.authorization_event_id,
            decision.request_id,
            decision.request_sha256,
            decision.policy_sha256,
            decision.principal_id,
            decision.agent_client_id,
            decision.tenant_id,
            decision.repository_id,
            decision.permission,
            int(decision.allowed),
            decision.reason,
            canonical_rfc3339(decision.decided_at),
            descriptor,
        )

    @classmethod
    def _stored_decision(cls, row: tuple[object, ...]) -> AuthorizationDecision:
        if len(row) != 13 or type(row[-1]) is not str:
            cls._persistence("SQLite authorization decision row has an invalid shape")
        try:
            decision = loads_authorization_decision(cast(str, row[-1]))
        except AuthorizationContractError as error:
            raise SQLiteAuthorizationV3PersistenceError(
                "SQLite authorization decision descriptor failed validation"
            ) from error
        if row != cls._decision_values(decision):
            cls._persistence(
                "SQLite authorization decision columns do not match descriptor"
            )
        return decision

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise SQLiteAuthorizationV3PersistenceError(message)

    def _put_policy(
        self,
        cursor: sqlite3.Cursor,
        policy: AuthorizationPolicyBundle,
    ) -> bool:
        values = self._policy_values(policy)
        cursor.execute(
            "SELECT policy_sha256, policy_version, descriptor "
            "FROM v3_authorization_policies WHERE policy_sha256 = ?",
            (policy.policy_sha256,),
        )
        rows = cursor.fetchall()
        if rows:
            stored = self._stored_policy(rows[0])
            if self._policy_values(stored) != values:
                raise SQLiteAuthorizationV3ConflictError(
                    "authorization policy identity has conflicting content"
                )
            return False
        cursor.execute(
            "INSERT INTO v3_authorization_policies "
            "(policy_sha256, policy_version, descriptor) VALUES (?, ?, ?)",
            values,
        )
        return True

    def _put_decision(
        self,
        cursor: sqlite3.Cursor,
        decision: AuthorizationDecision,
    ) -> bool:
        values = self._decision_values(decision)
        cursor.execute(
            "SELECT authorization_event_id, request_id, request_sha256, "
            "policy_sha256, principal_id, agent_client_id, tenant_id, "
            "repository_id, permission, allowed, reason, decided_at, descriptor "
            "FROM v3_authorization_decisions WHERE authorization_event_id = ?",
            (decision.authorization_event_id,),
        )
        rows = cursor.fetchall()
        if rows:
            stored = self._stored_decision(rows[0])
            if self._decision_values(stored) != values:
                raise SQLiteAuthorizationV3ConflictError(
                    "authorization decision identity has conflicting content"
                )
            return False
        cursor.execute(
            "INSERT INTO v3_authorization_decisions ("
            "authorization_event_id, request_id, request_sha256, policy_sha256, "
            "principal_id, agent_client_id, tenant_id, repository_id, "
            "permission, allowed, reason, decided_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True

    @_synchronized
    def store_policy(self, policy: AuthorizationPolicyBundle) -> bool:
        self._require_open()
        if type(policy) is not AuthorizationPolicyBundle:
            raise ValueError("policy must be exactly AuthorizationPolicyBundle")
        try:
            with self._transaction() as cursor:
                return self._put_policy(cursor, policy)
        except (SQLiteAuthorizationV3Error, ValueError):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteAuthorizationV3ConflictError(
                "authorization policy conflicts with stored identity"
            ) from error
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "failed to store authorization policy")

    @_synchronized
    def authorize_and_record(
        self,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        *,
        decided_at: str,
    ) -> tuple[AuthorizationDecision, SQLiteAuthorizationV3AppendResult]:
        self._require_open()
        if type(policy) is not AuthorizationPolicyBundle:
            raise ValueError("policy must be exactly AuthorizationPolicyBundle")
        if type(request) is not AuthorizationRequest:
            raise ValueError("request must be exactly AuthorizationRequest")
        decision = authorize(policy, request, decided_at=decided_at)
        result = self.append_decision(policy, request, decision)
        return decision, result

    @_synchronized
    def append_decision(
        self,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
    ) -> SQLiteAuthorizationV3AppendResult:
        self._require_open()
        if (
            type(policy) is not AuthorizationPolicyBundle
            or type(request) is not AuthorizationRequest
            or type(decision) is not AuthorizationDecision
        ):
            raise ValueError(
                "policy, request, and decision must be exact authorization records"
            )
        try:
            verify_authorization_decision(policy, request, decision)
        except AuthorizationContractError as error:
            raise SQLiteAuthorizationV3ConflictError(
                "authorization decision does not verify against its request"
            ) from error
        if decision.policy_sha256 != policy.policy_sha256:
            raise SQLiteAuthorizationV3ConflictError(
                "authorization decision references a different policy"
            )
        try:
            with self._transaction() as cursor:
                policy_inserted = self._put_policy(cursor, policy)
                decision_inserted = self._put_decision(cursor, decision)
            return SQLiteAuthorizationV3AppendResult(
                policy_sha256=policy.policy_sha256,
                policy_inserted=policy_inserted,
                authorization_event_id=decision.authorization_event_id,
                decision_inserted=decision_inserted,
            )
        except (SQLiteAuthorizationV3Error, ValueError):
            raise
        except sqlite3.IntegrityError as error:
            raise SQLiteAuthorizationV3ConflictError(
                "authorization decision conflicts with stored request identity"
            ) from error
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "failed to append authorization decision")

    @_synchronized
    def load_policy(self, policy_sha256: str) -> AuthorizationPolicyBundle:
        self._require_open()
        if type(policy_sha256) is not str or not _DIGEST_RE.fullmatch(policy_sha256):
            raise ValueError("policy_sha256 must be a canonical digest")
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    "SELECT policy_sha256, policy_version, descriptor "
                    "FROM v3_authorization_policies WHERE policy_sha256 = ?",
                    (policy_sha256,),
                )
                rows = cursor.fetchall()
                if not rows:
                    raise KeyError(policy_sha256)
                if len(rows) != 1:
                    self._persistence(
                        "SQLite authorization policy identity is not unique"
                    )
                return self._stored_policy(rows[0])
        except (KeyError, SQLiteAuthorizationV3Error):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "failed to load authorization policy")

    @_synchronized
    def load_decision(
        self,
        authorization_event_id: str,
    ) -> AuthorizationDecision:
        self._require_open()
        if type(authorization_event_id) is not str or not _DECISION_ID_RE.fullmatch(
            authorization_event_id
        ):
            raise ValueError("authorization_event_id must be canonical")
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    "SELECT authorization_event_id, request_id, request_sha256, "
                    "policy_sha256, principal_id, agent_client_id, tenant_id, "
                    "repository_id, permission, allowed, reason, decided_at, "
                    "descriptor FROM v3_authorization_decisions "
                    "WHERE authorization_event_id = ?",
                    (authorization_event_id,),
                )
                rows = cursor.fetchall()
                if not rows:
                    raise KeyError(authorization_event_id)
                if len(rows) != 1:
                    self._persistence(
                        "SQLite authorization decision identity is not unique"
                    )
                return self._stored_decision(rows[0])
        except (KeyError, SQLiteAuthorizationV3Error):
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "failed to load authorization decision")

    @_synchronized
    def list_decisions(
        self,
        policy_sha256: str,
        *,
        limit: int = 100,
    ) -> tuple[AuthorizationDecision, ...]:
        self._require_open()
        if type(policy_sha256) is not str or not _DIGEST_RE.fullmatch(policy_sha256):
            raise ValueError("policy_sha256 must be a canonical digest")
        if (
            type(limit) is not int
            or not 1 <= limit <= SQLITE_AUTHORIZATION_V3_MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {SQLITE_AUTHORIZATION_V3_MAX_PAGE_SIZE}"
            )
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    "SELECT authorization_event_id, request_id, request_sha256, "
                    "policy_sha256, principal_id, agent_client_id, tenant_id, "
                    "repository_id, permission, allowed, reason, decided_at, "
                    "descriptor FROM v3_authorization_decisions "
                    "WHERE policy_sha256 = ? "
                    "ORDER BY decided_at, authorization_event_id LIMIT ?",
                    (policy_sha256, limit),
                )
                return tuple(self._stored_decision(row) for row in cursor.fetchall())
        except SQLiteAuthorizationV3Error:
            raise
        except sqlite3.DatabaseError as error:
            self._raise_database_error(error, "failed to list authorization decisions")

    @staticmethod
    def _raise_database_error(error: sqlite3.DatabaseError, message: str) -> NoReturn:
        lowered = str(error).lower()
        if any(
            marker in lowered
            for marker in (
                "no such table",
                "no such column",
                "malformed database schema",
                "foreign key mismatch",
            )
        ):
            raise SQLiteAuthorizationV3SchemaError(_MISSING_SCHEMA_MESSAGE) from error
        raise SQLiteAuthorizationV3PersistenceError(message) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> SQLiteAuthorizationV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
