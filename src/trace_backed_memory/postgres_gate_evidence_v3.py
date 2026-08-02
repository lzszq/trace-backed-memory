from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import re
from threading import RLock
from typing import Iterator, NoReturn, ParamSpec, TypeVar, cast

from .event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    verify_event_trusted_context,
)
from .gate_evaluation_v3 import (
    SystemGateEvaluation,
    dumps_system_gate_evaluation,
    loads_system_gate_evaluation,
    verify_system_gate_evaluation,
)
from .gate_evidence_event_v1 import (
    RETRIEVAL_PREPARED_EVENT,
    SYSTEM_GATE_EVALUATED_EVENT,
    GateEvidenceEventV1Error,
    GateEvidenceRecordRef,
    build_retrieval_prepared_event,
    build_system_gate_evaluated_event,
    parse_gate_evidence_event,
    retrieval_snapshot_record_ref,
    system_gate_evaluation_record_ref,
)
from .ledger_port_v1 import (
    EventLedgerPortError,
    LedgerAccessContext,
    LedgerAppendRequest,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from .postgres import _load_psycopg
from .postgres_authorization_v3 import _CATALOG_SHA256_QUERY
from .retrieval_v3 import (
    RetrievalSnapshot,
    dumps_retrieval_snapshot,
    loads_retrieval_snapshot,
)


POSTGRES_GATE_EVIDENCE_V3_SCHEMA_VERSION = 1
POSTGRES_GATE_EVIDENCE_V3_CONTRACT_VERSION = "tbm.gate-evidence.v3"
_SCHEMA = "trace_backed_memory_v3_gate_evidence"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL gate evidence v3 schema is missing or incomplete"
)
_EXPECTED_CATALOG_SHA256 = (
    "5ad6a578df537848ed0fcffb8ddd3212c46dcd0257fb5b8fd620ca818fbbbd5f"
)
_GATE_EVIDENCE_CATALOG_SHA256_QUERY = _CATALOG_SHA256_QUERY.replace(
    "trace_backed_memory_v3_authorization.",
    "trace_backed_memory_v3_gate_evidence.",
).replace(" || '|' ||\n           attribute.attcompression::text", "")
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_SNAPSHOT_ID_RE = re.compile(r"retrieval_snapshot_sha256_[0-9a-f]{64}")
_EVALUATION_ID_RE = re.compile(r"system_gate_sha256_[0-9a-f]{64}")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresGateEvidenceV3Error(RuntimeError):
    pass


class PostgresGateEvidenceV3SchemaError(PostgresGateEvidenceV3Error):
    pass


class PostgresGateEvidenceV3ConflictError(PostgresGateEvidenceV3Error):
    pass


class PostgresGateEvidenceV3NotFoundError(PostgresGateEvidenceV3Error):
    pass


class PostgresGateEvidenceV3PersistenceError(PostgresGateEvidenceV3Error):
    pass


@dataclass(frozen=True)
class PostgresGateEvidenceV3StoreResult:
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


