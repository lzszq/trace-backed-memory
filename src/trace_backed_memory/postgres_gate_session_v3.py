from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache, wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from ._timestamps import (
    aware_datetime_to_rfc3339,
    canonical_rfc3339,
    parse_rfc3339,
)
from .contracts_v3 import V3ContractError
from .event_v1 import EventTrustedContext, loads_canonical_event
from .gate_session_event_v1 import build_gate_session_event
from .gate_session_v3 import (
    GATE_SESSION_CONTRACT_VERSION,
    GATE_SESSION_MAX_BYTES,
    GATE_SESSION_MAX_LEASE_SECONDS,
    GATE_SESSION_MAX_TTL_SECONDS,
    GateSession,
    GateSessionContractError,
    GateSessionStatus,
    create_gate_session,
    dumps_gate_session,
    loads_gate_session,
    renew_gate_session_lease,
    transition_gate_session,
)
from .ledger_port_v1 import (
    LedgerAccessContext,
    LedgerAppendRequest,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from .postgres import _load_psycopg
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_GATE_SESSION_SCHEMA_VERSION = 1
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL GateSession v3 schema is missing or incomplete"
)
_SCHEMA = "trace_backed_memory_v3_gate_session"
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_EXPECTED_RELATIONS = frozenset(
    {
        "gate_session_heads",
        "gate_session_heads_idempotency_key",
        "gate_session_heads_pkey",
        "gate_session_revisions",
        "gate_session_revisions_due",
        "gate_session_revisions_pkey",
        "schema_metadata",
        "schema_metadata_pkey",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {
        "protect_head_update",
        "reject_immutable_change",
        "validate_head_revision_consistency",
        "validate_revision_insert",
    }
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "gate_session_heads_immutable_delete",
        "gate_session_heads_insert_consistent_revision",
        "gate_session_heads_no_truncate",
        "gate_session_heads_protect_update",
        "gate_session_heads_update_consistent_revision",
        "gate_session_metadata_immutable",
        "gate_session_metadata_no_truncate",
        "gate_session_revisions_consistent_head",
        "gate_session_revisions_immutable_change",
        "gate_session_revisions_no_truncate",
        "gate_session_revisions_validate_insert",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "gate_session_heads_immutable_delete",
            "gate_session_heads",
            "reject_immutable_change",
            11,
        ),
        (
            "gate_session_heads_insert_consistent_revision",
            "gate_session_heads",
            "validate_head_revision_consistency",
            5,
        ),
        (
            "gate_session_heads_no_truncate",
            "gate_session_heads",
            "reject_immutable_change",
            34,
        ),
        (
            "gate_session_heads_protect_update",
            "gate_session_heads",
            "protect_head_update",
            19,
        ),
        (
            "gate_session_heads_update_consistent_revision",
            "gate_session_heads",
            "validate_head_revision_consistency",
            17,
        ),
        (
            "gate_session_metadata_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "gate_session_metadata_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
        (
            "gate_session_revisions_consistent_head",
            "gate_session_revisions",
            "validate_head_revision_consistency",
            5,
        ),
        (
            "gate_session_revisions_immutable_change",
            "gate_session_revisions",
            "reject_immutable_change",
            27,
        ),
        (
            "gate_session_revisions_no_truncate",
            "gate_session_revisions",
            "reject_immutable_change",
            34,
        ),
        (
            "gate_session_revisions_validate_insert",
            "gate_session_revisions",
            "validate_revision_insert",
            7,
        ),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "gate_session_heads_agent_client_id_check",
        "gate_session_heads_current_version_check",
        "gate_session_heads_idempotency_key",
        "gate_session_heads_idempotency_key_check",
        "gate_session_heads_insert_consistent_revision",
        "gate_session_heads_pkey",
        "gate_session_heads_principal_id_check",
        "gate_session_heads_repository_id_check",
        "gate_session_heads_request_fingerprint_check",
        "gate_session_heads_run_id_check",
        "gate_session_heads_session_id_check",
        "gate_session_heads_tenant_id_check",
        "gate_session_heads_trace_id_check",
        "gate_session_heads_update_consistent_revision",
        "gate_session_revisions_consistent_head",
        "gate_session_revisions_expiry_shape",
        "gate_session_revisions_head_fkey",
        "gate_session_revisions_lease_shape",
        "gate_session_revisions_payload_check",
        "gate_session_revisions_pkey",
        "gate_session_revisions_status_check",
        "gate_session_revisions_version_check",
        "schema_metadata_contract_version_check",
        "schema_metadata_pkey",
        "schema_metadata_schema_version_check",
        "schema_metadata_singleton_check",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("gate_session_heads", "agent_client_id", "text", "NO", "C"),
        ("gate_session_heads", "current_version", "integer", "NO", None),
        ("gate_session_heads", "idempotency_key", "text", "NO", "C"),
        ("gate_session_heads", "principal_id", "text", "NO", "C"),
        ("gate_session_heads", "repository_id", "text", "NO", "C"),
        ("gate_session_heads", "request_fingerprint", "text", "NO", "C"),
        ("gate_session_heads", "run_id", "text", "NO", "C"),
        ("gate_session_heads", "session_id", "text", "NO", "C"),
        ("gate_session_heads", "tenant_id", "text", "NO", "C"),
        ("gate_session_heads", "trace_id", "text", "NO", "C"),
        (
            "gate_session_revisions",
            "expires_at",
            "timestamp with time zone",
            "NO",
            None,
        ),
        (
            "gate_session_revisions",
            "lease_expires_at",
            "timestamp with time zone",
            "YES",
            None,
        ),
        ("gate_session_revisions", "payload", "text", "NO", "C"),
        ("gate_session_revisions", "session_id", "text", "NO", "C"),
        ("gate_session_revisions", "status", "text", "NO", "C"),
        (
            "gate_session_revisions",
            "updated_at",
            "timestamp with time zone",
            "NO",
            None,
        ),
        ("gate_session_revisions", "version", "integer", "NO", None),
        ("schema_metadata", "contract_version", "text", "NO", "C"),
        ("schema_metadata", "schema_version", "integer", "NO", None),
        ("schema_metadata", "singleton", "boolean", "NO", None),
    }
)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_FUNCTION_BODY_PATTERN = re.compile(
    r"CREATE FUNCTION\s+"
    r"trace_backed_memory_v3_gate_session\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)


class PostgresGateSessionError(V3ContractError):
    """Stable base failure for the isolated PostgreSQL GateSession store."""


class PostgresGateSessionSchemaError(PostgresGateSessionError):
    pass


class PostgresGateSessionConflictError(PostgresGateSessionError):
    pass


class PostgresGateSessionNotFoundError(PostgresGateSessionError):
    pass


class PostgresGateSessionPersistenceError(PostgresGateSessionError):
    pass


@dataclass(frozen=True)
class PostgresGateSessionCreateResult:
    session: GateSession
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


@lru_cache(maxsize=1)
def _expected_function_bodies() -> dict[str, str]:
    try:
        source = read_packaged_resource(
            "schemas/postgres-v3-gate-session.sql"
        ).decode("utf-8")
    except (PackagedResourceError, UnicodeError) as error:
        raise PostgresGateSessionSchemaError(
            "TBM_POSTGRES_GATE_SESSION_SCHEMA",
            "could not read canonical PostgreSQL GateSession schema",
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresGateSessionSchemaError(
            "TBM_POSTGRES_GATE_SESSION_SCHEMA",
            "canonical PostgreSQL GateSession functions are incomplete",
        )
    return bodies


class PostgresGateSessionRepository:
    """Append-only GateSession revisions in an isolated PostgreSQL schema."""

    def __init__(
        self,
        connection: object,
        *,
        owns_connection: bool = False,
        allow_direct_completion: bool = True,
    ) -> None:
        if connection is None:
            raise ValueError("connection is required")
        if type(allow_direct_completion) is not bool:
            raise ValueError("allow_direct_completion must be a boolean")
        self._connection = connection
        self._owns_connection = owns_connection
        self._allow_direct_completion = allow_direct_completion
        self._closed = False
        self._lock = RLock()
        self._event_first = False
        self._event_context: ContextVar[EventTrustedContext | None] = ContextVar(
            f"tbm_postgres_gate_session_event_context_{id(self)}",
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
            raise PostgresGateSessionConflictError(
                "TBM_POSTGRES_GATE_SESSION_EVENT_FIRST_STATE",
                "event-first mode cannot be enabled during a transaction",
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

    @contextmanager
    def bind_recovery_event_context(
        self,
        session: GateSession,
    ) -> Iterator[None]:
        if type(session) is not GateSession:
            raise ValueError("session must be exactly GateSession")
        if not self._event_first:
            yield
            return
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        with self._lock:
            self._require_open()
            try:
                with self._connection.transaction():
                    with self._connection.cursor(row_factory=dict_row) as cursor:
                        cursor.execute(
                            """
                            SELECT canonical_event
                            FROM trace_backed_memory_v3_event_ledger.events
                            WHERE stream_id = %s AND stream_version = %s
                            """,
                            (session.session_id, session.version),
                        )
                        rows = cursor.fetchall()
                if (
                    len(rows) != 1
                    or not isinstance(rows[0], Mapping)
                    or type(rows[0].get("canonical_event")) is not str
                ):
                    raise PostgresGateSessionPersistenceError(
                        "TBM_POSTGRES_GATE_SESSION_EVENT_HISTORY_INVALID",
                        "GateSession recovery event head is missing or ambiguous",
                    )
                head = loads_canonical_event(rows[0]["canonical_event"])
            except PostgresGateSessionError:
                raise
            except Exception as error:
                raise PostgresGateSessionPersistenceError(
                    "TBM_POSTGRES_GATE_SESSION_EVENT_HISTORY_INVALID",
                    "GateSession recovery event head is invalid",
                ) from error
            if (
                head.stream_id != session.session_id
                or head.stream_version != session.version
                or head.tenant_id != session.tenant_id
                or head.repository_id != session.repository_id
                or head.principal_id != session.principal_id
                or head.agent_client_id != session.agent_client_id
            ):
                raise PostgresGateSessionPersistenceError(
                    "TBM_POSTGRES_GATE_SESSION_EVENT_HISTORY_INVALID",
                    "GateSession recovery event head does not match the session",
                )
            trusted_context = EventTrustedContext(
                organization_id=head.organization_id,
                tenant_id=session.tenant_id,
                repository_id=session.repository_id,
                environment_id=head.environment_id,
                principal_id=session.principal_id,
                agent_client_id=session.agent_client_id,
                actor_type="worker",
                actor_id="worker_local_gate_recovery",
                authorization_decision_id="local_owner_recovery_authority",
            )
            with self.bind_event_context(trusted_context):
                yield

    def _event_access(self, session: GateSession | None = None) -> LedgerAccessContext:
        trusted_context = self._event_context.get()
        if trusted_context is None:
            raise PostgresGateSessionConflictError(
                "TBM_POSTGRES_GATE_SESSION_EVENT_CONTEXT_REQUIRED",
                "event-first GateSession mutation requires trusted event context",
            )
        if session is not None:
            for session_name, context_name in (
                ("tenant_id", "tenant_id"),
                ("repository_id", "repository_id"),
                ("principal_id", "principal_id"),
                ("agent_client_id", "agent_client_id"),
            ):
                if getattr(session, session_name) != getattr(
                    trusted_context,
                    context_name,
                ):
                    raise PostgresGateSessionConflictError(
                        "TBM_POSTGRES_GATE_SESSION_EVENT_CONTEXT_INVALID",
                        "trusted event context does not match GateSession identity",
                    )
        return LedgerAccessContext(
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

    def _prepare_event_first_write(self, cursor: object) -> None:
        if not self._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        ledger = PostgresEventLedgerV1(
            self._connection,
            self._event_access(),
        )
        try:
            ledger._lock_schema(cursor, write=True)
            ledger._select_global_position(cursor, for_update=True)
        finally:
            ledger.close()

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresGateSessionRepository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "failed to connect to PostgreSQL",
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_CLOSED",
                "PostgreSQL GateSession repository is closed",
            )

    @staticmethod
    def _deadline(now: str, seconds: int, *, maximum: int) -> str:
        if type(seconds) is not int or seconds < 1 or seconds > maximum:
            raise ValueError(
                f"seconds must be an integer from 1 through {maximum}"
            )
        return aware_datetime_to_rfc3339(
            parse_rfc3339(now) + timedelta(seconds=seconds)
        )

    @staticmethod
    def _timestamp_from_database(value: object) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL returned an invalid trusted timestamp",
            )
        return aware_datetime_to_rfc3339(value)

    @classmethod
    def _database_now(cls, cursor: object, *, previous: str | None = None) -> str:
        cursor.execute(
            "SELECT pg_catalog.clock_timestamp() AS trusted_now"
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or "trusted_now" not in rows[0]
        ):
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL trusted clock returned an invalid result",
            )
        now = cls._timestamp_from_database(rows[0]["trusted_now"])
        if previous is None:
            return now
        parsed_now = parse_rfc3339(now)
        parsed_previous = parse_rfc3339(previous)
        if parsed_now < parsed_previous:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_CLOCK",
                "PostgreSQL trusted clock moved backwards",
            )
        if parsed_now == parsed_previous:
            return aware_datetime_to_rfc3339(
                parsed_previous + timedelta(microseconds=1)
            )
        return now

    @staticmethod
    def _catalog_names(
        cursor: object,
        query: str,
    ) -> frozenset[str]:
        cursor.execute(query, (_SCHEMA,))
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping) or type(row.get("name")) is not str
            for row in rows
        ):
            raise PostgresGateSessionSchemaError(
                "TBM_POSTGRES_GATE_SESSION_SCHEMA",
                "PostgreSQL GateSession catalog has an invalid shape",
            )
        return frozenset(row["name"] for row in rows)

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            """
            SELECT active.schema_version AS active_schema_version,
                   gate.schema_version AS gate_schema_version,
                   gate.contract_version AS contract_version
            FROM public.trace_backed_memory_schema AS active
            CROSS JOIN
                trace_backed_memory_v3_gate_session.schema_metadata AS gate
            WHERE active.singleton AND gate.singleton
            FOR SHARE OF active, gate
            """
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresGateSessionSchemaError(
                "TBM_POSTGRES_GATE_SESSION_SCHEMA",
                "PostgreSQL GateSession metadata must contain one row",
            )
        if (
            rows[0].get("active_schema_version") != 2
            or rows[0].get("gate_schema_version")
            != POSTGRES_GATE_SESSION_SCHEMA_VERSION
            or rows[0].get("contract_version")
            != GATE_SESSION_CONTRACT_VERSION
        ):
            raise PostgresGateSessionSchemaError(
                "TBM_POSTGRES_GATE_SESSION_SCHEMA",
                "PostgreSQL GateSession schema metadata mismatch",
            )

        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_gate_session.schema_metadata, "
            "trace_backed_memory_v3_gate_session.gate_session_heads, "
            "trace_backed_memory_v3_gate_session.gate_session_revisions "
            "IN ROW SHARE MODE"
        )

        relations = self._catalog_names(
            cursor,
            """
            SELECT class.relname AS name
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s
              AND class.relkind IN ('r', 'i', 'p')
            """,
        )
        if relations != _EXPECTED_RELATIONS:
            self._schema_drift()

        functions = self._catalog_names(
            cursor,
            """
            SELECT procedure.proname AS name
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = %s
            """,
        )
        if functions != _EXPECTED_FUNCTIONS:
            self._schema_drift()

        triggers = self._catalog_names(
            cursor,
            """
            SELECT trigger.tgname AS name
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS class
              ON class.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s
              AND NOT trigger.tgisinternal
            """,
        )
        if triggers != _EXPECTED_TRIGGERS:
            self._schema_drift()

        cursor.execute(
            """
            SELECT trigger.tgname,
                   relation.relname AS table_name,
                   procedure.proname AS function_name,
                   function_namespace.nspname AS function_schema,
                   trigger.tgtype
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_proc AS procedure
              ON procedure.oid = trigger.tgfoid
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = procedure.pronamespace
            WHERE relation_namespace.nspname = %s
              AND NOT trigger.tgisinternal
            """,
            (_SCHEMA,),
        )
        trigger_rows = cursor.fetchall()
        try:
            trigger_shapes = frozenset(
                (
                    row["tgname"],
                    row["table_name"],
                    row["function_name"],
                    row["tgtype"],
                )
                for row in trigger_rows
                if row["function_schema"] == _SCHEMA
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if (
            len(trigger_shapes) != len(trigger_rows)
            or trigger_shapes != _EXPECTED_TRIGGER_SHAPES
        ):
            self._schema_drift()

        constraints = self._catalog_names(
            cursor,
            """
            SELECT constraint_record.conname AS name
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = constraint_record.connamespace
            WHERE namespace.nspname = %s
              AND constraint_record.contype <> 'n'
            """,
        )
        if constraints != _EXPECTED_CONSTRAINTS:
            missing = sorted(_EXPECTED_CONSTRAINTS - constraints)
            unexpected = sorted(constraints - _EXPECTED_CONSTRAINTS)
            detail = (
                f"constraint missing={missing[:1]} "
                f"unexpected={unexpected[:1]}"
            )
            self._schema_drift(detail)

        cursor.execute(
            """
            SELECT table_name,
                   column_name,
                   data_type,
                   is_nullable,
                   collation_name
            FROM information_schema.columns
            WHERE table_schema = %s
            """,
            (_SCHEMA,),
        )
        column_rows = cursor.fetchall()
        try:
            columns = frozenset(
                (
                    row["table_name"],
                    row["column_name"],
                    row["data_type"],
                    row["is_nullable"],
                    row["collation_name"],
                )
                for row in column_rows
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if columns != _EXPECTED_COLUMNS:
            self._schema_drift()

        cursor.execute(
            """
            SELECT procedure.proname,
                   procedure.proconfig,
                   procedure.prosrc,
                   language.lanname,
                   pg_catalog.pg_get_function_result(procedure.oid)
                       AS result_type
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            JOIN pg_catalog.pg_language AS language
              ON language.oid = procedure.prolang
            WHERE namespace.nspname = %s
            ORDER BY procedure.proname
            """,
            (_SCHEMA,),
        )
        function_rows = cursor.fetchall()
        if len(function_rows) != len(_EXPECTED_FUNCTIONS):
            self._schema_drift()
        expected_bodies = _expected_function_bodies()
        for row in function_rows:
            if (
                not isinstance(row, Mapping)
                or row.get("proname") not in _EXPECTED_FUNCTIONS
                or row.get("proconfig") != ["search_path=pg_catalog"]
                or row.get("lanname") != "plpgsql"
                or row.get("result_type") != "trigger"
                or type(row.get("prosrc")) is not str
                or row["prosrc"].replace("\r\n", "\n").strip()
                != expected_bodies[row["proname"]]
            ):
                self._schema_drift()

        cursor.execute(
            """
            SELECT
                ARRAY(
                    SELECT pg_catalog.pg_get_indexdef(index_record.indexrelid,
                                                       ordinal,
                                                       true)
                    FROM pg_catalog.generate_series(
                        1,
                        index_record.indnkeyatts
                    ) AS ordinal
                    ORDER BY ordinal
                ) AS columns,
                index_record.indpred IS NOT NULL AS partial
            FROM pg_catalog.pg_index AS index_record
            JOIN pg_catalog.pg_class AS index_class
              ON index_class.oid = index_record.indexrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = index_class.relnamespace
            WHERE namespace.nspname = %s
              AND index_class.relname = 'gate_session_revisions_due'
            """,
            (_SCHEMA,),
        )
        due_rows = cursor.fetchall()
        if (
            len(due_rows) != 1
            or due_rows[0].get("columns")
            != ["status", "expires_at", "lease_expires_at", "session_id"]
            or due_rows[0].get("partial") is not True
        ):
            self._schema_drift()

    @staticmethod
    def _schema_drift(detail: str | None = None) -> NoReturn:
        raise PostgresGateSessionSchemaError(
            "TBM_POSTGRES_GATE_SESSION_SCHEMA",
            "PostgreSQL GateSession schema definitions do not match"
            + (f": {detail}" if detail else ""),
        )

    @staticmethod
    def _revision_values(session: GateSession) -> tuple[object, ...]:
        payload = dumps_gate_session(session)
        if len(payload.encode("utf-8")) > GATE_SESSION_MAX_BYTES:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "GateSession payload exceeds the storage limit",
            )
        return (
            session.session_id,
            session.version,
            session.status,
            canonical_rfc3339(session.updated_at),
            canonical_rfc3339(session.expires_at),
            (
                canonical_rfc3339(session.lease_expires_at)
                if session.lease_expires_at is not None
                else None
            ),
            payload,
        )

    @staticmethod
    def _timestamp_column(row: Mapping[str, object], name: str) -> str | None:
        value = row[name]
        if value is None:
            return None
        return PostgresGateSessionRepository._timestamp_from_database(value)

    @classmethod
    def _session_from_row(cls, row: Mapping[str, object]) -> GateSession:
        payload = row.get("payload")
        if type(payload) is not str:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL GateSession revision row has an invalid shape",
            )
        try:
            session = loads_gate_session(payload)
            stored_values = (
                row["session_id"],
                row["version"],
                row["status"],
                cls._timestamp_column(row, "updated_at"),
                cls._timestamp_column(row, "expires_at"),
                cls._timestamp_column(row, "lease_expires_at"),
                payload,
            )
        except (GateSessionContractError, KeyError) as error:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "stored GateSession payload failed contract validation",
            ) from error
        if stored_values != cls._revision_values(session):
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL GateSession columns do not match payload",
            )
        expected_identity = (
            session.tenant_id,
            session.repository_id,
            session.principal_id,
            session.agent_client_id,
            session.trace_id,
            session.run_id,
            session.request_fingerprint,
            session.idempotency_key,
        )
        try:
            stored_identity = tuple(
                row[name]
                for name in (
                    "tenant_id",
                    "repository_id",
                    "principal_id",
                    "agent_client_id",
                    "trace_id",
                    "run_id",
                    "request_fingerprint",
                    "idempotency_key",
                )
            )
        except KeyError as error:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL GateSession head row has an invalid shape",
            ) from error
        if stored_identity != expected_identity:
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL GateSession head identity does not match payload",
            )
        return session

    @staticmethod
    def _same_idempotent_request(
        existing: GateSession,
        proposed: GateSession,
    ) -> bool:
        return all(
            getattr(existing, field_name) == getattr(proposed, field_name)
            for field_name in (
                "tenant_id",
                "repository_id",
                "principal_id",
                "agent_client_id",
                "trace_id",
                "run_id",
                "request_fingerprint",
                "idempotency_key",
            )
        )

    @classmethod
    def _insert_revision(cls, cursor: object, session: GateSession) -> None:
        cursor.execute(
            """
            INSERT INTO
                trace_backed_memory_v3_gate_session.gate_session_revisions (
                    session_id,
                    version,
                    status,
                    updated_at,
                    expires_at,
                    lease_expires_at,
                    payload
                )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            cls._revision_values(session),
        )

    @classmethod
    def _select_current(
        cls,
        cursor: object,
        session_id: str,
        *,
        for_update: bool,
    ) -> GateSession:
        if for_update:
            cursor.execute(
                """
                SELECT session_id
                FROM trace_backed_memory_v3_gate_session.gate_session_heads
                WHERE session_id = %s
                FOR UPDATE
                """,
                (session_id,),
            )
            if cursor.fetchone() is None:
                raise PostgresGateSessionNotFoundError(
                    "TBM_POSTGRES_GATE_SESSION_NOT_FOUND",
                    "GateSession was not found",
                )
        cursor.execute(
            """
            SELECT revision.session_id,
                   revision.version,
                   revision.status,
                   revision.updated_at,
                   revision.expires_at,
                   revision.lease_expires_at,
                   revision.payload,
                   head.tenant_id,
                   head.repository_id,
                   head.principal_id,
                   head.agent_client_id,
                   head.trace_id,
                   head.run_id,
                   head.request_fingerprint,
                   head.idempotency_key
            FROM trace_backed_memory_v3_gate_session.gate_session_heads
                    AS head
            JOIN trace_backed_memory_v3_gate_session.gate_session_revisions
                    AS revision
              ON revision.session_id = head.session_id
             AND revision.version = head.current_version
            WHERE head.session_id = %s
            """,
            (session_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresGateSessionNotFoundError(
                "TBM_POSTGRES_GATE_SESSION_NOT_FOUND",
                "GateSession was not found",
            )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresGateSessionPersistenceError(
                "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                "PostgreSQL GateSession current row has an invalid shape",
            )
        return cls._session_from_row(rows[0])

    def _append_revision(
        self,
        cursor: object,
        current: GateSession,
        next_session: GateSession,
        expected_version: int,
    ) -> None:
        self._append_revision_event(
            cursor,
            previous_session=current,
            next_session=next_session,
        )
        self._insert_revision(cursor, next_session)
        cursor.execute(
            """
            UPDATE trace_backed_memory_v3_gate_session.gate_session_heads
            SET current_version = %s
            WHERE session_id = %s AND current_version = %s
            """,
            (
                next_session.version,
                current.session_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise GateSessionContractError(
                "TBM_GATE_SESSION_STALE_VERSION",
                "expected_version does not match the current session revision",
            )

    def _append_revision_event(
        self,
        cursor: object,
        *,
        previous_session: GateSession | None,
        next_session: GateSession,
    ) -> None:
        if not self._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        access = self._event_access(next_session)
        trusted_context = access.event_trusted_context()
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            ledger._lock_schema(cursor, write=True)
            next_global_position = ledger._select_global_position(
                cursor,
                for_update=True,
            ) + 1
            parent_event = ledger._select_head_event(
                cursor,
                next_session.session_id,
                for_update=False,
            )
            event = build_gate_session_event(
                next_session,
                previous_session=previous_session,
                parent_event=parent_event,
                global_position=next_global_position,
                trusted_context=trusted_context,
            )
            idempotency = LedgerIdempotency(
                event.idempotency_key_sha256,
                event.request_sha256,
            )
            request = LedgerAppendRequest(
                access=access,
                stream_id=next_session.session_id,
                expected_stream_version=(
                    0 if previous_session is None else previous_session.version
                ),
                events=(event,),
                idempotency=idempotency,
            )
            ledger._append_in_transaction(cursor, request)
        finally:
            ledger.close()

    @staticmethod
    def _require_live_transition(
        current: GateSession,
        now: str,
        target_status: GateSessionStatus,
    ) -> None:
        parsed_now = parse_rfc3339(now)
        if target_status == "expired":
            return
        if parsed_now >= parse_rfc3339(current.expires_at):
            raise PostgresGateSessionConflictError(
                "TBM_POSTGRES_GATE_SESSION_EXPIRED",
                "GateSession expiry has passed",
            )
        if (
            current.lease_expires_at is not None
            and parsed_now >= parse_rfc3339(current.lease_expires_at)
        ):
            raise PostgresGateSessionConflictError(
                "TBM_POSTGRES_GATE_SESSION_LEASE_EXPIRED",
                "GateSession lease has expired",
            )

    @_synchronized
    def create_or_get(
        self,
        *,
        session_id: str,
        tenant_id: str,
        repository_id: str,
        principal_id: str,
        agent_client_id: str,
        trace_id: str,
        run_id: str,
        request_fingerprint: str,
        idempotency_key: str,
        expires_in_seconds: int,
    ) -> PostgresGateSessionCreateResult:
        self._require_open()
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._prepare_event_first_write(cursor)
                    self._lock_schema(cursor)
                    now = self._database_now(cursor)
                    expires_at = self._deadline(
                        now,
                        expires_in_seconds,
                        maximum=GATE_SESSION_MAX_TTL_SECONDS,
                    )
                    proposed = create_gate_session(
                        session_id=session_id,
                        tenant_id=tenant_id,
                        repository_id=repository_id,
                        principal_id=principal_id,
                        agent_client_id=agent_client_id,
                        trace_id=trace_id,
                        run_id=run_id,
                        request_fingerprint=request_fingerprint,
                        idempotency_key=idempotency_key,
                        created_at=now,
                        expires_at=expires_at,
                    )
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_gate_session
                                .gate_session_heads (
                                    session_id,
                                    tenant_id,
                                    repository_id,
                                    principal_id,
                                    agent_client_id,
                                    trace_id,
                                    run_id,
                                    request_fingerprint,
                                    idempotency_key,
                                    current_version
                                )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                        ON CONFLICT DO NOTHING
                        RETURNING session_id
                        """,
                        (
                            session_id,
                            tenant_id,
                            repository_id,
                            principal_id,
                            agent_client_id,
                            trace_id,
                            run_id,
                            request_fingerprint,
                            idempotency_key,
                        ),
                    )
                    inserted = cursor.fetchone()
                    if inserted is not None:
                        self._append_revision_event(
                            cursor,
                            previous_session=None,
                            next_session=proposed,
                        )
                        self._insert_revision(cursor, proposed)
                        return PostgresGateSessionCreateResult(
                            session=proposed,
                            inserted=True,
                        )

                    cursor.execute(
                        """
                        SELECT session_id
                        FROM trace_backed_memory_v3_gate_session
                                .gate_session_heads
                        WHERE tenant_id = %s
                          AND repository_id = %s
                          AND principal_id = %s
                          AND agent_client_id = %s
                          AND idempotency_key = %s
                        FOR UPDATE
                        """,
                        (
                            tenant_id,
                            repository_id,
                            principal_id,
                            agent_client_id,
                            idempotency_key,
                        ),
                    )
                    idempotent_rows = cursor.fetchall()
                    if idempotent_rows:
                        existing = self._select_current(
                            cursor,
                            idempotent_rows[0]["session_id"],
                            for_update=False,
                        )
                        if not self._same_idempotent_request(
                            existing,
                            proposed,
                        ):
                            raise PostgresGateSessionConflictError(
                                "TBM_POSTGRES_GATE_SESSION_IDEMPOTENCY_CONFLICT",
                                "idempotency key is bound to another request",
                            )
                        return PostgresGateSessionCreateResult(
                            session=existing,
                            inserted=False,
                        )
                    cursor.execute(
                        """
                        SELECT 1
                        FROM trace_backed_memory_v3_gate_session
                                .gate_session_heads
                        WHERE session_id = %s
                        FOR UPDATE
                        """,
                        (session_id,),
                    )
                    if cursor.fetchone() is not None:
                        raise PostgresGateSessionConflictError(
                            "TBM_POSTGRES_GATE_SESSION_ID_CONFLICT",
                            "session_id is already bound to another request",
                        )
                    raise PostgresGateSessionPersistenceError(
                        "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                        "concurrent GateSession insert could not be resolved",
                    )
        except (
            GateSessionContractError,
            PostgresGateSessionConflictError,
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to create PostgreSQL GateSession",
            )

    @_synchronized
    def get(self, session_id: str) -> GateSession:
        self._require_open()
        if type(session_id) is not str:
            raise ValueError("session_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor)
                    return self._select_current(
                        cursor,
                        session_id,
                        for_update=False,
                    )
        except (
            PostgresGateSessionNotFoundError,
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load PostgreSQL GateSession",
            )

    @_synchronized
    def find_by_idempotency(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        principal_id: str,
        agent_client_id: str,
        idempotency_key: str,
    ) -> GateSession | None:
        self._require_open()
        values = (
            tenant_id,
            repository_id,
            principal_id,
            agent_client_id,
            idempotency_key,
        )
        if any(type(value) is not str or not value for value in values):
            raise ValueError("idempotency lookup values must be nonblank strings")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        """
                        SELECT session_id
                        FROM trace_backed_memory_v3_gate_session
                                .gate_session_heads
                        WHERE tenant_id = %s
                          AND repository_id = %s
                          AND principal_id = %s
                          AND agent_client_id = %s
                          AND idempotency_key = %s
                        """,
                        values,
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        return None
                    if len(rows) != 1 or not isinstance(rows[0], Mapping):
                        raise PostgresGateSessionPersistenceError(
                            "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
                            "PostgreSQL idempotency lookup has an invalid shape",
                        )
                    return self._select_current(
                        cursor,
                        rows[0]["session_id"],
                        for_update=False,
                    )
        except (
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to find PostgreSQL GateSession",
            )

    @_synchronized
    def history(self, session_id: str) -> tuple[GateSession, ...]:
        self._require_open()
        if type(session_id) is not str:
            raise ValueError("session_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        """
                        SELECT revision.session_id,
                               revision.version,
                               revision.status,
                               revision.updated_at,
                               revision.expires_at,
                               revision.lease_expires_at,
                               revision.payload,
                               head.tenant_id,
                               head.repository_id,
                               head.principal_id,
                               head.agent_client_id,
                               head.trace_id,
                               head.run_id,
                               head.request_fingerprint,
                               head.idempotency_key
                        FROM trace_backed_memory_v3_gate_session
                                .gate_session_revisions AS revision
                        JOIN trace_backed_memory_v3_gate_session
                                .gate_session_heads AS head
                          ON head.session_id = revision.session_id
                        WHERE revision.session_id = %s
                        ORDER BY revision.version
                        """,
                        (session_id,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise PostgresGateSessionNotFoundError(
                            "TBM_POSTGRES_GATE_SESSION_NOT_FOUND",
                            "GateSession was not found",
                        )
                    return tuple(self._session_from_row(row) for row in rows)
        except (
            PostgresGateSessionNotFoundError,
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load PostgreSQL GateSession history",
            )

    @_synchronized
    def transition(
        self,
        session_id: str,
        target_status: GateSessionStatus,
        *,
        expected_version: int,
        lease_seconds: int | None = None,
        retrieval_snapshot_id: str | None = None,
        system_gate_evaluation_id: str | None = None,
        semantic_gate_attempt_ids: tuple[str, ...] | None = None,
        decision_id: str | None = None,
        final_memory_revision_ids: tuple[str, ...] | None = None,
        injection_artifact_id: str | None = None,
        usage_decision_id: str | None = None,
        run_outcome_id: str | None = None,
        terminal_reason: str | None = None,
    ) -> GateSession:
        self._require_open()
        if target_status == "completed" and not self._allow_direct_completion:
            raise PostgresGateSessionConflictError(
                "TBM_POSTGRES_GATE_SESSION_COMPLETION_AUTHORITY",
                "GateSession completion requires the RunOutcome authority",
            )
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._prepare_event_first_write(cursor)
                    self._lock_schema(cursor)
                    current = self._select_current(
                        cursor,
                        session_id,
                        for_update=True,
                    )
                    now = self._database_now(
                        cursor,
                        previous=current.updated_at,
                    )
                    self._require_live_transition(
                        current,
                        now,
                        target_status,
                    )
                    lease_expires_at = None
                    if lease_seconds is not None:
                        lease_expires_at = self._deadline(
                            now,
                            lease_seconds,
                            maximum=GATE_SESSION_MAX_LEASE_SECONDS,
                        )
                    next_session = transition_gate_session(
                        current,
                        target_status,
                        expected_version=expected_version,
                        updated_at=now,
                        lease_expires_at=lease_expires_at,
                        retrieval_snapshot_id=retrieval_snapshot_id,
                        system_gate_evaluation_id=system_gate_evaluation_id,
                        semantic_gate_attempt_ids=semantic_gate_attempt_ids,
                        decision_id=decision_id,
                        final_memory_revision_ids=final_memory_revision_ids,
                        injection_artifact_id=injection_artifact_id,
                        usage_decision_id=usage_decision_id,
                        run_outcome_id=run_outcome_id,
                        terminal_reason=terminal_reason,
                    )
                    self._append_revision(
                        cursor,
                        current,
                        next_session,
                        expected_version,
                    )
                    return next_session
        except (
            GateSessionContractError,
            PostgresGateSessionConflictError,
            PostgresGateSessionNotFoundError,
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to transition PostgreSQL GateSession",
            )

    @_synchronized
    def renew_lease(
        self,
        session_id: str,
        *,
        expected_version: int,
        lease_seconds: int,
    ) -> GateSession:
        self._require_open()
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._prepare_event_first_write(cursor)
                    self._lock_schema(cursor)
                    current = self._select_current(
                        cursor,
                        session_id,
                        for_update=True,
                    )
                    now = self._database_now(
                        cursor,
                        previous=current.updated_at,
                    )
                    if (
                        current.lease_expires_at is not None
                        and parse_rfc3339(now)
                        >= parse_rfc3339(current.lease_expires_at)
                    ):
                        raise PostgresGateSessionConflictError(
                            "TBM_POSTGRES_GATE_SESSION_LEASE_EXPIRED",
                            "GateSession lease has expired",
                        )
                    lease_expires_at = self._deadline(
                        now,
                        lease_seconds,
                        maximum=GATE_SESSION_MAX_LEASE_SECONDS,
                    )
                    if parse_rfc3339(lease_expires_at) > parse_rfc3339(
                        current.expires_at
                    ):
                        lease_expires_at = current.expires_at
                    next_session = renew_gate_session_lease(
                        current,
                        expected_version=expected_version,
                        updated_at=now,
                        lease_expires_at=lease_expires_at,
                    )
                    self._append_revision(
                        cursor,
                        current,
                        next_session,
                        expected_version,
                    )
                    return next_session
        except (
            GateSessionContractError,
            PostgresGateSessionConflictError,
            PostgresGateSessionNotFoundError,
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to renew PostgreSQL GateSession lease",
            )

    @_synchronized
    def list_due(self, *, limit: int = 100) -> tuple[GateSession, ...]:
        self._require_open()
        if type(limit) is not int or limit < 1 or limit > 10_000:
            raise ValueError("limit must be an integer from 1 through 10000")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor)
                    now = self._database_now(cursor)
                    cursor.execute(
                        """
                        SELECT revision.session_id,
                               revision.version,
                               revision.status,
                               revision.updated_at,
                               revision.expires_at,
                               revision.lease_expires_at,
                               revision.payload,
                               head.tenant_id,
                               head.repository_id,
                               head.principal_id,
                               head.agent_client_id,
                               head.trace_id,
                               head.run_id,
                               head.request_fingerprint,
                               head.idempotency_key
                        FROM trace_backed_memory_v3_gate_session
                                .gate_session_heads AS head
                        JOIN trace_backed_memory_v3_gate_session
                                .gate_session_revisions AS revision
                          ON revision.session_id = head.session_id
                         AND revision.version = head.current_version
                        WHERE revision.status IN (
                            'prepared',
                            'awaiting_decision',
                            'decided',
                            'finalized',
                            'executing'
                        )
                          AND (
                              revision.expires_at <= %s
                              OR revision.lease_expires_at <= %s
                          )
                        ORDER BY revision.expires_at,
                                 revision.lease_expires_at,
                                 revision.session_id
                        LIMIT %s
                        """,
                        (now, now, limit),
                    )
                    return tuple(
                        self._session_from_row(row)
                        for row in cursor.fetchall()
                    )
        except (
            PostgresGateSessionPersistenceError,
            PostgresGateSessionSchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to list due PostgreSQL GateSessions",
            )

    @staticmethod
    def _raise_database_error(
        error: BaseException,
        message: str,
    ) -> NoReturn:
        if getattr(error, "sqlstate", None) in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresGateSessionSchemaError(
                "TBM_POSTGRES_GATE_SESSION_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise PostgresGateSessionPersistenceError(
            "TBM_POSTGRES_GATE_SESSION_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresGateSessionRepository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
