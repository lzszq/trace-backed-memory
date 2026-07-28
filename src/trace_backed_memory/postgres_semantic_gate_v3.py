from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

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
from .postgres import _load_psycopg
from .postgres_authorization_v3 import _CATALOG_SHA256_QUERY
from .retrieval_v3 import RetrievalSnapshot, loads_retrieval_snapshot


POSTGRES_SEMANTIC_GATE_V3_SCHEMA_VERSION = 1
POSTGRES_SEMANTIC_GATE_V3_CONTRACT_VERSION = (
    "tbm.semantic-gate-attempt.v3"
)
_SCHEMA = "trace_backed_memory_v3_semantic_gate"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL semantic Gate v3 schema is missing or incomplete"
)
_EXPECTED_CATALOG_SHA256 = (
    "fdb61aaf2a5c295b3d578eec2981dd09a9609c9aff1208fa59024daf641d66b4"
)
_POSTGRES_SEMANTIC_GATE_CATALOG_SHA256_QUERY = (
    _CATALOG_SHA256_QUERY.replace(
        "trace_backed_memory_v3_authorization.",
        "trace_backed_memory_v3_semantic_gate.",
    )
    .replace(" || '|' ||\n           attribute.attcompression::text", "")
    .replace(
        "(namespace.nspowner <> 0)::text || '|' ||\n"
        "           COALESCE(",
        "(namespace.nspowner <> 0)::text || '|' ||\n"
        "           (namespace.nspowner = (\n"
        "               SELECT active_class.relowner\n"
        "               FROM pg_catalog.pg_class AS active_class\n"
        "               JOIN pg_catalog.pg_namespace AS active_namespace\n"
        "                 ON active_namespace.oid = active_class.relnamespace\n"
        "               WHERE active_namespace.nspname = 'public'\n"
        "                 AND active_class.relname = "
        "'trace_backed_memory_schema'\n"
        "                 AND active_class.relkind IN ('r', 'p')\n"
        "           ))::text || '|' ||\n"
        "           COALESCE(",
    )
    .replace(
        "procedure.proargtypes::text || '|' ||\n"
        "           pg_catalog.has_function_privilege(",
        "procedure.proargtypes::text || '|' ||\n"
        "           (procedure.proowner = namespace.nspowner)::text || '|' ||\n"
        "           pg_catalog.has_function_privilege(",
    )
)
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_ATTEMPT_ID_RE = re.compile(r"semantic_attempt_sha256_[0-9a-f]{64}")
_EVALUATION_ID_RE = re.compile(r"system_gate_sha256_[0-9a-f]{64}")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresSemanticGateV3Error(RuntimeError):
    pass


class PostgresSemanticGateV3SchemaError(PostgresSemanticGateV3Error):
    pass


class PostgresSemanticGateV3ConflictError(PostgresSemanticGateV3Error):
    pass


class PostgresSemanticGateV3NotFoundError(PostgresSemanticGateV3Error):
    pass


class PostgresSemanticGateV3PersistenceError(PostgresSemanticGateV3Error):
    pass


@dataclass(frozen=True)
class PostgresSemanticGateV3StoreResult:
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