class PostgresGateEvidenceV3Repository:
    """Immutable PostgreSQL ledger for retrieval and System Gate evidence."""

    def __init__(self, connection: object, *, owns_connection: bool = False):
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False
        self._event_first = False
        self._event_context: ContextVar[EventTrustedContext | None] = ContextVar(
            f"tbm_postgres_gate_evidence_event_context_{id(self)}",
            default=None,
        )

    @_synchronized
    def enable_event_first(self) -> None:
        self._require_open()
        transaction_status = getattr(
            getattr(self._connection, "info", None),
            "transaction_status",
            None,
        )
        if transaction_status is not None and int(transaction_status) != 0:
            raise PostgresGateEvidenceV3ConflictError(
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
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresGateEvidenceV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresGateEvidenceV3PersistenceError(
                "failed to connect to PostgreSQL gate evidence v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresGateEvidenceV3Error(
                "PostgreSQL gate evidence v3 repository is closed"
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            "SELECT active.schema_version AS active_version, "
            "evidence.schema_version AS evidence_version, "
            "evidence.contract_version AS contract_version "
            "FROM public.trace_backed_memory_schema AS active "
            "CROSS JOIN "
            "trace_backed_memory_v3_gate_evidence.schema_metadata "
            "AS evidence "
            "WHERE active.singleton AND evidence.singleton = 1 "
            "FOR SHARE OF active, evidence"
        )
        rows = cursor.fetchall()
        if rows != [
            {
                "active_version": 2,
                "evidence_version": POSTGRES_GATE_EVIDENCE_V3_SCHEMA_VERSION,
                "contract_version": POSTGRES_GATE_EVIDENCE_V3_CONTRACT_VERSION,
            }
        ]:
            raise PostgresGateEvidenceV3SchemaError(
                "PostgreSQL gate evidence v3 metadata mismatch"
            )
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
        if cursor.fetchall() != [
            {
                "policy_count": 0,
                "rule_count": 0,
                "unsupported_relation_count": 0,
            }
        ]:
            raise PostgresGateEvidenceV3SchemaError(
                "PostgreSQL gate evidence v3 contains unsupported "
                "policies, rules, or relation kinds"
            )
        cursor.execute(
            _GATE_EVIDENCE_CATALOG_SHA256_QUERY,
            (_SCHEMA,) * 7,
        )
        catalog_rows = cursor.fetchall()
        if (
            len(catalog_rows) != 1
            or catalog_rows[0].get("catalog_sha256")
            != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresGateEvidenceV3SchemaError(
                "PostgreSQL gate evidence v3 catalog does not match"
            )

    @staticmethod
    def _snapshot_values(
        snapshot: RetrievalSnapshot,
    ) -> tuple[str, str, str, str]:
        return (
            snapshot.snapshot_id,
            snapshot.session_id,
            snapshot.authorization_event_id,
            dumps_retrieval_snapshot(snapshot),
        )

    @staticmethod
    def _evaluation_values(
        evaluation: SystemGateEvaluation,
    ) -> tuple[str, str, str, str, str]:
        return (
            evaluation.evaluation_id,
            evaluation.session_id,
            evaluation.retrieval_snapshot_id,
            evaluation.authorization_event_id,
            dumps_system_gate_evaluation(evaluation),
        )

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
            cls._persistence("retrieval snapshot row has invalid shape")
        try:
            snapshot = loads_retrieval_snapshot(cast(str, values[3]))
        except ValueError as error:
            raise PostgresGateEvidenceV3PersistenceError(
                "stored retrieval snapshot failed validation"
            ) from error
        if values != cls._snapshot_values(snapshot):
            cls._persistence(
                "retrieval snapshot columns do not match descriptor"
            )
        return snapshot

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
            cls._persistence("System Gate evaluation row has invalid shape")
        try:
            evaluation = loads_system_gate_evaluation(
                cast(str, values[4])
            )
        except ValueError as error:
            raise PostgresGateEvidenceV3PersistenceError(
                "stored System Gate evaluation failed validation"
            ) from error
        if values != cls._evaluation_values(evaluation):
            cls._persistence(
                "System Gate evaluation columns do not match descriptor"
            )
        return evaluation

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise PostgresGateEvidenceV3PersistenceError(message)

    def _put_snapshot(
        self,
        cursor: object,
        snapshot: RetrievalSnapshot,
    ) -> bool:
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.v3_retrieval_snapshots "
            "(snapshot_id, session_id, authorization_event_id, descriptor) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING snapshot_id",
            self._snapshot_values(snapshot),
        )
        inserted = cursor.fetchone() is not None
        cursor.execute(
            "SELECT snapshot_id, session_id, authorization_event_id, "
            f"descriptor FROM {_SCHEMA}.v3_retrieval_snapshots "
            "WHERE snapshot_id = %s FOR SHARE",
            (snapshot.snapshot_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            self._persistence("retrieval snapshot lookup is ambiguous")
        stored = self._stored_snapshot(rows[0])
        if stored != snapshot:
            raise PostgresGateEvidenceV3ConflictError(
                "retrieval snapshot ID has conflicting immutable content"
            )
        return inserted

    def _put_evaluation(
        self,
        cursor: object,
        evaluation: SystemGateEvaluation,
    ) -> bool:
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.v3_system_gate_evaluations "
            "(evaluation_id, session_id, retrieval_snapshot_id, "
            "authorization_event_id, descriptor) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING evaluation_id",
            self._evaluation_values(evaluation),
        )
        inserted = cursor.fetchone() is not None
        cursor.execute(
            "SELECT evaluation_id, session_id, retrieval_snapshot_id, "
            f"authorization_event_id, descriptor FROM {_SCHEMA}."
            "v3_system_gate_evaluations "
            "WHERE evaluation_id = %s OR retrieval_snapshot_id = %s "
            "FOR SHARE",
            (
                evaluation.evaluation_id,
                evaluation.retrieval_snapshot_id,
            ),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            self._persistence("System Gate evaluation lookup is ambiguous")
        stored = self._stored_evaluation(rows[0])
        if stored != evaluation:
            raise PostgresGateEvidenceV3ConflictError(
                "System Gate evaluation identity has conflicting content"
            )
        return inserted

    def _event_access(
        self,
        authorization_event_id: str,
    ) -> tuple[EventTrustedContext, LedgerAccessContext]:
        trusted_context = self._event_context.get()
        if trusted_context is None:
            raise PostgresGateEvidenceV3ConflictError(
                "event-first gate evidence mutation requires trusted event context"
            )
        if (
            trusted_context.authorization_decision_id
            != authorization_event_id
        ):
            raise PostgresGateEvidenceV3ConflictError(
                "trusted event context does not match gate evidence authorization"
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
    def _verify_retained_event(
        event: CanonicalEvent,
        expected_type: str,
        expected_ref: GateEvidenceRecordRef,
        trusted_context: EventTrustedContext,
    ) -> None:
        try:
            verify_event_trusted_context(event, trusted_context)
            retained_ref = parse_gate_evidence_event(event)
        except Exception as error:
            raise PostgresGateEvidenceV3ConflictError(
                "retained gate evidence event failed validation"
            ) from error
        if event.event_type != expected_type or retained_ref != expected_ref:
            raise PostgresGateEvidenceV3ConflictError(
                "gate evidence stream has conflicting immutable content"
            )

    def _prepare_event_first_write(self, cursor: object) -> None:
        if not self._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        trusted_context = self._event_context.get()
        if trusted_context is None:
            raise PostgresGateEvidenceV3ConflictError(
                "event-first gate evidence mutation requires trusted event context"
            )
        _trusted, access = self._event_access(
            trusted_context.authorization_decision_id
        )
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            ledger._lock_schema(cursor, write=True)
            ledger._select_global_position(cursor, for_update=True)
        finally:
            ledger.close()

    def _append_bundle_events(
        self,
        cursor: object,
        snapshot: RetrievalSnapshot,
        evaluation: SystemGateEvaluation,
    ) -> None:
        if not self._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        trusted_context, access = self._event_access(
            snapshot.authorization_event_id
        )
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            ledger._lock_schema(cursor, write=True)
            retrieval_event = ledger._select_head_event(
                cursor,
                snapshot.snapshot_id,
                for_update=False,
            )
            expected_retrieval = retrieval_snapshot_record_ref(snapshot)
            if retrieval_event is None:
                retrieval_event = build_retrieval_prepared_event(
                    snapshot,
                    global_position=(
                        ledger._select_global_position(
                            cursor,
                            for_update=True,
                        )
                        + 1
                    ),
                    trusted_context=trusted_context,
                )
                ledger._append_in_transaction(
                    cursor,
                    LedgerAppendRequest(
                        access=access,
                        stream_id=snapshot.snapshot_id,
                        expected_stream_version=0,
                        events=(retrieval_event,),
                        idempotency=LedgerIdempotency(
                            retrieval_event.idempotency_key_sha256,
                            retrieval_event.request_sha256,
                        ),
                    ),
                )
            else:
                self._verify_retained_event(
                    retrieval_event,
                    RETRIEVAL_PREPARED_EVENT,
                    expected_retrieval,
                    trusted_context,
                )

            evaluation_event = ledger._select_head_event(
                cursor,
                evaluation.evaluation_id,
                for_update=False,
            )
            expected_evaluation = system_gate_evaluation_record_ref(
                evaluation,
                causation_event_id=retrieval_event.event_id,
            )
            if evaluation_event is None:
                evaluation_event = build_system_gate_evaluated_event(
                    evaluation,
                    retrieval_event=retrieval_event,
                    global_position=(
                        ledger._select_global_position(
                            cursor,
                            for_update=True,
                        )
                        + 1
                    ),
                    trusted_context=trusted_context,
                )
                ledger._append_in_transaction(
                    cursor,
                    LedgerAppendRequest(
                        access=access,
                        stream_id=evaluation.evaluation_id,
                        expected_stream_version=0,
                        events=(evaluation_event,),
                        idempotency=LedgerIdempotency(
                            evaluation_event.idempotency_key_sha256,
                            evaluation_event.request_sha256,
                        ),
                    ),
                )
            else:
                self._verify_retained_event(
                    evaluation_event,
                    SYSTEM_GATE_EVALUATED_EVENT,
                    expected_evaluation,
                    trusted_context,
                )
        finally:
            ledger.close()

    @_synchronized
    def store_bundle(
        self,
        snapshot: RetrievalSnapshot,
        evaluation: SystemGateEvaluation,
    ) -> PostgresGateEvidenceV3StoreResult:
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
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._prepare_event_first_write(cursor)
                    self._lock_schema(cursor)
                    self._append_bundle_events(cursor, snapshot, evaluation)
                    snapshot_inserted = self._put_snapshot(cursor, snapshot)
                    evaluation_inserted = self._put_evaluation(
                        cursor,
                        evaluation,
                    )
            return PostgresGateEvidenceV3StoreResult(
                snapshot_id=snapshot.snapshot_id,
                snapshot_inserted=snapshot_inserted,
                evaluation_id=evaluation.evaluation_id,
                evaluation_inserted=evaluation_inserted,
            )
        except (
            PostgresGateEvidenceV3Error,
            ValueError,
        ):
            raise
        except (GateEvidenceEventV1Error, EventLedgerPortError) as error:
            raise PostgresGateEvidenceV3ConflictError(
                "gate evidence event conflicts with immutable storage"
            ) from error
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot:
        self._require_open()
        if (
            type(snapshot_id) is not str
            or _SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None
        ):
            raise ValueError("snapshot_id must be a v3 snapshot ID")
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        "SELECT snapshot_id, session_id, "
                        f"authorization_event_id, descriptor FROM {_SCHEMA}."
                        "v3_retrieval_snapshots WHERE snapshot_id = %s "
                        "FOR SHARE",
                        (snapshot_id,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise PostgresGateEvidenceV3NotFoundError(
                            "retrieval snapshot was not found"
                        )
                    if len(rows) != 1:
                        self._persistence(
                            "retrieval snapshot lookup is ambiguous"
                        )
                    return self._stored_snapshot(rows[0])
        except PostgresGateEvidenceV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_evaluation(
        self,
        evaluation_id: str,
    ) -> SystemGateEvaluation:
        self._require_open()
        if (
            type(evaluation_id) is not str
            or _EVALUATION_ID_RE.fullmatch(evaluation_id) is None
        ):
            raise ValueError("evaluation_id must be a v3 evaluation ID")
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        "SELECT evaluation_id, session_id, "
                        "retrieval_snapshot_id, authorization_event_id, "
                        f"descriptor FROM {_SCHEMA}."
                        "v3_system_gate_evaluations "
                        "WHERE evaluation_id = %s FOR SHARE",
                        (evaluation_id,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise PostgresGateEvidenceV3NotFoundError(
                            "System Gate evaluation was not found"
                        )
                    if len(rows) != 1:
                        self._persistence(
                            "System Gate evaluation lookup is ambiguous"
                        )
                    return self._stored_evaluation(rows[0])
        except PostgresGateEvidenceV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    def _raise_database(self, error: Exception) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresGateEvidenceV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise PostgresGateEvidenceV3ConflictError(
                "gate evidence conflicts with immutable PostgreSQL storage"
            ) from error
        raise PostgresGateEvidenceV3PersistenceError(
            "PostgreSQL gate evidence v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresGateEvidenceV3Repository:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()
