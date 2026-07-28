from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from .completion_outbox_v3 import (
    CompletionOutboxContractError,
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    _validate_completion_outbox_claim,
    acknowledge_completion_outbox_delivery,
    build_completion_outbox_event,
    build_initial_completion_outbox_delivery,
    claim_completion_outbox_delivery,
    dumps_completion_outbox_delivery,
    dumps_completion_outbox_event,
    fail_completion_outbox_delivery,
    loads_completion_outbox_delivery,
    loads_completion_outbox_event,
    verify_completion_outbox_delivery_transition,
    verify_completion_outbox_event,
)
from .contracts_v3 import V3ContractError
from .gate_completion_v3 import GateCompletionRequest, GateCompletionResult
from .postgres import _load_psycopg
from .postgres_gate_session_v3 import (
    PostgresGateSessionPersistenceError,
    PostgresGateSessionRepository,
)
from .postgres_outcome_v3 import (
    PostgresOutcomeV3ConflictError,
    PostgresOutcomeV3NotFoundError,
    PostgresOutcomeV3PersistenceError,
    PostgresOutcomeV3Repository,
    PostgresOutcomeV3SchemaError,
)
from .postgres_outcome_attribution_v3 import _CATALOG_SHA256_QUERY
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_COMPLETION_OUTBOX_V3_SCHEMA_VERSION = 1
POSTGRES_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE = 1000
POSTGRES_COMPLETION_OUTBOX_V3_CONTRACT_VERSION = (
    "tbm.completion-outbox.v3"
)
_SCHEMA = "trace_backed_memory_v3_completion_outbox"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL completion outbox v3 schema is missing or incomplete"
)
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_EXPECTED_RELATIONS = frozenset(
    {
        "completion_outbox_due",
        "delivery_heads",
        "delivery_heads_pkey",
        "delivery_revisions",
        "delivery_revisions_delivery_revision_id_key",
        "delivery_revisions_pkey",
        "events",
        "events_pkey",
        "events_run_outcome_id_key",
        "schema_metadata",
        "schema_metadata_pkey",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {
        "reject_immutable_change",
        "validate_delivery_head_consistency",
        "validate_delivery_insert",
        "validate_event_insert",
        "validate_head_insert",
        "validate_head_update",
    }
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "completion_outbox_delivery_immutable",
        "completion_outbox_delivery_head_consistency",
        "completion_outbox_delivery_no_truncate",
        "completion_outbox_delivery_validate_insert",
        "completion_outbox_events_immutable",
        "completion_outbox_events_no_truncate",
        "completion_outbox_events_validate_insert",
        "completion_outbox_heads_no_delete",
        "completion_outbox_heads_no_truncate",
        "completion_outbox_heads_validate_insert",
        "completion_outbox_heads_validate_update",
        "completion_outbox_metadata_immutable",
        "completion_outbox_metadata_no_truncate",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("schema_metadata", "singleton"),
        ("schema_metadata", "schema_version"),
        ("schema_metadata", "contract_version"),
        ("events", "event_id"),
        ("events", "event_type"),
        ("events", "tenant_id"),
        ("events", "repository_id"),
        ("events", "session_id"),
        ("events", "trace_id"),
        ("events", "run_id"),
        ("events", "usage_decision_id"),
        ("events", "run_outcome_id"),
        ("events", "outcome_descriptor_sha256"),
        ("events", "occurred_at"),
        ("events", "descriptor"),
        ("delivery_revisions", "event_id"),
        ("delivery_revisions", "version"),
        ("delivery_revisions", "delivery_revision_id"),
        ("delivery_revisions", "status"),
        ("delivery_revisions", "attempt_count"),
        ("delivery_revisions", "updated_at"),
        ("delivery_revisions", "available_at"),
        ("delivery_revisions", "worker_id"),
        ("delivery_revisions", "lease_expires_at"),
        ("delivery_revisions", "delivered_at"),
        ("delivery_revisions", "last_error_code"),
        ("delivery_revisions", "response_sha256"),
        ("delivery_revisions", "descriptor"),
        ("delivery_heads", "event_id"),
        ("delivery_heads", "current_version"),
    }
)
_EXPECTED_CATALOG_SHA256 = (
    "714656d94b9e40b05bf2c60225799cf13d2dc86c6c208e50648fdee8ff02546f"
)
_FUNCTION_BODY_PATTERN = re.compile(
    r"CREATE FUNCTION\s+"
    r"trace_backed_memory_v3_completion_outbox\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresCompletionOutboxV3Error(V3ContractError):
    """Stable base failure for PostgreSQL completion outbox operations."""


class PostgresCompletionOutboxV3SchemaError(
    PostgresCompletionOutboxV3Error
):
    pass


class PostgresCompletionOutboxV3ConflictError(
    PostgresCompletionOutboxV3Error
):
    pass


class PostgresCompletionOutboxV3NotFoundError(
    PostgresCompletionOutboxV3Error
):
    pass


class PostgresCompletionOutboxV3PersistenceError(
    PostgresCompletionOutboxV3Error
):
    pass


@dataclass(frozen=True)
class PostgresCompletionOutboxWrite:
    completion: GateCompletionResult
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery
    event_inserted: bool


@dataclass(frozen=True)
class PostgresCompletionOutboxClaim:
    event: CompletionOutboxEvent
    delivery: CompletionOutboxDelivery


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
            "schemas/postgres-v3-completion-outbox.sql"
        ).decode("utf-8")
    except (PackagedResourceError, UnicodeError) as error:
        raise PostgresCompletionOutboxV3SchemaError(
            "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
            "could not read canonical PostgreSQL completion outbox schema",
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresCompletionOutboxV3SchemaError(
            "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
            "canonical PostgreSQL completion outbox functions are incomplete",
        )
    return bodies


class PostgresCompletionOutboxV3Repository:
    """Atomic completion and append-only PostgreSQL delivery outbox."""

    def __init__(
        self,
        connection: object,
        *,
        owns_connection: bool = False,
    ) -> None:
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False
        self._lock = RLock()
        self._outcomes = PostgresOutcomeV3Repository(connection)
        self._outcomes._lock = self._lock
        self._outcomes._gate_sessions._lock = self._lock

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresCompletionOutboxV3Repository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "failed to connect to PostgreSQL completion outbox storage",
            ) from error
        return cls(connection, owns_connection=True)

    @property
    def outcomes(self) -> PostgresOutcomeV3Repository:
        self._require_open()
        return self._outcomes

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_CLOSED",
                "PostgreSQL completion outbox repository is closed",
            )

    @staticmethod
    def _catalog_names(cursor: object, query: str) -> frozenset[str]:
        cursor.execute(query, (_SCHEMA,))
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping) or type(row.get("name")) is not str
            for row in rows
        ):
            raise PostgresCompletionOutboxV3SchemaError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
                "PostgreSQL completion outbox catalog has invalid shape",
            )
        return frozenset(row["name"] for row in rows)

    @staticmethod
    def _schema_drift(detail: str | None = None) -> NoReturn:
        raise PostgresCompletionOutboxV3SchemaError(
            "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
            "PostgreSQL completion outbox schema definitions do not match"
            + (f": {detail}" if detail else ""),
        )

    def _verify_schema_catalog(self, cursor: object) -> None:
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
            SELECT columns.table_name, columns.column_name
            FROM information_schema.columns AS columns
            WHERE columns.table_schema = %s
            """,
            (_SCHEMA,),
        )
        rows = cursor.fetchall()
        try:
            columns = frozenset(
                (row["table_name"], row["column_name"]) for row in rows
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
        expected_bodies = _expected_function_bodies()
        if len(function_rows) != len(expected_bodies):
            self._schema_drift()
        for row in function_rows:
            if (
                not isinstance(row, Mapping)
                or row.get("proname") not in expected_bodies
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
                (SELECT count(*)
                 FROM pg_catalog.pg_policy AS policy
                 JOIN pg_catalog.pg_class AS class
                   ON class.oid = policy.polrelid
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s) AS policy_count,
                (SELECT count(*)
                 FROM pg_catalog.pg_rewrite AS rule
                 JOIN pg_catalog.pg_class AS class
                   ON class.oid = rule.ev_class
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s
                   AND rule.rulename <> '_RETURN') AS rule_count,
                (SELECT count(*)
                 FROM pg_catalog.pg_class AS class
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s
                   AND class.relkind NOT IN ('r', 'i', 'p'))
                    AS unsupported_relation_count
            """,
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [
            {
                "policy_count": 0,
                "rule_count": 0,
                "unsupported_relation_count": 0,
            }
        ]:
            self._schema_drift()
        cursor.execute(_CATALOG_SHA256_QUERY, (_SCHEMA,) * 7)
        catalog_rows = cursor.fetchall()
        if (
            len(catalog_rows) != 1
            or not isinstance(catalog_rows[0], Mapping)
            or catalog_rows[0].get("catalog_sha256")
            != _EXPECTED_CATALOG_SHA256
        ):
            actual = (
                catalog_rows[0].get("catalog_sha256")
                if len(catalog_rows) == 1
                and isinstance(catalog_rows[0], Mapping)
                else None
            )
            self._schema_drift(f"catalog digest {actual!r}")

    def _lock_outbox_schema(
        self,
        cursor: object,
        *,
        for_write: bool,
    ) -> None:
        cursor.execute(
            """
            SELECT metadata.schema_version,
                   metadata.contract_version
            FROM trace_backed_memory_v3_completion_outbox.schema_metadata
                AS metadata
            WHERE metadata.singleton
            FOR SHARE OF metadata
            """
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresCompletionOutboxV3SchemaError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
                "PostgreSQL completion outbox metadata must contain one row",
            )
        if (
            rows[0].get("schema_version")
            != POSTGRES_COMPLETION_OUTBOX_V3_SCHEMA_VERSION
            or rows[0].get("contract_version")
            != POSTGRES_COMPLETION_OUTBOX_V3_CONTRACT_VERSION
        ):
            raise PostgresCompletionOutboxV3SchemaError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
                "PostgreSQL completion outbox metadata mismatch",
            )
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_completion_outbox.schema_metadata, "
            "trace_backed_memory_v3_completion_outbox.events, "
            "trace_backed_memory_v3_completion_outbox.delivery_revisions, "
            "trace_backed_memory_v3_completion_outbox.delivery_heads "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_schema_catalog(cursor)

    def _lock_schema(self, cursor: object, *, for_write: bool) -> None:
        self._outcomes._lock_schema(cursor, for_write=for_write)
        self._lock_outbox_schema(cursor, for_write=for_write)

    @staticmethod
    def _timestamp(value: object) -> str:
        try:
            return PostgresGateSessionRepository._timestamp_from_database(
                value
            )
        except PostgresGateSessionPersistenceError as error:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "stored PostgreSQL completion outbox timestamp is invalid",
            ) from error

    @classmethod
    def _event_from_row(cls, row: Mapping[str, object]) -> CompletionOutboxEvent:
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox event row has invalid shape",
            )
        try:
            event = loads_completion_outbox_event(descriptor)
            stored = (
                row["event_id"],
                row["event_type"],
                row["tenant_id"],
                row["repository_id"],
                row["session_id"],
                row["trace_id"],
                row["run_id"],
                row["usage_decision_id"],
                row["run_outcome_id"],
                row["outcome_descriptor_sha256"],
                cls._timestamp(row["occurred_at"]),
                descriptor,
            )
        except (KeyError, CompletionOutboxContractError) as error:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "stored PostgreSQL completion outbox event failed validation",
            ) from error
        expected = (
            event.event_id,
            event.event_type,
            event.tenant_id,
            event.repository_id,
            event.session_id,
            event.trace_id,
            event.run_id,
            event.usage_decision_id,
            event.run_outcome_id,
            event.outcome_descriptor_sha256,
            event.occurred_at,
            dumps_completion_outbox_event(event),
        )
        if stored != expected:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox event columns do not match",
            )
        return event

    @classmethod
    def _delivery_from_row(
        cls,
        row: Mapping[str, object],
    ) -> CompletionOutboxDelivery:
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox delivery row has invalid shape",
            )
        try:
            delivery = loads_completion_outbox_delivery(descriptor)
            stored = (
                row["event_id"],
                row["version"],
                row["delivery_revision_id"],
                row["status"],
                row["attempt_count"],
                cls._timestamp(row["updated_at"]),
                (
                    None
                    if row["available_at"] is None
                    else cls._timestamp(row["available_at"])
                ),
                row["worker_id"],
                (
                    None
                    if row["lease_expires_at"] is None
                    else cls._timestamp(row["lease_expires_at"])
                ),
                (
                    None
                    if row["delivered_at"] is None
                    else cls._timestamp(row["delivered_at"])
                ),
                row["last_error_code"],
                row["response_sha256"],
                descriptor,
            )
        except (KeyError, CompletionOutboxContractError) as error:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "stored PostgreSQL completion outbox delivery failed validation",
            ) from error
        expected = (
            delivery.event_id,
            delivery.version,
            delivery.delivery_revision_id,
            delivery.status,
            delivery.attempt_count,
            delivery.updated_at,
            delivery.available_at,
            delivery.worker_id,
            delivery.lease_expires_at,
            delivery.delivered_at,
            delivery.last_error_code,
            delivery.response_sha256,
            dumps_completion_outbox_delivery(delivery),
        )
        if stored != expected:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox delivery columns do not match",
            )
        return delivery

    @staticmethod
    def _event_select() -> str:
        return (
            "SELECT event_id, event_type, tenant_id, repository_id, "
            "session_id, trace_id, run_id, usage_decision_id, "
            "run_outcome_id, outcome_descriptor_sha256, occurred_at, "
            "descriptor FROM "
            "trace_backed_memory_v3_completion_outbox.events "
        )

    @staticmethod
    def _delivery_select() -> str:
        return (
            "SELECT revision.event_id, revision.version, "
            "revision.delivery_revision_id, revision.status, "
            "revision.attempt_count, revision.updated_at, "
            "revision.available_at, revision.worker_id, "
            "revision.lease_expires_at, revision.delivered_at, "
            "revision.last_error_code, revision.response_sha256, "
            "revision.descriptor FROM "
            "trace_backed_memory_v3_completion_outbox.delivery_revisions "
            "AS revision "
        )

    @classmethod
    def _select_event(
        cls,
        cursor: object,
        event_id: str,
    ) -> CompletionOutboxEvent:
        cursor.execute(cls._event_select() + "WHERE event_id = %s", (event_id,))
        rows = cursor.fetchall()
        if not rows:
            raise PostgresCompletionOutboxV3NotFoundError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_EVENT_NOT_FOUND",
                "completion outbox event was not found",
            )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox event query has invalid shape",
            )
        return cls._event_from_row(rows[0])

    @classmethod
    def _select_event_by_outcome(
        cls,
        cursor: object,
        run_outcome_id: str,
    ) -> CompletionOutboxEvent:
        cursor.execute(
            cls._event_select() + "WHERE run_outcome_id = %s",
            (run_outcome_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresCompletionOutboxV3NotFoundError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_EVENT_NOT_FOUND",
                "completion outbox event was not found",
            )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox event query has invalid shape",
            )
        return cls._event_from_row(rows[0])

    @classmethod
    def _select_current_delivery(
        cls,
        cursor: object,
        event_id: str,
        *,
        for_update: bool = False,
    ) -> CompletionOutboxDelivery:
        suffix = " FOR UPDATE OF head"
        cursor.execute(
            cls._delivery_select()
            + "JOIN trace_backed_memory_v3_completion_outbox.delivery_heads "
            "AS head ON head.event_id = revision.event_id "
            "AND head.current_version = revision.version "
            "WHERE revision.event_id = %s"
            + (suffix if for_update else ""),
            (event_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresCompletionOutboxV3NotFoundError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_DELIVERY_NOT_FOUND",
                "completion outbox delivery head was not found",
            )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                "PostgreSQL completion outbox delivery query has invalid shape",
            )
        return cls._delivery_from_row(rows[0])

    @staticmethod
    def _event_values(event: CompletionOutboxEvent) -> tuple[object, ...]:
        return (
            event.event_id,
            event.event_type,
            event.tenant_id,
            event.repository_id,
            event.session_id,
            event.trace_id,
            event.run_id,
            event.usage_decision_id,
            event.run_outcome_id,
            event.outcome_descriptor_sha256,
            event.occurred_at,
            dumps_completion_outbox_event(event),
        )

    @staticmethod
    def _delivery_values(
        delivery: CompletionOutboxDelivery,
    ) -> tuple[object, ...]:
        return (
            delivery.event_id,
            delivery.version,
            delivery.delivery_revision_id,
            delivery.status,
            delivery.attempt_count,
            delivery.updated_at,
            delivery.available_at,
            delivery.worker_id,
            delivery.lease_expires_at,
            delivery.delivered_at,
            delivery.last_error_code,
            delivery.response_sha256,
            dumps_completion_outbox_delivery(delivery),
        )

    @classmethod
    def _insert_bundle(
        cls,
        cursor: object,
        event: CompletionOutboxEvent,
        delivery: CompletionOutboxDelivery,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_completion_outbox.events (
                event_id, event_type, tenant_id, repository_id, session_id,
                trace_id, run_id, usage_decision_id, run_outcome_id,
                outcome_descriptor_sha256, occurred_at, descriptor
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            cls._event_values(event),
        )
        cls._insert_delivery(cursor, delivery)
        cursor.execute(
            """
            INSERT INTO
                trace_backed_memory_v3_completion_outbox.delivery_heads (
                    event_id, current_version
                )
            VALUES (%s, 1)
            """,
            (event.event_id,),
        )

    @classmethod
    def _insert_delivery(
        cls,
        cursor: object,
        delivery: CompletionOutboxDelivery,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO
                trace_backed_memory_v3_completion_outbox.delivery_revisions (
                    event_id, version, delivery_revision_id, status,
                    attempt_count, updated_at, available_at, worker_id,
                    lease_expires_at, delivered_at, last_error_code,
                    response_sha256, descriptor
                )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            cls._delivery_values(delivery),
        )

    @classmethod
    def _append_delivery(
        cls,
        cursor: object,
        previous: CompletionOutboxDelivery,
        current: CompletionOutboxDelivery,
    ) -> None:
        verify_completion_outbox_delivery_transition(previous, current)
        cls._insert_delivery(cursor, current)
        cursor.execute(
            """
            UPDATE trace_backed_memory_v3_completion_outbox.delivery_heads
            SET current_version = %s
            WHERE event_id = %s AND current_version = %s
            """,
            (current.version, current.event_id, previous.version),
        )
        if cursor.rowcount != 1:
            raise PostgresCompletionOutboxV3ConflictError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_STALE",
                "completion outbox delivery version is stale",
            )

    @_synchronized
    def complete_session(
        self,
        request: GateCompletionRequest,
    ) -> PostgresCompletionOutboxWrite:
        self._require_open()
        if type(request) is not GateCompletionRequest:
            raise TypeError("request must be exactly GateCompletionRequest")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, for_write=True)
                    completion = self._outcomes.complete_session(request)
                    event = build_completion_outbox_event(
                        completion.outcome,
                        completion.session,
                    )
                    initial = build_initial_completion_outbox_delivery(event)
                    if completion.inserted:
                        self._insert_bundle(cursor, event, initial)
                        event_inserted = True
                    else:
                        try:
                            retained = self._select_event_by_outcome(
                                cursor,
                                completion.outcome.run_outcome_id,
                            )
                        except PostgresCompletionOutboxV3NotFoundError as error:
                            raise PostgresCompletionOutboxV3PersistenceError(
                                "TBM_POSTGRES_COMPLETION_OUTBOX_ORPHANED_OUTCOME",
                                "completed outcome has no outbox event",
                            ) from error
                        if retained != event:
                            raise PostgresCompletionOutboxV3ConflictError(
                                "TBM_POSTGRES_COMPLETION_OUTBOX_CONFLICT",
                                "outcome is linked to another outbox event",
                            )
                        event_inserted = False
                    retained_event = self._select_event(cursor, event.event_id)
                    retained_delivery = self._select_current_delivery(
                        cursor,
                        event.event_id,
                    )
                    if retained_event != event:
                        raise PostgresCompletionOutboxV3PersistenceError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_READBACK",
                            "completion outbox event read-back changed",
                        )
                    if event_inserted and retained_delivery != initial:
                        raise PostgresCompletionOutboxV3PersistenceError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_READBACK",
                            "initial completion outbox delivery changed",
                        )
                    verify_completion_outbox_event(
                        retained_event,
                        completion.outcome,
                        completion.session,
                    )
                    self._verify_schema_catalog(cursor)
                    return PostgresCompletionOutboxWrite(
                        completion,
                        retained_event,
                        retained_delivery,
                        event_inserted,
                    )
        except (
            CompletionOutboxContractError,
            PostgresCompletionOutboxV3ConflictError,
            PostgresCompletionOutboxV3NotFoundError,
            PostgresCompletionOutboxV3PersistenceError,
            PostgresCompletionOutboxV3SchemaError,
        ):
            raise
        except PostgresOutcomeV3SchemaError as error:
            raise PostgresCompletionOutboxV3SchemaError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
                "PostgreSQL outcome dependency failed schema validation",
            ) from error
        except PostgresOutcomeV3NotFoundError as error:
            raise PostgresCompletionOutboxV3NotFoundError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_SESSION_NOT_FOUND",
                "GateSession or RunOutcome was not found",
            ) from error
        except PostgresOutcomeV3ConflictError as error:
            raise PostgresCompletionOutboxV3ConflictError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_COMPLETION_CONFLICT",
                "GateSession completion conflicts with retained state",
            ) from error
        except PostgresOutcomeV3PersistenceError as error:
            raise PostgresCompletionOutboxV3PersistenceError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_DEPENDENCY",
                "PostgreSQL outcome dependency failed during completion",
            ) from error
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to complete GateSession with an outbox event",
            )

    @_synchronized
    def get_event(self, event_id: str) -> CompletionOutboxEvent:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_outbox_schema(cursor, for_write=False)
                    return self._select_event(cursor, event_id)
        except (
            PostgresCompletionOutboxV3NotFoundError,
            PostgresCompletionOutboxV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load completion outbox event",
            )

    @_synchronized
    def get_delivery(self, event_id: str) -> CompletionOutboxDelivery:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_outbox_schema(cursor, for_write=False)
                    return self._select_current_delivery(cursor, event_id)
        except (
            PostgresCompletionOutboxV3NotFoundError,
            PostgresCompletionOutboxV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load completion outbox delivery",
            )

    @_synchronized
    def list_delivery_history(
        self,
        event_id: str,
    ) -> tuple[CompletionOutboxDelivery, ...]:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_outbox_schema(cursor, for_write=False)
                    self._select_event(cursor, event_id)
                    cursor.execute(
                        self._delivery_select()
                        + "WHERE revision.event_id = %s "
                        "ORDER BY revision.version",
                        (event_id,),
                    )
                    rows = cursor.fetchall()
                    if any(not isinstance(row, Mapping) for row in rows):
                        raise PostgresCompletionOutboxV3PersistenceError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                            "delivery history query has invalid shape",
                        )
                    deliveries = tuple(
                        self._delivery_from_row(row) for row in rows
                    )
                    for previous, current in zip(
                        deliveries,
                        deliveries[1:],
                        strict=False,
                    ):
                        verify_completion_outbox_delivery_transition(
                            previous,
                            current,
                        )
                    return deliveries
        except (
            CompletionOutboxContractError,
            PostgresCompletionOutboxV3NotFoundError,
            PostgresCompletionOutboxV3PersistenceError,
            PostgresCompletionOutboxV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to list completion outbox delivery history",
            )

    @_synchronized
    def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int = 100,
    ) -> tuple[PostgresCompletionOutboxClaim, ...]:
        self._require_open()
        _validate_completion_outbox_claim(worker_id, lease_seconds)
        if (
            type(limit) is not int
            or not 1 <= limit <= POSTGRES_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE
        ):
            raise ValueError("limit is outside the supported range")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_outbox_schema(cursor, for_write=True)
                    now = self._outcomes._gate_sessions._database_now(cursor)
                    cursor.execute(
                        self._delivery_select()
                        + "JOIN "
                        "trace_backed_memory_v3_completion_outbox."
                        "delivery_heads AS head "
                        "ON head.event_id = revision.event_id "
                        "AND head.current_version = revision.version "
                        "WHERE (revision.status IN ('pending','retry_wait') "
                        "AND revision.available_at <= %s) "
                        "OR (revision.status = 'leased' "
                        "AND revision.lease_expires_at <= %s) "
                        "ORDER BY COALESCE("
                        "revision.available_at, revision.lease_expires_at), "
                        "revision.event_id LIMIT %s "
                        "FOR UPDATE OF head SKIP LOCKED",
                        (now, now, limit),
                    )
                    rows = cursor.fetchall()
                    if any(not isinstance(row, Mapping) for row in rows):
                        raise PostgresCompletionOutboxV3PersistenceError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
                            "due delivery query has invalid shape",
                        )
                    due = tuple(self._delivery_from_row(row) for row in rows)
                    claimed = tuple(
                        claim_completion_outbox_delivery(
                            delivery,
                            worker_id=worker_id,
                            claimed_at=now,
                            lease_seconds=lease_seconds,
                        )
                        for delivery in due
                    )
                    events = tuple(
                        self._select_event(cursor, current.event_id)
                        for current in claimed
                    )
                    for previous, current in zip(
                        due,
                        claimed,
                        strict=True,
                    ):
                        self._append_delivery(cursor, previous, current)
                    retained = tuple(
                        self._select_current_delivery(
                            cursor,
                            current.event_id,
                        )
                        for current in claimed
                    )
                    if retained != claimed:
                        raise PostgresCompletionOutboxV3PersistenceError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_READBACK",
                            "claimed delivery read-back changed",
                        )
                    self._verify_schema_catalog(cursor)
                    return tuple(
                        PostgresCompletionOutboxClaim(event, delivery)
                        for event, delivery in zip(
                            events,
                            retained,
                            strict=True,
                        )
                    )
        except (
            CompletionOutboxContractError,
            PostgresCompletionOutboxV3ConflictError,
            PostgresCompletionOutboxV3PersistenceError,
            PostgresCompletionOutboxV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to claim completion outbox deliveries",
            )

    @_synchronized
    def acknowledge(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        response_sha256: str | None = None,
    ) -> CompletionOutboxDelivery:
        return self._finish_delivery(
            event_id,
            expected_version=expected_version,
            operation=lambda current, now: (
                acknowledge_completion_outbox_delivery(
                    current,
                    worker_id=worker_id,
                    acknowledged_at=now,
                    response_sha256=response_sha256,
                )
            ),
            message="failed to acknowledge completion outbox delivery",
        )

    @_synchronized
    def fail_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> CompletionOutboxDelivery:
        return self._finish_delivery(
            event_id,
            expected_version=expected_version,
            operation=lambda current, now: fail_completion_outbox_delivery(
                current,
                worker_id=worker_id,
                failed_at=now,
                error_code=error_code,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            ),
            message="failed to record completion outbox delivery failure",
        )

    def _finish_delivery(
        self,
        event_id: str,
        *,
        expected_version: int,
        operation: Callable[
            [CompletionOutboxDelivery, str],
            CompletionOutboxDelivery,
        ],
        message: str,
    ) -> CompletionOutboxDelivery:
        self._require_open()
        if type(event_id) is not str:
            raise ValueError("event_id must be a string")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_outbox_schema(cursor, for_write=True)
                    current = self._select_current_delivery(
                        cursor,
                        event_id,
                        for_update=True,
                    )
                    if current.version != expected_version:
                        raise PostgresCompletionOutboxV3ConflictError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_STALE",
                            "completion outbox delivery version is stale",
                        )
                    now = self._outcomes._gate_sessions._database_now(
                        cursor,
                        previous=current.updated_at,
                    )
                    updated = operation(current, now)
                    self._append_delivery(cursor, current, updated)
                    retained = self._select_current_delivery(
                        cursor,
                        event_id,
                    )
                    if retained != updated:
                        raise PostgresCompletionOutboxV3PersistenceError(
                            "TBM_POSTGRES_COMPLETION_OUTBOX_READBACK",
                            "completion outbox delivery read-back changed",
                        )
                    self._verify_schema_catalog(cursor)
                    return retained
        except (
            CompletionOutboxContractError,
            PostgresCompletionOutboxV3ConflictError,
            PostgresCompletionOutboxV3NotFoundError,
            PostgresCompletionOutboxV3PersistenceError,
            PostgresCompletionOutboxV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, message)

    @staticmethod
    def _raise_database_error(error: object, message: str) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresCompletionOutboxV3SchemaError(
                "TBM_POSTGRES_COMPLETION_OUTBOX_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise PostgresCompletionOutboxV3PersistenceError(
            "TBM_POSTGRES_COMPLETION_OUTBOX_PERSISTENCE",
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

    def __enter__(self) -> PostgresCompletionOutboxV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "POSTGRES_COMPLETION_OUTBOX_V3_CONTRACT_VERSION",
    "POSTGRES_COMPLETION_OUTBOX_V3_MAX_PAGE_SIZE",
    "POSTGRES_COMPLETION_OUTBOX_V3_SCHEMA_VERSION",
    "PostgresCompletionOutboxClaim",
    "PostgresCompletionOutboxV3ConflictError",
    "PostgresCompletionOutboxV3Error",
    "PostgresCompletionOutboxV3NotFoundError",
    "PostgresCompletionOutboxV3PersistenceError",
    "PostgresCompletionOutboxV3Repository",
    "PostgresCompletionOutboxV3SchemaError",
    "PostgresCompletionOutboxWrite",
]