class PostgresSemanticGateV3Repository:
    """Immutable PostgreSQL SemanticGateAttempt chain authority."""

    def __init__(self, connection: object, *, owns_connection: bool = False):
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresSemanticGateV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresSemanticGateV3PersistenceError(
                "failed to connect to PostgreSQL semantic Gate v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresSemanticGateV3Error(
                "PostgreSQL semantic Gate v3 repository is closed"
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    def _lock_schema(self, cursor: object, *, for_write: bool) -> str:
        cursor.execute(
            "SELECT pg_catalog.current_setting('search_path') "
            "AS search_path"
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or type(rows[0].get("search_path")) is not str
        ):
            raise PostgresSemanticGateV3SchemaError(
                "PostgreSQL search_path has invalid shape"
            )
        original_search_path = cast(str, rows[0]["search_path"])
        cursor.execute(
            "SELECT pg_catalog.set_config("
            "'search_path', 'pg_catalog', true)"
        )
        cursor.execute(
            "SELECT active.schema_version AS active_version, "
            "evidence.schema_version AS evidence_version, "
            "evidence.contract_version AS evidence_contract, "
            "semantic.schema_version AS semantic_version, "
            "semantic.contract_version AS semantic_contract "
            "FROM public.trace_backed_memory_schema AS active "
            "CROSS JOIN "
            "trace_backed_memory_v3_gate_evidence.schema_metadata "
            "AS evidence "
            "CROSS JOIN "
            "trace_backed_memory_v3_semantic_gate.schema_metadata "
            "AS semantic "
            "WHERE active.singleton "
            "AND evidence.singleton = 1 AND semantic.singleton = 1 "
            "FOR SHARE OF active, evidence, semantic"
        )
        if cursor.fetchall() != [{
            "active_version": 2,
            "evidence_version": 1,
            "evidence_contract": "tbm.gate-evidence.v3",
            "semantic_version": POSTGRES_SEMANTIC_GATE_V3_SCHEMA_VERSION,
            "semantic_contract": POSTGRES_SEMANTIC_GATE_V3_CONTRACT_VERSION,
        }]:
            raise PostgresSemanticGateV3SchemaError(
                "PostgreSQL semantic Gate v3 metadata mismatch"
            )
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_semantic_gate.schema_metadata, "
            "trace_backed_memory_v3_semantic_gate."
            "v3_semantic_gate_attempt_heads, "
            "trace_backed_memory_v3_semantic_gate."
            "v3_semantic_gate_attempts "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_schema_catalog(cursor)
        return original_search_path

    def _verify_schema_catalog(self, cursor: object) -> None:
        cursor.execute(
            "SELECT "
            "(SELECT count(*) FROM pg_catalog.pg_policy AS policy "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = policy.polrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s) AS policy_count, "
            "(SELECT count(*) FROM pg_catalog.pg_rewrite AS rule "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = rule.ev_class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND rule.rulename <> '_RETURN') AS rule_count, "
            "(SELECT count(*) FROM pg_catalog.pg_class AS class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND class.relkind NOT IN ('r', 'i', 'p')) "
            "AS unsupported_relation_count",
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [{
            "policy_count": 0,
            "rule_count": 0,
            "unsupported_relation_count": 0,
        }]:
            raise PostgresSemanticGateV3SchemaError(
                "PostgreSQL semantic Gate v3 contains unsupported "
                "policies, rules, or relation kinds"
            )
        cursor.execute(
            _POSTGRES_SEMANTIC_GATE_CATALOG_SHA256_QUERY,
            (_SCHEMA,) * 7,
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0].get("catalog_sha256")
            != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresSemanticGateV3SchemaError(
                "PostgreSQL semantic Gate v3 catalog does not match"
            )

    @contextmanager
    def _secured_cursor(self, *, for_write: bool) -> Iterator[object]:
        with self._cursor() as cursor:
            original_search_path = self._lock_schema(
                cursor,
                for_write=for_write,
            )
            try:
                yield cursor
            except Exception:
                raise
            else:
                self._verify_schema_catalog(cursor)
                cursor.execute(
                    "SELECT pg_catalog.set_config("
                    "'search_path', %s, true)",
                    (original_search_path,),
                )

    @staticmethod
    def _attempt_values(
        attempt: SemanticGateAttempt,
    ) -> tuple[object, ...]:
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

    @classmethod
    def _stored_attempt(
        cls,
        row: Mapping[str, object],
    ) -> SemanticGateAttempt:
        values = (
            row.get("attempt_id"),
            row.get("session_id"),
            row.get("retrieval_snapshot_id"),
            row.get("system_gate_evaluation_id"),
            row.get("sequence"),
            row.get("previous_attempt_id"),
            row.get("status"),
            row.get("started_at"),
            row.get("finished_at"),
            row.get("descriptor"),
        )
        if type(values[9]) is not str:
            cls._persistence(
                "stored Semantic Gate attempt row has invalid shape"
            )
        try:
            attempt = loads_semantic_gate_attempt(cast(str, values[9]))
        except GateEvaluationContractError as error:
            raise PostgresSemanticGateV3PersistenceError(
                "stored Semantic Gate attempt descriptor failed validation"
            ) from error
        if values != cls._attempt_values(attempt):
            cls._persistence(
                "stored Semantic Gate attempt columns do not match descriptor"
            )
        return attempt

    @classmethod
    def _stored_head(
        cls,
        row: Mapping[str, object],
    ) -> _AttemptHead:
        values = (
            row.get("system_gate_evaluation_id"),
            row.get("session_id"),
            row.get("retrieval_snapshot_id"),
            row.get("current_sequence"),
            row.get("current_attempt_id"),
        )
        if (
            type(values[0]) is not str
            or type(values[1]) is not str
            or type(values[2]) is not str
            or type(values[3]) is not int
            or (values[4] is not None and type(values[4]) is not str)
        ):
            cls._persistence(
                "stored Semantic Gate head row has invalid shape"
            )
        return _AttemptHead(
            system_gate_evaluation_id=cast(str, values[0]),
            session_id=cast(str, values[1]),
            retrieval_snapshot_id=cast(str, values[2]),
            current_sequence=cast(int, values[3]),
            current_attempt_id=cast(str | None, values[4]),
        )

    @classmethod
    def _stored_evaluation(
        cls,
        row: Mapping[str, object],
    ) -> SystemGateEvaluation:
        values = (
            row.get("evaluation_id"),
            row.get("session_id"),
            row.get("retrieval_snapshot_id"),
            row.get("authorization_event_id"),
            row.get("descriptor"),
        )
        if type(values[4]) is not str:
            cls._persistence("stored System Gate row has invalid shape")
        try:
            evaluation = loads_system_gate_evaluation(
                cast(str, values[4])
            )
        except GateEvaluationContractError as error:
            raise PostgresSemanticGateV3PersistenceError(
                "stored System Gate descriptor failed validation"
            ) from error
        if values != (
            evaluation.evaluation_id,
            evaluation.session_id,
            evaluation.retrieval_snapshot_id,
            evaluation.authorization_event_id,
            values[4],
        ):
            cls._persistence(
                "stored System Gate columns do not match descriptor"
            )
        return evaluation

    @classmethod
    def _stored_snapshot(
        cls,
        row: Mapping[str, object],
    ) -> RetrievalSnapshot:
        values = (
            row.get("snapshot_id"),
            row.get("session_id"),
            row.get("authorization_event_id"),
            row.get("descriptor"),
        )
        if type(values[3]) is not str:
            cls._persistence(
                "stored retrieval snapshot row has invalid shape"
            )
        try:
            snapshot = loads_retrieval_snapshot(cast(str, values[3]))
        except ValueError as error:
            raise PostgresSemanticGateV3PersistenceError(
                "stored retrieval snapshot descriptor failed validation"
            ) from error
        if values != (
            snapshot.snapshot_id,
            snapshot.session_id,
            snapshot.authorization_event_id,
            values[3],
        ):
            cls._persistence(
                "stored retrieval snapshot columns do not match descriptor"
            )
        return snapshot

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise PostgresSemanticGateV3PersistenceError(message)

    def _load_gate_records(
        self,
        cursor: object,
        evaluation_id: str,
    ) -> tuple[SystemGateEvaluation, RetrievalSnapshot]:
        cursor.execute(
            "SELECT evaluation_id, session_id, retrieval_snapshot_id, "
            "authorization_event_id, descriptor FROM "
            "trace_backed_memory_v3_gate_evidence."
            "v3_system_gate_evaluations WHERE evaluation_id = %s",
            (evaluation_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresSemanticGateV3NotFoundError(
                "System Gate evaluation was not found"
            )
        if len(rows) != 1:
            self._persistence(
                "System Gate evaluation lookup is ambiguous"
            )
        unlocked_evaluation = self._stored_evaluation(rows[0])
        cursor.execute(
            "SELECT snapshot_id, session_id, authorization_event_id, "
            "descriptor FROM trace_backed_memory_v3_gate_evidence."
            "v3_retrieval_snapshots WHERE snapshot_id = %s FOR SHARE",
            (unlocked_evaluation.retrieval_snapshot_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            self._persistence(
                "System Gate evaluation references a missing snapshot"
            )
        snapshot = self._stored_snapshot(rows[0])
        cursor.execute(
            "SELECT evaluation_id, session_id, retrieval_snapshot_id, "
            "authorization_event_id, descriptor FROM "
            "trace_backed_memory_v3_gate_evidence."
            "v3_system_gate_evaluations WHERE evaluation_id = %s FOR SHARE",
            (evaluation_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            self._persistence(
                "System Gate evaluation lookup changed during lock"
            )
        evaluation = self._stored_evaluation(rows[0])
        try:
            verify_system_gate_evaluation(evaluation, snapshot)
        except GateEvaluationContractError as error:
            raise PostgresSemanticGateV3PersistenceError(
                "stored Gate evidence failed cross-record validation"
            ) from error
        return evaluation, snapshot

    def _select_head(
        self,
        cursor: object,
        evaluation_id: str,
        *,
        for_update: bool,
    ) -> _AttemptHead | None:
        cursor.execute(
            "SELECT system_gate_evaluation_id, session_id, "
            "retrieval_snapshot_id, current_sequence, current_attempt_id "
            f"FROM {_SCHEMA}.v3_semantic_gate_attempt_heads "
            "WHERE system_gate_evaluation_id = %s "
            f"FOR {'UPDATE' if for_update else 'SHARE'}",
            (evaluation_id,),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            self._persistence("Semantic Gate head lookup is ambiguous")
        return None if not rows else self._stored_head(rows[0])

    def _select_attempt(
        self,
        cursor: object,
        attempt_id: str,
    ) -> SemanticGateAttempt | None:
        cursor.execute(
            "SELECT attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor "
            f"FROM {_SCHEMA}.v3_semantic_gate_attempts "
            "WHERE attempt_id = %s FOR SHARE",
            (attempt_id,),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            self._persistence("Semantic Gate attempt lookup is ambiguous")
        return None if not rows else self._stored_attempt(rows[0])

    def _load_chain(
        self,
        cursor: object,
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
            self._persistence(
                "stored Semantic Gate head does not match Gate evidence"
            )
        cursor.execute(
            "SELECT attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor "
            f"FROM {_SCHEMA}.v3_semantic_gate_attempts "
            "WHERE system_gate_evaluation_id = %s "
            "ORDER BY sequence FOR SHARE",
            (evaluation.evaluation_id,),
        )
        attempts = tuple(
            self._stored_attempt(row) for row in cursor.fetchall()
        )
        if (
            len(attempts) != head.current_sequence
            or attempts[-1].attempt_id != head.current_attempt_id
        ):
            self._persistence(
                "stored Semantic Gate head does not match its attempt chain"
            )
        try:
            verify_semantic_gate_attempt_chain(
                attempts,
                evaluation,
                snapshot,
            )
        except GateEvaluationContractError as error:
            raise PostgresSemanticGateV3PersistenceError(
                "stored Semantic Gate attempt chain failed validation"
            ) from error
        return attempts

    def _ensure_head(
        self,
        cursor: object,
        evaluation: SystemGateEvaluation,
        snapshot: RetrievalSnapshot,
    ) -> _AttemptHead:
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.v3_semantic_gate_attempt_heads "
            "(system_gate_evaluation_id, session_id, "
            "retrieval_snapshot_id, current_sequence, current_attempt_id) "
            "VALUES (%s, %s, %s, 0, NULL) "
            "ON CONFLICT (system_gate_evaluation_id) DO NOTHING",
            (
                evaluation.evaluation_id,
                evaluation.session_id,
                snapshot.snapshot_id,
            ),
        )
        head = self._select_head(
            cursor,
            evaluation.evaluation_id,
            for_update=True,
        )
        if head is None:
            self._persistence("Semantic Gate head is missing after insert")
        return head

    def _append_attempt(
        self,
        cursor: object,
        attempt: SemanticGateAttempt,
        evaluation: SystemGateEvaluation,
        snapshot: RetrievalSnapshot,
    ) -> bool:
        head = self._ensure_head(cursor, evaluation, snapshot)
        existing = self._select_attempt(cursor, attempt.attempt_id)
        if existing is not None:
            if self._attempt_values(existing) != self._attempt_values(attempt):
                raise PostgresSemanticGateV3ConflictError(
                    "Semantic Gate attempt ID has conflicting content"
                )
            if attempt not in self._load_chain(
                cursor,
                evaluation,
                snapshot,
                head,
            ):
                self._persistence(
                    "stored Semantic Gate replay is outside its chain"
                )
            return False

        parent: SemanticGateAttempt | None
        if head.current_sequence == 0:
            parent = None
        else:
            chain = self._load_chain(
                cursor,
                evaluation,
                snapshot,
                head,
            )
            parent = chain[-1]
        try:
            verify_semantic_gate_attempt_parent(attempt, parent)
        except GateEvaluationContractError as error:
            raise PostgresSemanticGateV3ConflictError(
                "Semantic Gate attempt does not extend the current chain"
            ) from error
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.v3_semantic_gate_attempts ("
            "attempt_id, session_id, retrieval_snapshot_id, "
            "system_gate_evaluation_id, sequence, previous_attempt_id, "
            "status, started_at, finished_at, descriptor"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            self._attempt_values(attempt),
        )
        cursor.execute(
            f"UPDATE {_SCHEMA}.v3_semantic_gate_attempt_heads "
            "SET current_sequence = %s, current_attempt_id = %s "
            "WHERE system_gate_evaluation_id = %s "
            "AND current_sequence = %s "
            "AND current_attempt_id IS NOT DISTINCT FROM %s",
            (
                attempt.sequence,
                attempt.attempt_id,
                attempt.system_gate_evaluation_id,
                head.current_sequence,
                head.current_attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise PostgresSemanticGateV3ConflictError(
                "Semantic Gate attempt chain changed during append"
            )
        return True

    @_synchronized
    def store_attempt(
        self,
        attempt: SemanticGateAttempt,
    ) -> PostgresSemanticGateV3StoreResult:
        if type(attempt) is not SemanticGateAttempt:
            raise ValueError("attempt must be exactly SemanticGateAttempt")
        if attempt.sequence > GATE_EVALUATION_MAX_DECISIONS:
            raise ValueError(
                "semantic Gate attempt sequence exceeds ledger bound"
            )
        self._require_open()
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=True) as cursor:
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
                        raise PostgresSemanticGateV3ConflictError(
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
                        for_update=False,
                    )
                    if head is None:
                        self._persistence(
                            "Semantic Gate head is missing after append"
                        )
                    chain = self._load_chain(
                        cursor,
                        evaluation,
                        snapshot,
                        head,
                    )
                    if attempt not in chain:
                        self._persistence(
                            "Semantic Gate attempt read-back does not match"
                        )
            return PostgresSemanticGateV3StoreResult(
                attempt.attempt_id,
                attempt.sequence,
                inserted,
            )
        except (
            PostgresSemanticGateV3Error,
            ValueError,
        ):
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_attempt(self, attempt_id: str) -> SemanticGateAttempt:
        self._require_open()
        if (
            type(attempt_id) is not str
            or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None
        ):
            raise ValueError(
                "attempt_id must be a v3 Semantic Gate attempt ID"
            )
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    attempt = self._select_attempt(cursor, attempt_id)
                    if attempt is None:
                        raise PostgresSemanticGateV3NotFoundError(
                            "Semantic Gate attempt was not found"
                        )
                    evaluation, snapshot = self._load_gate_records(
                        cursor,
                        attempt.system_gate_evaluation_id,
                    )
                    head = self._select_head(
                        cursor,
                        attempt.system_gate_evaluation_id,
                        for_update=False,
                    )
                    if head is None or attempt not in self._load_chain(
                        cursor,
                        evaluation,
                        snapshot,
                        head,
                    ):
                        self._persistence(
                            "stored Semantic Gate attempt is outside its chain"
                        )
                    return attempt
        except PostgresSemanticGateV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_chain(
        self,
        evaluation_id: str,
    ) -> tuple[SemanticGateAttempt, ...]:
        self._require_open()
        if (
            type(evaluation_id) is not str
            or _EVALUATION_ID_RE.fullmatch(evaluation_id) is None
        ):
            raise ValueError(
                "evaluation_id must be a v3 System Gate evaluation ID"
            )
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    evaluation, snapshot = self._load_gate_records(
                        cursor,
                        evaluation_id,
                    )
                    head = self._select_head(
                        cursor,
                        evaluation_id,
                        for_update=False,
                    )
                    if head is None:
                        raise PostgresSemanticGateV3NotFoundError(
                            "Semantic Gate attempt chain was not found"
                        )
                    return self._load_chain(
                        cursor,
                        evaluation,
                        snapshot,
                        head,
                    )
        except PostgresSemanticGateV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    def _raise_database(self, error: Exception) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresSemanticGateV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise PostgresSemanticGateV3ConflictError(
                "Semantic Gate attempt conflicts with immutable "
                "PostgreSQL storage"
            ) from error
        raise PostgresSemanticGateV3PersistenceError(
            "PostgreSQL semantic Gate v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresSemanticGateV3Repository:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()


__all__ = [
    "POSTGRES_SEMANTIC_GATE_V3_CONTRACT_VERSION",
    "POSTGRES_SEMANTIC_GATE_V3_SCHEMA_VERSION",
    "PostgresSemanticGateV3ConflictError",
    "PostgresSemanticGateV3Error",
    "PostgresSemanticGateV3NotFoundError",
    "PostgresSemanticGateV3PersistenceError",
    "PostgresSemanticGateV3Repository",
    "PostgresSemanticGateV3SchemaError",
    "PostgresSemanticGateV3StoreResult",
]
