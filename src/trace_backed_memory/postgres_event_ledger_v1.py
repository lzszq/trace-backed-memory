from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import lru_cache, wraps
import json
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast
from uuid import uuid4

from .event_v1 import (
    EVENT_JSON_MAX_BYTES,
    CanonicalEvent,
    EventV1ContractError,
    dumps_canonical_event,
    loads_canonical_event,
    verify_event_parent,
)
from .ledger_port_v1 import (
    EventLedgerClassificationDeniedError,
    EventLedgerConflictError,
    EventLedgerIdempotencyConflictError,
    EventLedgerInvalidRequestError,
    EventLedgerPortError,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerGlobalReadRequest,
    LedgerIdempotency,
    LedgerPage,
    LedgerStreamReadRequest,
    LedgerStreamVerification,
    LedgerSubscriptionPage,
    LedgerSubscriptionRequest,
    LedgerTenantPartition,
    LedgerVerificationIssueCode,
    build_ledger_append_receipt,
    build_ledger_page,
    verify_ledger_append_precondition,
    verify_ledger_append_receipt,
    verify_ledger_global_page,
    verify_ledger_stream_page,
    verify_ledger_stream_verification,
)
from .postgres import _load_psycopg
from .postgres_outcome_attribution_v3 import _CATALOG_SHA256_QUERY
from .projection_checkpoint import (
    PROJECTION_MAX_ACTIVATIONS_PER_LIST,
    PROJECTION_MAX_CHECKPOINTS_PER_LIST,
    ProjectionActivation,
    ProjectionCheckpoint,
    ProjectionCheckpointConflictError,
    ProjectionCheckpointError,
    ProjectionCheckpointNotFoundError,
    parse_projection_activation,
    parse_projection_checkpoint,
)
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_EVENT_LEDGER_V1_SCHEMA_VERSION = 1
POSTGRES_EVENT_LEDGER_V1_SCHEMA_RESOURCE = "schemas/postgres-v3-event-ledger.sql"
POSTGRES_EVENT_LEDGER_V1_ROLLBACK_RESOURCE = (
    "schemas/postgres-v3-event-ledger-rollback.sql"
)
_SCHEMA = "trace_backed_memory_v3_event_ledger"
_MISSING_SCHEMA_MESSAGE = "PostgreSQL event ledger schema is missing or incomplete"
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_CHECKPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXPECTED_CATALOG_SHA256 = (
    "30901979ba713bab223378aa2d95bc2233f048070dde56e54268f51376cd9314"
)
_EXPECTED_TABLES = frozenset(
    {
        "artifacts",
        "checkpoints",
        "events",
        "global_head",
        "idempotency",
        "projection_activations",
        "schema_metadata",
        "stream_heads",
    }
)
_EXPECTED_INDEXES = frozenset(
    {
        "event_ledger_artifacts_event_artifact_key",
        "event_ledger_artifacts_pkey",
        "event_ledger_checkpoints_pkey",
        "event_ledger_events_global_key",
        "event_ledger_events_partition_global",
        "event_ledger_events_partition_stream",
        "event_ledger_events_pkey",
        "event_ledger_events_sha256_key",
        "event_ledger_events_stream_version_key",
        "event_ledger_global_head_pkey",
        "event_ledger_idempotency_pkey",
        "event_ledger_idempotency_stream",
        "event_ledger_projection_activations_pkey",
        "event_ledger_stream_heads_pkey",
        "schema_metadata_pkey",
        "projection_activations_activation_sha256_key",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {
        "reject_immutable_change",
        "validate_artifact_insert",
        "validate_event_insert",
        "validate_global_head_insert",
        "validate_global_head_update",
        "validate_projection_activation_insert",
        "validate_stream_head_insert",
        "validate_stream_head_update",
    }
)
_EXPECTED_TRIGGER_FUNCTIONS = {
    "event_ledger_artifacts_immutable": "reject_immutable_change",
    "event_ledger_artifacts_no_truncate": "reject_immutable_change",
    "event_ledger_artifacts_validate_insert": "validate_artifact_insert",
    "event_ledger_checkpoints_immutable": "reject_immutable_change",
    "event_ledger_checkpoints_no_truncate": "reject_immutable_change",
    "event_ledger_events_immutable": "reject_immutable_change",
    "event_ledger_events_no_truncate": "reject_immutable_change",
    "event_ledger_events_validate_insert": "validate_event_insert",
    "event_ledger_global_head_advance": "validate_global_head_update",
    "event_ledger_global_head_initial": "validate_global_head_insert",
    "event_ledger_global_head_no_delete": "reject_immutable_change",
    "event_ledger_global_head_no_truncate": "reject_immutable_change",
    "event_ledger_idempotency_immutable": "reject_immutable_change",
    "event_ledger_idempotency_no_truncate": "reject_immutable_change",
    "event_ledger_projection_activations_immutable": "reject_immutable_change",
    "event_ledger_projection_activations_no_truncate": "reject_immutable_change",
    "event_ledger_projection_activations_validate_insert": "validate_projection_activation_insert",
    "event_ledger_schema_immutable": "reject_immutable_change",
    "event_ledger_schema_no_truncate": "reject_immutable_change",
    "event_ledger_stream_heads_advance": "validate_stream_head_update",
    "event_ledger_stream_heads_initial": "validate_stream_head_insert",
    "event_ledger_stream_heads_no_delete": "reject_immutable_change",
    "event_ledger_stream_heads_no_truncate": "reject_immutable_change",
}
_EXPECTED_COLUMNS = {
    "schema_metadata": (
        "singleton",
        "schema_version",
        "contract_version",
    ),
    "global_head": (
        "singleton",
        "current_global_position",
        "current_event_id",
        "current_event_sha256",
    ),
    "stream_heads": (
        "partition_sha256",
        "stream_id",
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "current_stream_version",
        "current_event_id",
        "current_event_sha256",
    ),
    "events": (
        "event_id",
        "event_sha256",
        "partition_sha256",
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "stream_id",
        "stream_version",
        "global_position",
        "previous_stream_event_sha256",
        "classification",
        "artifact_ref_count",
        "canonical_event",
    ),
    "artifacts": (
        "event_id",
        "ordinal",
        "artifact_id",
        "content_sha256",
        "media_type",
        "size_bytes",
        "classification",
        "retention_policy_id",
        "encryption_key_id",
        "availability",
        "descriptor",
    ),
    "idempotency": (
        "partition_sha256",
        "idempotency_key_sha256",
        "command_sha256",
        "request_sha256",
        "stream_id",
        "previous_stream_version",
        "current_stream_version",
        "first_global_position",
        "last_global_position",
        "event_sha256s_json",
        "receipt_sha256",
    ),
    "checkpoints": (
        "projection_name",
        "projection_version",
        "partition_sha256",
        "global_position",
        "state_sha256",
        "descriptor",
    ),
    "projection_activations": (
        "projection_name",
        "partition_sha256",
        "head_version",
        "target_build_id",
        "previous_build_id",
        "operation",
        "activation_sha256",
        "descriptor",
    ),
}
_INTEGER_COLUMNS = frozenset(
    {
        ("schema_metadata", "schema_version"),
        ("stream_heads", "current_stream_version"),
        ("events", "stream_version"),
        ("events", "artifact_ref_count"),
        ("artifacts", "ordinal"),
        ("idempotency", "previous_stream_version"),
        ("idempotency", "current_stream_version"),
        ("checkpoints", "projection_version"),
    }
)
_BIGINT_COLUMNS = frozenset(
    {
        ("global_head", "current_global_position"),
        ("events", "global_position"),
        ("artifacts", "size_bytes"),
        ("idempotency", "first_global_position"),
        ("idempotency", "last_global_position"),
        ("checkpoints", "global_position"),
        ("projection_activations", "head_version"),
    }
)
_BOOLEAN_COLUMNS = frozenset(
    {
        ("schema_metadata", "singleton"),
        ("global_head", "singleton"),
    }
)
_NULLABLE_COLUMNS = frozenset(
    {
        ("global_head", "current_event_id"),
        ("global_head", "current_event_sha256"),
        ("stream_heads", "current_event_id"),
        ("stream_heads", "current_event_sha256"),
        ("events", "previous_stream_event_sha256"),
        ("artifacts", "encryption_key_id"),
        ("projection_activations", "previous_build_id"),
    }
)
_FUNCTION_BODY_PATTERN = re.compile(
    r"trace_backed_memory_v3_event_ledger\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)
_VERIFICATION_ISSUE_ORDER: tuple[LedgerVerificationIssueCode, ...] = (
    "EVENT_HASH_MISMATCH",
    "GLOBAL_POSITION_INVALID",
    "HASH_CHAIN_MISMATCH",
    "HEAD_MISMATCH",
    "PARTITION_MISMATCH",
    "STREAM_ID_MISMATCH",
    "STREAM_VERSION_GAP",
    "TRUSTED_CONTEXT_MISMATCH",
)
_ZERO_DIGEST = "sha256:" + "0" * 64
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresEventLedgerV1Error(EventLedgerPortError):
    """Stable base failure for the PostgreSQL canonical event ledger."""


class PostgresEventLedgerV1SchemaError(PostgresEventLedgerV1Error):
    pass


class PostgresEventLedgerV1PersistenceError(PostgresEventLedgerV1Error):
    pass


class PostgresEventLedgerV1IntegrityError(PostgresEventLedgerV1Error):
    pass


def _schema_error(message: str) -> PostgresEventLedgerV1SchemaError:
    return PostgresEventLedgerV1SchemaError(
        "TBM_EVENT_LEDGER_POSTGRES_SCHEMA",
        message,
    )


def _persistence_error(message: str) -> PostgresEventLedgerV1PersistenceError:
    return PostgresEventLedgerV1PersistenceError(
        "TBM_EVENT_LEDGER_POSTGRES_PERSISTENCE",
        message,
    )


def _integrity_error(message: str) -> PostgresEventLedgerV1IntegrityError:
    return PostgresEventLedgerV1IntegrityError(
        "TBM_EVENT_LEDGER_POSTGRES_INTEGRITY",
        message,
    )


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EventLedgerInvalidRequestError(
            "TBM_EVENT_LEDGER_NON_CANONICAL_JSON",
            "ledger value is not canonical JSON",
        ) from error


def _valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


@lru_cache(maxsize=1)
def _expected_function_bodies() -> dict[str, str]:
    try:
        source = read_packaged_resource(
            POSTGRES_EVENT_LEDGER_V1_SCHEMA_RESOURCE
        ).decode("utf-8")
    except (PackagedResourceError, UnicodeError) as error:
        raise _schema_error(
            "could not read canonical PostgreSQL event ledger schema"
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise _schema_error(
            "canonical PostgreSQL event ledger functions are incomplete"
        )
    return bodies


class PostgresEventLedgerV1:
    """Access-bound PostgreSQL event ledger with row-locked append CAS."""

    def __init__(
        self,
        connection: object,
        access_context: LedgerAccessContext,
        *,
        owns_connection: bool = False,
    ) -> None:
        if connection is None:
            raise ValueError("connection is required")
        if type(access_context) is not LedgerAccessContext:
            raise ValueError("access_context must be exactly LedgerAccessContext")
        if type(owns_connection) is not bool:
            raise ValueError("owns_connection must be a boolean")
        self._connection = connection
        self._access_context = access_context
        self._owns_connection = owns_connection
        self._closed = False
        self._lock = RLock()

    @classmethod
    def connect(
        cls,
        access_context: LedgerAccessContext,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresEventLedgerV1:
        if type(access_context) is not LedgerAccessContext:
            raise ValueError("access_context must be exactly LedgerAccessContext")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise _persistence_error(
                "failed to connect to PostgreSQL event ledger"
            ) from error
        return cls(connection, access_context, owns_connection=True)

    @property
    def access_context(self) -> LedgerAccessContext:
        return self._access_context

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresEventLedgerV1Error(
                "TBM_EVENT_LEDGER_POSTGRES_CLOSED",
                "PostgreSQL event ledger is closed",
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    @staticmethod
    def _names(cursor: object, query: str) -> frozenset[str]:
        cursor.execute(query, (_SCHEMA,))
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping) or type(row.get("name")) is not str
            for row in rows
        ):
            raise _schema_error("PostgreSQL event ledger catalog is malformed")
        return frozenset(cast(str, row["name"]) for row in rows)

    def _lock_schema(self, cursor: object, *, write: bool) -> None:
        cursor.execute(
            """
            SELECT active.schema_version AS active_schema_version,
                   ledger.schema_version AS ledger_schema_version,
                   ledger.contract_version AS contract_version
            FROM public.trace_backed_memory_schema AS active
            CROSS JOIN trace_backed_memory_v3_event_ledger.schema_metadata
                AS ledger
            WHERE active.singleton AND ledger.singleton
            FOR SHARE OF active, ledger
            """
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or rows[0].get("active_schema_version") != 2
            or rows[0].get("ledger_schema_version") != 1
            or rows[0].get("contract_version") != "tbm.event-ledger-port.v1"
        ):
            raise _schema_error("PostgreSQL event ledger schema metadata mismatch")
        mode = "ROW EXCLUSIVE" if write else "ACCESS SHARE"
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_event_ledger.schema_metadata, "
            "trace_backed_memory_v3_event_ledger.global_head, "
            "trace_backed_memory_v3_event_ledger.stream_heads, "
            "trace_backed_memory_v3_event_ledger.events, "
            "trace_backed_memory_v3_event_ledger.artifacts, "
            "trace_backed_memory_v3_event_ledger.idempotency, "
            "trace_backed_memory_v3_event_ledger.checkpoints, "
            "trace_backed_memory_v3_event_ledger.projection_activations "
            f"IN {mode} MODE"
        )
        tables = self._names(
            cursor,
            """
            SELECT class.relname AS name
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s AND class.relkind IN ('r', 'p')
            """,
        )
        indexes = self._names(
            cursor,
            """
            SELECT class.relname AS name
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s AND class.relkind = 'i'
            """,
        )
        functions = self._names(
            cursor,
            """
            SELECT procedure.proname AS name
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = %s
            """,
        )
        if (
            tables != _EXPECTED_TABLES
            or indexes != _EXPECTED_INDEXES
            or functions != _EXPECTED_FUNCTIONS
        ):
            raise _schema_error("PostgreSQL event ledger catalog mismatch")
        self._verify_columns(cursor)
        self._verify_functions_and_triggers(cursor)
        cursor.execute(
            """
            SELECT
                pg_catalog.has_schema_privilege('public', namespace.oid, 'USAGE')
                    AS public_usage,
                pg_catalog.has_schema_privilege('public', namespace.oid, 'CREATE')
                    AS public_create
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = %s
            """,
            (_SCHEMA,),
        )
        privileges = cursor.fetchall()
        if (
            len(privileges) != 1
            or not isinstance(privileges[0], Mapping)
            or privileges[0].get("public_usage") is not False
            or privileges[0].get("public_create") is not False
        ):
            raise _schema_error(
                "PostgreSQL event ledger schema privileges have drifted"
            )
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM pg_catalog.pg_policy AS policy
                 JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s) AS policies,
                (SELECT count(*) FROM pg_catalog.pg_rewrite AS rewrite
                 JOIN pg_catalog.pg_class AS class ON class.oid = rewrite.ev_class
                 JOIN pg_catalog.pg_namespace AS namespace
                   ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s
                   AND rewrite.rulename <> '_RETURN') AS rules
            """,
            (_SCHEMA, _SCHEMA),
        )
        extras = cursor.fetchall()
        if (
            len(extras) != 1
            or not isinstance(extras[0], Mapping)
            or extras[0].get("policies") != 0
            or extras[0].get("rules") != 0
        ):
            raise _schema_error(
                "PostgreSQL event ledger policy or rule catalog mismatch"
            )
        cursor.execute(_CATALOG_SHA256_QUERY, (_SCHEMA,) * 7)
        catalog_rows = cursor.fetchall()
        if (
            len(catalog_rows) != 1
            or not isinstance(catalog_rows[0], Mapping)
            or catalog_rows[0].get("catalog_sha256")
            != _EXPECTED_CATALOG_SHA256
        ):
            raise _schema_error(
                "PostgreSQL event ledger catalog digest mismatch"
            )

    @staticmethod
    def _verify_columns(cursor: object) -> None:
        cursor.execute(
            """
            SELECT class.relname AS table_name, attribute.attname AS column_name,
                   pg_catalog.format_type(
                       attribute.atttypid, attribute.atttypmod
                   ) AS data_type,
                   attribute.attnotnull AS not_null,
                   collation_record.collname AS collation_name
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS class
              ON class.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            LEFT JOIN pg_catalog.pg_collation AS collation_record
              ON collation_record.oid = attribute.attcollation
            WHERE namespace.nspname = %s
              AND class.relkind IN ('r', 'p')
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            ORDER BY class.relname, attribute.attnum
            """,
            (_SCHEMA,),
        )
        rows = cursor.fetchall()
        observed: dict[str, list[str]] = {name: [] for name in _EXPECTED_TABLES}
        for row in rows:
            if (
                not isinstance(row, Mapping)
                or type(row.get("table_name")) is not str
                or type(row.get("column_name")) is not str
            ):
                raise _schema_error("PostgreSQL event ledger columns are malformed")
            table = cast(str, row["table_name"])
            column = cast(str, row["column_name"])
            if table not in observed:
                raise _schema_error("PostgreSQL event ledger has an extra table")
            observed[table].append(column)
            key = (table, column)
            expected_type = (
                "boolean"
                if key in _BOOLEAN_COLUMNS
                else "integer"
                if key in _INTEGER_COLUMNS
                else "bigint"
                if key in _BIGINT_COLUMNS
                else "text"
            )
            if (
                row.get("data_type") != expected_type
                or row.get("not_null") != (key not in _NULLABLE_COLUMNS)
                or (
                    expected_type == "text"
                    and row.get("collation_name") != "C"
                )
                or (
                    expected_type != "text"
                    and row.get("collation_name") is not None
                )
            ):
                raise _schema_error(
                    "PostgreSQL event ledger column definition mismatch"
                )
        if any(
            tuple(observed[name]) != _EXPECTED_COLUMNS[name]
            for name in _EXPECTED_TABLES
        ):
            raise _schema_error("PostgreSQL event ledger column catalog mismatch")

    @staticmethod
    def _verify_functions_and_triggers(cursor: object) -> None:
        cursor.execute(
            """
            SELECT procedure.proname AS name, procedure.prosrc AS body,
                   language.lanname AS language,
                   procedure.prorettype::pg_catalog.regtype::text AS return_type,
                   procedure.proconfig
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            JOIN pg_catalog.pg_language AS language
              ON language.oid = procedure.prolang
            WHERE namespace.nspname = %s
            """,
            (_SCHEMA,),
        )
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping)
            or type(row.get("name")) is not str
            or type(row.get("body")) is not str
            or row.get("language") != "plpgsql"
            or row.get("return_type") != "trigger"
            or row.get("proconfig") != ["search_path=pg_catalog"]
            for row in rows
        ):
            raise _schema_error("PostgreSQL event ledger function drift")
        stored_bodies = {
            cast(str, row["name"]): cast(str, row["body"])
            .replace("\r\n", "\n")
            .strip()
            for row in rows
        }
        if stored_bodies != _expected_function_bodies():
            raise _schema_error("PostgreSQL event ledger function body drift")
        cursor.execute(
            """
            SELECT trigger.tgname AS name, procedure.proname AS function_name,
                   function_namespace.nspname AS function_schema,
                   trigger.tgenabled
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            JOIN pg_catalog.pg_proc AS procedure
              ON procedure.oid = trigger.tgfoid
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = %s AND NOT trigger.tgisinternal
            """,
            (_SCHEMA,),
        )
        trigger_rows = cursor.fetchall()
        observed: dict[str, str] = {}
        for row in trigger_rows:
            if (
                not isinstance(row, Mapping)
                or type(row.get("name")) is not str
                or type(row.get("function_name")) is not str
                or row.get("function_schema") != _SCHEMA
                or row.get("tgenabled") != "O"
            ):
                raise _schema_error("PostgreSQL event ledger trigger drift")
            observed[cast(str, row["name"])] = cast(str, row["function_name"])
        if observed != _EXPECTED_TRIGGER_FUNCTIONS:
            raise _schema_error("PostgreSQL event ledger trigger catalog mismatch")

    @staticmethod
    def _event_values(
        event: CanonicalEvent,
        partition_sha256: str,
    ) -> tuple[object, ...]:
        descriptor = dumps_canonical_event(event)
        if len(descriptor.encode("utf-8")) > EVENT_JSON_MAX_BYTES:
            raise EventLedgerInvalidRequestError(
                "TBM_EVENT_LEDGER_REQUEST_INVALID",
                "canonical event exceeds the storage byte limit",
            )
        return (
            event.event_id,
            event.event_sha256,
            partition_sha256,
            event.organization_id,
            event.tenant_id,
            event.repository_id,
            event.environment_id,
            event.stream_id,
            event.stream_version,
            event.global_position,
            event.previous_stream_event_sha256,
            event.classification,
            len(event.artifact_refs),
            descriptor,
        )

    @staticmethod
    def _artifact_values(
        event: CanonicalEvent,
        ordinal: int,
    ) -> tuple[object, ...]:
        artifact = event.artifact_refs[ordinal]
        return (
            event.event_id,
            ordinal,
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.media_type,
            artifact.size_bytes,
            artifact.classification,
            artifact.retention_policy_id,
            artifact.encryption_key_id,
            artifact.availability,
            _canonical_json(artifact.to_dict()),
        )

    def _stored_event(self, cursor: object, row: object) -> CanonicalEvent:
        fields = (
            "event_id",
            "event_sha256",
            "partition_sha256",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "stream_id",
            "stream_version",
            "global_position",
            "previous_stream_event_sha256",
            "classification",
            "artifact_ref_count",
            "canonical_event",
        )
        if not isinstance(row, Mapping) or set(row) != set(fields):
            raise _integrity_error("stored PostgreSQL event row has an invalid shape")
        descriptor = row.get("canonical_event")
        if type(descriptor) is not str:
            raise _integrity_error("stored PostgreSQL event descriptor is invalid")
        try:
            event = loads_canonical_event(descriptor)
        except EventV1ContractError as error:
            raise _integrity_error(
                "stored PostgreSQL event descriptor failed validation"
            ) from error
        expected = dict(
            zip(
                fields,
                self._event_values(event, cast(str, row["partition_sha256"])),
                strict=True,
            )
        )
        if dict(row) != expected:
            raise _integrity_error(
                "stored PostgreSQL event columns do not match its descriptor"
            )
        cursor.execute(
            """
            SELECT event_id, ordinal, artifact_id, content_sha256, media_type,
                   size_bytes, classification, retention_policy_id,
                   encryption_key_id, availability, descriptor
            FROM trace_backed_memory_v3_event_ledger.artifacts
            WHERE event_id = %s ORDER BY ordinal
            """,
            (event.event_id,),
        )
        artifact_rows = cursor.fetchall()
        artifact_fields = (
            "event_id",
            "ordinal",
            "artifact_id",
            "content_sha256",
            "media_type",
            "size_bytes",
            "classification",
            "retention_policy_id",
            "encryption_key_id",
            "availability",
            "descriptor",
        )
        expected_artifacts = tuple(
            dict(
                zip(
                    artifact_fields,
                    self._artifact_values(event, ordinal),
                    strict=True,
                )
            )
            for ordinal in range(len(event.artifact_refs))
        )
        if tuple(dict(item) for item in artifact_rows) != expected_artifacts:
            raise _integrity_error(
                "stored PostgreSQL artifact descriptors do not match the event"
            )
        return event

    def _select_event_by_sha256(
        self,
        cursor: object,
        event_sha256: str,
    ) -> CanonicalEvent | None:
        cursor.execute(
            """
            SELECT event_id, event_sha256, partition_sha256, organization_id,
                   tenant_id, repository_id, environment_id, stream_id,
                   stream_version, global_position,
                   previous_stream_event_sha256, classification,
                   artifact_ref_count, canonical_event
            FROM trace_backed_memory_v3_event_ledger.events
            WHERE event_sha256 = %s
            """,
            (event_sha256,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise _integrity_error("event hash resolved to multiple rows")
        return self._stored_event(cursor, rows[0])

    def _select_global_position(self, cursor: object, *, for_update: bool) -> int:
        cursor.execute(
            "SELECT current_global_position "
            "FROM trace_backed_memory_v3_event_ledger.global_head "
            "WHERE singleton" + (" FOR UPDATE" if for_update else "")
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or type(rows[0].get("current_global_position")) is not int
            or cast(int, rows[0]["current_global_position"]) < 0
        ):
            raise _integrity_error("PostgreSQL global ledger head is invalid")
        return cast(int, rows[0]["current_global_position"])

    def _select_head_event(
        self,
        cursor: object,
        stream_id: str,
        *,
        for_update: bool,
    ) -> CanonicalEvent | None:
        partition = self._access_context.partition
        cursor.execute(
            """
            SELECT organization_id, tenant_id, repository_id, environment_id,
                   current_stream_version, current_event_id,
                   current_event_sha256
            FROM trace_backed_memory_v3_event_ledger.stream_heads
            WHERE partition_sha256 = %s AND stream_id = %s
            """ + (" FOR UPDATE" if for_update else ""),
            (partition.partition_sha256, stream_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise _integrity_error("PostgreSQL stream head has an invalid shape")
        row = rows[0]
        if (
            row.get("organization_id") != partition.organization_id
            or row.get("tenant_id") != partition.tenant_id
            or row.get("repository_id") != partition.repository_id
            or row.get("environment_id") != partition.environment_id
        ):
            raise _integrity_error("PostgreSQL stream head partition mismatch")
        version = row.get("current_stream_version")
        if version == 0 and row.get("current_event_id") is None and row.get(
            "current_event_sha256"
        ) is None:
            return None
        digest = row.get("current_event_sha256")
        if type(version) is not int or type(digest) is not str:
            raise _integrity_error("PostgreSQL stream head is malformed")
        event = self._select_event_by_sha256(cursor, digest)
        if (
            event is None
            or event.event_id != row.get("current_event_id")
            or event.stream_id != stream_id
            or event.stream_version != version
        ):
            raise _integrity_error(
                "PostgreSQL stream head does not match its event tail"
            )
        return event

    def _select_idempotency(
        self,
        cursor: object,
        idempotency_key_sha256: str,
        *,
        for_update: bool,
    ) -> object | None:
        cursor.execute(
            """
            SELECT command_sha256, request_sha256, stream_id,
                   previous_stream_version, current_stream_version,
                   first_global_position, last_global_position,
                   event_sha256s_json, receipt_sha256
            FROM trace_backed_memory_v3_event_ledger.idempotency
            WHERE partition_sha256 = %s AND idempotency_key_sha256 = %s
            """ + (" FOR UPDATE" if for_update else ""),
            (
                self._access_context.partition.partition_sha256,
                idempotency_key_sha256,
            ),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise _integrity_error("idempotency key resolved to multiple rows")
        return rows[0]

    def _receipt_from_row(
        self,
        cursor: object,
        idempotency_key_sha256: str,
        row: object,
    ) -> LedgerAppendReceipt:
        fields = (
            "command_sha256",
            "request_sha256",
            "stream_id",
            "previous_stream_version",
            "current_stream_version",
            "first_global_position",
            "last_global_position",
            "event_sha256s_json",
            "receipt_sha256",
        )
        if not isinstance(row, Mapping) or set(row) != set(fields):
            raise _integrity_error("stored idempotency row has an invalid shape")
        raw_hashes = row.get("event_sha256s_json")
        if type(raw_hashes) is not str:
            raise _integrity_error("stored idempotency event list is invalid")
        try:
            event_sha256s = json.loads(raw_hashes)
        except json.JSONDecodeError as error:
            raise _integrity_error("stored idempotency event list is invalid") from error
        if (
            type(event_sha256s) is not list
            or not event_sha256s
            or len(event_sha256s) > 100
            or any(not _valid_digest(item) for item in event_sha256s)
            or _canonical_json(event_sha256s) != raw_hashes
        ):
            raise _integrity_error("stored idempotency event list is noncanonical")
        events: list[CanonicalEvent] = []
        for digest in event_sha256s:
            event = self._select_event_by_sha256(cursor, digest)
            if event is None:
                raise _integrity_error(
                    "stored idempotency record references a missing event"
                )
            events.append(event)
        try:
            return LedgerAppendReceipt(
                request_sha256=cast(str, row["request_sha256"]),
                idempotency_key_sha256=idempotency_key_sha256,
                command_sha256=cast(str, row["command_sha256"]),
                stream_id=cast(str, row["stream_id"]),
                previous_stream_version=cast(int, row["previous_stream_version"]),
                current_stream_version=cast(int, row["current_stream_version"]),
                first_global_position=cast(int, row["first_global_position"]),
                last_global_position=cast(int, row["last_global_position"]),
                events=tuple(events),
                outcome="committed",
                receipt_sha256=cast(str, row["receipt_sha256"]),
            )
        except EventLedgerPortError as error:
            raise _integrity_error(
                "stored idempotency receipt failed validation"
            ) from error

    def _insert_event(self, cursor: object, event: CanonicalEvent) -> None:
        partition_sha256 = self._access_context.partition.partition_sha256
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_event_ledger.events (
                event_id, event_sha256, partition_sha256, organization_id,
                tenant_id, repository_id, environment_id, stream_id,
                stream_version, global_position,
                previous_stream_event_sha256, classification,
                artifact_ref_count, canonical_event
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            self._event_values(event, partition_sha256),
        )
        for ordinal in range(len(event.artifact_refs)):
            cursor.execute(
                """
                INSERT INTO trace_backed_memory_v3_event_ledger.artifacts (
                    event_id, ordinal, artifact_id, content_sha256,
                    media_type, size_bytes, classification,
                    retention_policy_id, encryption_key_id, availability,
                    descriptor
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                self._artifact_values(event, ordinal),
            )
        cursor.execute(
            """
            UPDATE trace_backed_memory_v3_event_ledger.stream_heads
            SET current_stream_version = %s, current_event_id = %s,
                current_event_sha256 = %s
            WHERE partition_sha256 = %s AND stream_id = %s
              AND current_stream_version = %s
              AND current_event_sha256 IS NOT DISTINCT FROM %s
            """,
            (
                event.stream_version,
                event.event_id,
                event.event_sha256,
                partition_sha256,
                event.stream_id,
                event.stream_version - 1,
                event.previous_stream_event_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_STALE_STREAM_VERSION",
                "stream head changed during append",
            )
        cursor.execute(
            """
            UPDATE trace_backed_memory_v3_event_ledger.global_head
            SET current_global_position = %s, current_event_id = %s,
                current_event_sha256 = %s
            WHERE singleton AND current_global_position = %s
            """,
            (
                event.global_position,
                event.event_id,
                event.event_sha256,
                event.global_position - 1,
            ),
        )
        if cursor.rowcount != 1:
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT",
                "global ledger head changed during append",
            )

    @_synchronized
    def append(
        self,
        stream_id: str,
        expected_version: int,
        events: tuple[CanonicalEvent, ...],
        idempotency: LedgerIdempotency,
    ) -> LedgerAppendReceipt:
        self._require_open()
        request = LedgerAppendRequest(
            access=self._access_context,
            stream_id=stream_id,
            expected_stream_version=expected_version,
            events=events,
            idempotency=idempotency,
        )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=True)
                    next_global = self._select_global_position(
                        cursor,
                        for_update=True,
                    ) + 1
                    retained_row = self._select_idempotency(
                        cursor,
                        idempotency.idempotency_key_sha256,
                        for_update=True,
                    )
                    if retained_row is not None:
                        if (
                            retained_row.get("command_sha256")
                            != idempotency.command_sha256
                            or retained_row.get("request_sha256")
                            != request.request_sha256
                        ):
                            raise EventLedgerIdempotencyConflictError(
                                "TBM_EVENT_LEDGER_IDEMPOTENCY_CONFLICT",
                                "idempotency key is bound to another command",
                            )
                        retained = self._receipt_from_row(
                            cursor,
                            idempotency.idempotency_key_sha256,
                            retained_row,
                        )
                        verify_ledger_append_receipt(request, retained)
                        return retained
                    partition = self._access_context.partition
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_event_ledger.stream_heads (
                                partition_sha256, stream_id, organization_id,
                                tenant_id, repository_id, environment_id,
                                current_stream_version, current_event_id,
                                current_event_sha256
                            ) VALUES (%s, %s, %s, %s, %s, %s, 0, NULL, NULL)
                        ON CONFLICT (partition_sha256, stream_id) DO NOTHING
                        """,
                        (
                            partition.partition_sha256,
                            stream_id,
                            partition.organization_id,
                            partition.tenant_id,
                            partition.repository_id,
                            partition.environment_id,
                        ),
                    )
                    current_head = self._select_head_event(
                        cursor,
                        stream_id,
                        for_update=True,
                    )
                    verify_ledger_append_precondition(
                        request,
                        current_head=current_head,
                        next_global_position=next_global,
                    )
                    for event in events:
                        self._insert_event(cursor, event)
                    receipt = build_ledger_append_receipt(request)
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_event_ledger.idempotency (
                                partition_sha256, idempotency_key_sha256,
                                command_sha256, request_sha256, stream_id,
                                previous_stream_version, current_stream_version,
                                first_global_position, last_global_position,
                                event_sha256s_json, receipt_sha256
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                            )
                        """,
                        (
                            partition.partition_sha256,
                            idempotency.idempotency_key_sha256,
                            idempotency.command_sha256,
                            request.request_sha256,
                            stream_id,
                            receipt.previous_stream_version,
                            receipt.current_stream_version,
                            receipt.first_global_position,
                            receipt.last_global_position,
                            _canonical_json(
                                [event.event_sha256 for event in events]
                            ),
                            receipt.receipt_sha256,
                        ),
                    )
                    retained_row = self._select_idempotency(
                        cursor,
                        idempotency.idempotency_key_sha256,
                        for_update=False,
                    )
                    if retained_row is None:
                        raise _integrity_error(
                            "committed idempotency receipt could not be read back"
                        )
                    retained = self._receipt_from_row(
                        cursor,
                        idempotency.idempotency_key_sha256,
                        retained_row,
                    )
                    verify_ledger_append_receipt(request, retained)
                    return retained
        except EventLedgerPortError:
            raise
        except Exception as error:
            self._raise_database_error(error, "PostgreSQL event ledger append failed")

    def _events_from_query(
        self,
        cursor: object,
        query: str,
        parameters: tuple[object, ...],
    ) -> tuple[CanonicalEvent, ...]:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
        return tuple(self._stored_event(cursor, row) for row in rows)

    @_synchronized
    def read_stream(
        self,
        stream_id: str,
        from_version: int = 1,
        limit: int = 100,
    ) -> LedgerPage:
        self._require_open()
        request = LedgerStreamReadRequest(
            self._access_context,
            stream_id,
            from_version,
            limit,
        )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    events = self._events_from_query(
                        cursor,
                        """
                        SELECT event_id, event_sha256, partition_sha256,
                               organization_id, tenant_id, repository_id,
                               environment_id, stream_id, stream_version,
                               global_position, previous_stream_event_sha256,
                               classification, artifact_ref_count,
                               canonical_event
                        FROM trace_backed_memory_v3_event_ledger.events
                        WHERE partition_sha256 = %s AND stream_id = %s
                          AND stream_version >= %s
                        ORDER BY stream_version LIMIT %s
                        """,
                        (
                            self._access_context.partition.partition_sha256,
                            stream_id,
                            from_version,
                            limit + 1,
                        ),
                    )
                    has_more = len(events) > limit
                    selected = events[:limit]
                    page = build_ledger_page(
                        read_kind="stream",
                        events=selected,
                        high_watermark_global_position=(
                            self._select_global_position(
                                cursor,
                                for_update=False,
                            )
                        ),
                        next_stream_version=(
                            selected[-1].stream_version + 1
                            if has_more and selected
                            else None
                        ),
                        next_global_position=None,
                        has_more=has_more,
                    )
                    verify_ledger_stream_page(request, page)
                    return page
        except EventLedgerPortError:
            raise
        except Exception as error:
            self._raise_database_error(error, "PostgreSQL stream read failed")

    @_synchronized
    def read_global(
        self,
        after_position: int = 0,
        limit: int = 100,
    ) -> LedgerPage:
        self._require_open()
        request = LedgerGlobalReadRequest(
            self._access_context,
            after_position,
            limit,
        )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    events = self._events_from_query(
                        cursor,
                        """
                        SELECT event_id, event_sha256, partition_sha256,
                               organization_id, tenant_id, repository_id,
                               environment_id, stream_id, stream_version,
                               global_position, previous_stream_event_sha256,
                               classification, artifact_ref_count,
                               canonical_event
                        FROM trace_backed_memory_v3_event_ledger.events
                        WHERE partition_sha256 = %s AND global_position > %s
                          AND classification = ANY(%s)
                        ORDER BY global_position LIMIT %s
                        """,
                        (
                            self._access_context.partition.partition_sha256,
                            after_position,
                            list(
                                self._access_context.classification_filter.allowed
                            ),
                            limit + 1,
                        ),
                    )
                    has_more = len(events) > limit
                    selected = events[:limit]
                    page = build_ledger_page(
                        read_kind="global",
                        events=selected,
                        high_watermark_global_position=(
                            self._select_global_position(
                                cursor,
                                for_update=False,
                            )
                        ),
                        next_stream_version=None,
                        next_global_position=(
                            selected[-1].global_position
                            if has_more and selected
                            else None
                        ),
                        has_more=has_more,
                    )
                    verify_ledger_global_page(request, page)
                    return page
        except EventLedgerPortError:
            raise
        except Exception as error:
            self._raise_database_error(error, "PostgreSQL global read failed")

    def _verification_result(
        self,
        cursor: object,
        stream_id: str,
    ) -> LedgerStreamVerification:
        partition_sha256 = self._access_context.partition.partition_sha256
        events = self._events_from_query(
            cursor,
            """
            SELECT event_id, event_sha256, partition_sha256, organization_id,
                   tenant_id, repository_id, environment_id, stream_id,
                   stream_version, global_position,
                   previous_stream_event_sha256, classification,
                   artifact_ref_count, canonical_event
            FROM trace_backed_memory_v3_event_ledger.events
            WHERE partition_sha256 = %s AND stream_id = %s
            ORDER BY stream_version
            """,
            (partition_sha256, stream_id),
        )
        issues: set[LedgerVerificationIssueCode] = set()
        previous: CanonicalEvent | None = None
        previous_global = 0
        for index, event in enumerate(events, start=1):
            if event.stream_id != stream_id:
                issues.add("STREAM_ID_MISMATCH")
            if event.stream_version != index:
                issues.add("STREAM_VERSION_GAP")
            if (
                LedgerTenantPartition(
                    organization_id=event.organization_id,
                    tenant_id=event.tenant_id,
                    repository_id=event.repository_id,
                    environment_id=event.environment_id,
                ).partition_sha256
                != partition_sha256
            ):
                issues.add("PARTITION_MISMATCH")
            if not self._access_context.classification_filter.allows(
                event.classification
            ):
                raise EventLedgerClassificationDeniedError(
                    "TBM_EVENT_LEDGER_CLASSIFICATION_DENIED",
                    "stream classification is not allowed for verification",
                )
            if event.global_position <= previous_global:
                issues.add("GLOBAL_POSITION_INVALID")
            previous_global = event.global_position
            try:
                verify_event_parent(event, previous)
            except EventV1ContractError:
                issues.add("HASH_CHAIN_MISMATCH")
            previous = event
        head = self._select_head_event(cursor, stream_id, for_update=False)
        if (not events and head is not None) or (
            events
            and (
                head is None
                or head.event_id != events[-1].event_id
                or head.event_sha256 != events[-1].event_sha256
            )
        ):
            issues.add("HEAD_MISMATCH")
        ordered = tuple(
            issue for issue in _VERIFICATION_ISSUE_ORDER if issue in issues
        )
        result = LedgerStreamVerification(
            stream_id=stream_id,
            partition_sha256=partition_sha256,
            verified_stream_version=len(events),
            verified_event_count=len(events),
            head_event_sha256=(
                None
                if not events
                else events[-1].event_sha256
                if _valid_digest(events[-1].event_sha256)
                else _ZERO_DIGEST
            ),
            valid=not ordered,
            issue_codes=ordered,
        )
        verify_ledger_stream_verification(
            self._access_context,
            stream_id,
            result,
        )
        return result

    def _projection_checkpoint_from_row(
        self,
        row: object,
    ) -> ProjectionCheckpoint:
        fields = {
            "projection_name",
            "projection_version",
            "partition_sha256",
            "global_position",
            "state_sha256",
            "descriptor",
        }
        if not isinstance(row, Mapping) or set(row) != fields:
            raise _integrity_error(
                "stored projection checkpoint has an invalid shape"
            )
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            raise _integrity_error(
                "stored projection checkpoint descriptor is invalid"
            )
        try:
            parsed = json.loads(descriptor)
            if _canonical_json(parsed) != descriptor:
                raise ValueError("checkpoint descriptor is noncanonical")
            checkpoint = parse_projection_checkpoint(parsed)
        except (ProjectionCheckpointError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise _integrity_error(
                "stored projection checkpoint descriptor is invalid"
            ) from error
        if (
            checkpoint.projection_name != row.get("projection_name")
            or checkpoint.reducer_version != row.get("projection_version")
            or checkpoint.partition_sha256 != row.get("partition_sha256")
            or checkpoint.global_position != row.get("global_position")
            or checkpoint.state_sha256 != row.get("state_sha256")
        ):
            raise _integrity_error(
                "stored projection checkpoint columns do not match its descriptor"
            )
        return checkpoint

    def _projection_activation_from_row(
        self,
        row: object,
    ) -> ProjectionActivation:
        fields = {
            "projection_name",
            "partition_sha256",
            "head_version",
            "target_build_id",
            "previous_build_id",
            "activation_sha256",
            "descriptor",
        }
        if not isinstance(row, Mapping) or set(row) != fields:
            raise _integrity_error(
                "stored projection activation has an invalid shape"
            )
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            raise _integrity_error(
                "stored projection activation descriptor is invalid"
            )
        try:
            parsed = json.loads(descriptor)
            if _canonical_json(parsed) != descriptor:
                raise ValueError("activation descriptor is noncanonical")
            activation = parse_projection_activation(parsed)
        except (ProjectionCheckpointError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise _integrity_error(
                "stored projection activation descriptor is invalid"
            ) from error
        if (
            activation.projection_name != row.get("projection_name")
            or activation.partition_sha256 != row.get("partition_sha256")
            or activation.head_version != row.get("head_version")
            or activation.target_build_id != row.get("target_build_id")
            or activation.previous_build_id != row.get("previous_build_id")
            or activation.activation_sha256 != row.get("activation_sha256")
        ):
            raise _integrity_error(
                "stored projection activation columns do not match its descriptor"
            )
        return activation

    @_synchronized
    def save_checkpoint(
        self,
        checkpoint: ProjectionCheckpoint,
    ) -> ProjectionCheckpoint:
        self._require_open()
        if type(checkpoint) is not ProjectionCheckpoint:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_CHECKPOINT_INVALID",
                "checkpoint must be exactly ProjectionCheckpoint",
            )
        if checkpoint.partition_sha256 != self._access_context.partition.partition_sha256:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_PARTITION_MISMATCH",
                "checkpoint partition does not match ledger access",
            )
        parameters = (
            checkpoint.projection_name,
            checkpoint.reducer_version,
            checkpoint.partition_sha256,
            checkpoint.global_position,
        )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=True)
                    cursor.execute(
                        """
                        INSERT INTO trace_backed_memory_v3_event_ledger.checkpoints (
                            projection_name, projection_version,
                            partition_sha256, global_position, state_sha256,
                            descriptor
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (
                            projection_name, projection_version,
                            partition_sha256, global_position
                        ) DO NOTHING
                        """,
                        (
                            *parameters,
                            checkpoint.state_sha256,
                            _canonical_json(checkpoint.to_dict()),
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT projection_name, projection_version,
                               partition_sha256, global_position, state_sha256,
                               descriptor
                        FROM trace_backed_memory_v3_event_ledger.checkpoints
                        WHERE projection_name = %s AND projection_version = %s
                          AND partition_sha256 = %s AND global_position = %s
                        FOR SHARE
                        """,
                        parameters,
                    )
                    rows = cursor.fetchall()
                    if len(rows) != 1:
                        raise _integrity_error(
                            "projection checkpoint read-back is missing"
                        )
                    retained = self._projection_checkpoint_from_row(rows[0])
                    if retained.build_id != checkpoint.build_id:
                        raise ProjectionCheckpointConflictError(
                            "TBM_PROJECTION_CHECKPOINT_CONFLICT",
                            "checkpoint position retained a different projection digest",
                        )
                    return retained
        except (ProjectionCheckpointError, EventLedgerPortError):
            raise
        except Exception as error:
            self._raise_projection_database_error(
                error,
                "PostgreSQL projection checkpoint write failed",
            )

    @_synchronized
    def load_checkpoint(self, build_id: str) -> ProjectionCheckpoint:
        self._require_open()
        if not _valid_digest(build_id):
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_CHECKPOINT_INVALID",
                "build_id is invalid",
            )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    cursor.execute(
                        """
                        SELECT projection_name, projection_version,
                               partition_sha256, global_position, state_sha256,
                               descriptor
                        FROM trace_backed_memory_v3_event_ledger.checkpoints
                        WHERE partition_sha256 = %s
                        ORDER BY projection_name, projection_version,
                                 global_position
                        LIMIT %s
                        """,
                        (
                            self._access_context.partition.partition_sha256,
                            PROJECTION_MAX_CHECKPOINTS_PER_LIST + 1,
                        ),
                    )
                    rows = cursor.fetchall()
                    if len(rows) > PROJECTION_MAX_CHECKPOINTS_PER_LIST:
                        raise ProjectionCheckpointError(
                            "TBM_PROJECTION_LIST_LIMIT_EXCEEDED",
                            "projection checkpoint list exceeds the bounded limit",
                        )
                    matches = [
                        checkpoint
                        for checkpoint in (
                            self._projection_checkpoint_from_row(row)
                            for row in rows
                        )
                        if checkpoint.build_id == build_id
                    ]
                    if not matches:
                        raise ProjectionCheckpointNotFoundError(
                            "TBM_PROJECTION_NOT_FOUND",
                            "projection checkpoint is not retained",
                        )
                    if len(matches) != 1:
                        raise _integrity_error(
                            "projection build ID is retained more than once"
                        )
                    return matches[0]
        except (ProjectionCheckpointError, EventLedgerPortError):
            raise
        except Exception as error:
            self._raise_projection_database_error(
                error,
                "PostgreSQL projection checkpoint read failed",
            )

    @_synchronized
    def load_latest_checkpoint(
        self,
        projection_name: str,
        reducer_version: int,
        partition_sha256: str,
    ) -> ProjectionCheckpoint | None:
        self._require_open()
        if partition_sha256 != self._access_context.partition.partition_sha256:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_PARTITION_MISMATCH",
                "checkpoint partition does not match ledger access",
            )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    cursor.execute(
                        """
                        SELECT projection_name, projection_version,
                               partition_sha256, global_position, state_sha256,
                               descriptor
                        FROM trace_backed_memory_v3_event_ledger.checkpoints
                        WHERE projection_name = %s AND projection_version = %s
                          AND partition_sha256 = %s
                        ORDER BY global_position DESC LIMIT 1
                        """,
                        (projection_name, reducer_version, partition_sha256),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        return None
                    if len(rows) != 1:
                        raise _integrity_error(
                            "latest projection checkpoint is ambiguous"
                        )
                    return self._projection_checkpoint_from_row(rows[0])
        except (ProjectionCheckpointError, EventLedgerPortError):
            raise
        except Exception as error:
            self._raise_projection_database_error(
                error,
                "PostgreSQL projection checkpoint read failed",
            )

    @_synchronized
    def list_checkpoints(
        self,
        projection_name: str | None = None,
        partition_sha256: str | None = None,
    ) -> tuple[ProjectionCheckpoint, ...]:
        self._require_open()
        selected_partition = (
            self._access_context.partition.partition_sha256
            if partition_sha256 is None
            else partition_sha256
        )
        if selected_partition != self._access_context.partition.partition_sha256:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_PARTITION_MISMATCH",
                "checkpoint partition does not match ledger access",
            )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    if projection_name is None:
                        cursor.execute(
                            """
                            SELECT projection_name, projection_version,
                                   partition_sha256, global_position,
                                   state_sha256, descriptor
                            FROM trace_backed_memory_v3_event_ledger.checkpoints
                            WHERE partition_sha256 = %s
                            ORDER BY projection_name, projection_version,
                                     global_position
                            LIMIT %s
                            """,
                            (
                                selected_partition,
                                PROJECTION_MAX_CHECKPOINTS_PER_LIST + 1,
                            ),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT projection_name, projection_version,
                                   partition_sha256, global_position,
                                   state_sha256, descriptor
                            FROM trace_backed_memory_v3_event_ledger.checkpoints
                            WHERE partition_sha256 = %s
                              AND projection_name = %s
                            ORDER BY projection_name, projection_version,
                                     global_position
                            LIMIT %s
                            """,
                            (
                                selected_partition,
                                projection_name,
                                PROJECTION_MAX_CHECKPOINTS_PER_LIST + 1,
                            ),
                        )
                    rows = cursor.fetchall()
                    if len(rows) > PROJECTION_MAX_CHECKPOINTS_PER_LIST:
                        raise ProjectionCheckpointError(
                            "TBM_PROJECTION_LIST_LIMIT_EXCEEDED",
                            "projection checkpoint list exceeds the bounded limit",
                        )
                    return tuple(
                        self._projection_checkpoint_from_row(row) for row in rows
                    )
        except (ProjectionCheckpointError, EventLedgerPortError):
            raise
        except Exception as error:
            self._raise_projection_database_error(
                error,
                "PostgreSQL projection checkpoint list failed",
            )

    @_synchronized
    def append_activation(
        self,
        activation: ProjectionActivation,
        *,
        expected_head_version: int,
        expected_current_build_id: str | None,
    ) -> ProjectionActivation:
        self._require_open()
        if type(activation) is not ProjectionActivation:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_ACTIVATION_INVALID",
                "activation must be exactly ProjectionActivation",
            )
        if activation.partition_sha256 != self._access_context.partition.partition_sha256:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_PARTITION_MISMATCH",
                "activation partition does not match ledger access",
            )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=True)
                    cursor.execute(
                        """
                        SELECT projection_name, projection_version,
                               partition_sha256, global_position, state_sha256,
                               descriptor
                        FROM trace_backed_memory_v3_event_ledger.checkpoints
                        WHERE partition_sha256 = %s AND projection_name = %s
                        ORDER BY projection_version, global_position
                        LIMIT %s
                        FOR SHARE
                        """,
                        (
                            activation.partition_sha256,
                            activation.projection_name,
                            PROJECTION_MAX_CHECKPOINTS_PER_LIST + 1,
                        ),
                    )
                    target_rows = cursor.fetchall()
                    if len(target_rows) > PROJECTION_MAX_CHECKPOINTS_PER_LIST:
                        raise ProjectionCheckpointError(
                            "TBM_PROJECTION_LIST_LIMIT_EXCEEDED",
                            "projection checkpoint list exceeds the bounded limit",
                        )
                    targets = [
                        checkpoint
                        for checkpoint in (
                            self._projection_checkpoint_from_row(row)
                            for row in target_rows
                        )
                        if checkpoint.build_id == activation.target_build_id
                    ]
                    if len(targets) != 1:
                        raise ProjectionCheckpointNotFoundError(
                            "TBM_PROJECTION_NOT_FOUND",
                            "activation target checkpoint is not retained",
                        )
                    cursor.execute(
                        """
                        SELECT projection_name, partition_sha256, head_version,
                               target_build_id, previous_build_id,
                               activation_sha256, descriptor
                        FROM trace_backed_memory_v3_event_ledger.projection_activations
                        WHERE projection_name = %s AND partition_sha256 = %s
                        ORDER BY head_version DESC LIMIT 1
                        FOR UPDATE
                        """,
                        (activation.projection_name, activation.partition_sha256),
                    )
                    current_rows = cursor.fetchall()
                    current = (
                        None
                        if not current_rows
                        else self._projection_activation_from_row(current_rows[0])
                    )
                    current_version = 0 if current is None else current.head_version
                    current_build = None if current is None else current.target_build_id
                    if (
                        current_version != expected_head_version
                        or current_build != expected_current_build_id
                        or activation.head_version != current_version + 1
                        or activation.previous_build_id != current_build
                    ):
                        raise ProjectionCheckpointConflictError(
                            "TBM_PROJECTION_HEAD_CONFLICT",
                            "projection head changed before activation",
                        )
                    cursor.execute(
                        """
                        INSERT INTO
                            trace_backed_memory_v3_event_ledger.projection_activations (
                                projection_name, partition_sha256, head_version,
                                target_build_id, previous_build_id, operation,
                                activation_sha256, descriptor
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            activation.projection_name,
                            activation.partition_sha256,
                            activation.head_version,
                            activation.target_build_id,
                            activation.previous_build_id,
                            activation.operation,
                            activation.activation_sha256,
                            _canonical_json(activation.to_dict()),
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT projection_name, partition_sha256, head_version,
                               target_build_id, previous_build_id,
                               activation_sha256, descriptor
                        FROM trace_backed_memory_v3_event_ledger.projection_activations
                        WHERE projection_name = %s AND partition_sha256 = %s
                          AND head_version = %s
                        FOR SHARE
                        """,
                        (
                            activation.projection_name,
                            activation.partition_sha256,
                            activation.head_version,
                        ),
                    )
                    rows = cursor.fetchall()
                    if len(rows) != 1:
                        raise _integrity_error(
                            "projection activation read-back is missing"
                        )
                    retained = self._projection_activation_from_row(rows[0])
                    if retained != activation:
                        raise _integrity_error(
                            "projection activation read-back does not match"
                        )
                    return retained
        except (ProjectionCheckpointError, EventLedgerPortError):
            raise
        except Exception as error:
            self._raise_projection_database_error(
                error,
                "PostgreSQL projection activation failed",
            )

    @_synchronized
    def current_activation(
        self,
        projection_name: str,
        partition_sha256: str,
    ) -> ProjectionActivation | None:
        history = self.activation_history(projection_name, partition_sha256)
        return history[-1] if history else None

    @_synchronized
    def activation_history(
        self,
        projection_name: str,
        partition_sha256: str,
    ) -> tuple[ProjectionActivation, ...]:
        self._require_open()
        if partition_sha256 != self._access_context.partition.partition_sha256:
            raise ProjectionCheckpointError(
                "TBM_PROJECTION_PARTITION_MISMATCH",
                "activation partition does not match ledger access",
            )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    cursor.execute(
                        """
                        SELECT projection_name, partition_sha256, head_version,
                               target_build_id, previous_build_id,
                               activation_sha256, descriptor
                        FROM trace_backed_memory_v3_event_ledger.projection_activations
                        WHERE projection_name = %s AND partition_sha256 = %s
                        ORDER BY head_version LIMIT %s
                        """,
                        (
                            projection_name,
                            partition_sha256,
                            PROJECTION_MAX_ACTIVATIONS_PER_LIST + 1,
                        ),
                    )
                    rows = cursor.fetchall()
                    if len(rows) > PROJECTION_MAX_ACTIVATIONS_PER_LIST:
                        raise ProjectionCheckpointError(
                            "TBM_PROJECTION_LIST_LIMIT_EXCEEDED",
                            "projection activation list exceeds the bounded limit",
                        )
                    return tuple(
                        self._projection_activation_from_row(row) for row in rows
                    )
        except (ProjectionCheckpointError, EventLedgerPortError):
            raise
        except Exception as error:
            self._raise_projection_database_error(
                error,
                "PostgreSQL projection activation read failed",
            )

    @_synchronized
    def verify_stream(self, stream_id: str) -> LedgerStreamVerification:
        self._require_open()
        LedgerStreamReadRequest(self._access_context, stream_id, 1, 1)
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    return self._verification_result(cursor, stream_id)
        except EventLedgerPortError:
            raise
        except Exception as error:
            self._raise_database_error(
                error,
                "PostgreSQL stream verification failed",
            )

    @_synchronized
    def verify_integrity(self) -> tuple[LedgerStreamVerification, ...]:
        self._require_open()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor, write=False)
                    all_events = self._events_from_query(
                        cursor,
                        """
                        SELECT event_id, event_sha256, partition_sha256,
                               organization_id, tenant_id, repository_id,
                               environment_id, stream_id, stream_version,
                               global_position, previous_stream_event_sha256,
                               classification, artifact_ref_count,
                               canonical_event
                        FROM trace_backed_memory_v3_event_ledger.events
                        ORDER BY global_position
                        """,
                        (),
                    )
                    if any(
                        event.global_position != position
                        for position, event in enumerate(all_events, start=1)
                    ):
                        raise _integrity_error(
                            "PostgreSQL global positions are not gap-free"
                        )
                    if self._select_global_position(
                        cursor,
                        for_update=False,
                    ) != len(all_events):
                        raise _integrity_error(
                            "PostgreSQL global head does not match event count"
                        )
                    cursor.execute(
                        """
                        SELECT stream_id
                        FROM trace_backed_memory_v3_event_ledger.stream_heads
                        WHERE partition_sha256 = %s
                        ORDER BY stream_id
                        """,
                        (self._access_context.partition.partition_sha256,),
                    )
                    rows = cursor.fetchall()
                    if any(
                        not isinstance(row, Mapping)
                        or type(row.get("stream_id")) is not str
                        for row in rows
                    ):
                        raise _integrity_error(
                            "PostgreSQL stream inventory is malformed"
                        )
                    verifications = tuple(
                        self._verification_result(
                            cursor,
                            cast(str, row["stream_id"]),
                        )
                        for row in rows
                    )
                    cursor.execute(
                        """
                        SELECT projection_name, projection_version,
                               partition_sha256, global_position, state_sha256,
                               descriptor
                        FROM trace_backed_memory_v3_event_ledger.checkpoints
                        ORDER BY projection_name, projection_version,
                                 partition_sha256, global_position
                        """
                    )
                    for checkpoint in cursor.fetchall():
                        if (
                            not isinstance(checkpoint, Mapping)
                            or type(checkpoint.get("projection_name")) is not str
                            or _CHECKPOINT_NAME_RE.fullmatch(
                                cast(str, checkpoint["projection_name"])
                            )
                            is None
                            or type(checkpoint.get("projection_version")) is not int
                            or cast(int, checkpoint["projection_version"]) < 1
                            or not _valid_digest(
                                checkpoint.get("partition_sha256")
                            )
                            or type(checkpoint.get("global_position")) is not int
                            or not 0
                            <= checkpoint["global_position"]
                            <= len(all_events)
                            or not _valid_digest(checkpoint.get("state_sha256"))
                            or type(checkpoint.get("descriptor")) is not str
                        ):
                            raise _integrity_error(
                                "stored projection checkpoint has an invalid shape"
                            )
                        descriptor = cast(str, checkpoint["descriptor"])
                        try:
                            parsed_descriptor = json.loads(descriptor)
                        except (TypeError, json.JSONDecodeError) as error:
                            raise _integrity_error(
                                "stored projection checkpoint descriptor is invalid"
                            ) from error
                        if _canonical_json(parsed_descriptor) != descriptor:
                            raise _integrity_error(
                                "stored projection checkpoint descriptor is noncanonical"
                            )
                        retained_checkpoint = self._projection_checkpoint_from_row(
                            checkpoint
                        )
                        if (
                            retained_checkpoint.global_position
                            > len(all_events)
                        ):
                            raise _integrity_error(
                                "stored projection checkpoint is ahead of the ledger"
                            )
                    cursor.execute(
                        """
                        SELECT projection_name, partition_sha256, head_version,
                               target_build_id, previous_build_id,
                               activation_sha256, descriptor
                        FROM trace_backed_memory_v3_event_ledger.projection_activations
                        ORDER BY projection_name, partition_sha256, head_version
                        """
                    )
                    activation_heads: dict[
                        tuple[str, str], ProjectionActivation
                    ] = {}
                    for activation_row in cursor.fetchall():
                        activation = self._projection_activation_from_row(
                            activation_row
                        )
                        key = (
                            activation.projection_name,
                            activation.partition_sha256,
                        )
                        previous = activation_heads.get(key)
                        if (
                            activation.head_version
                            != (1 if previous is None else previous.head_version + 1)
                            or activation.previous_build_id
                            != (
                                None
                                if previous is None
                                else previous.target_build_id
                            )
                        ):
                            raise _integrity_error(
                                "stored projection activation chain is not contiguous"
                            )
                        cursor.execute(
                            """
                            SELECT projection_name, projection_version,
                                   partition_sha256, global_position,
                                   state_sha256, descriptor
                            FROM trace_backed_memory_v3_event_ledger.checkpoints
                            WHERE projection_name = %s AND partition_sha256 = %s
                            ORDER BY projection_version, global_position
                            LIMIT %s
                            """,
                            (
                                activation.projection_name,
                                activation.partition_sha256,
                                PROJECTION_MAX_CHECKPOINTS_PER_LIST + 1,
                            ),
                        )
                        target_count = sum(
                            1
                            for item in cursor.fetchall()
                            if self._projection_checkpoint_from_row(item).build_id
                            == activation.target_build_id
                        )
                        if target_count != 1:
                            raise _integrity_error(
                                "stored projection activation target is not retained exactly once"
                            )
                        activation_heads[key] = activation
                    return verifications
        except EventLedgerPortError:
            raise
        except Exception as error:
            self._raise_database_error(
                error,
                "PostgreSQL event ledger integrity verification failed",
            )

    @_synchronized
    def subscribe(
        self,
        after_position: int = 0,
        limit: int = 100,
        poll_timeout_seconds: int = 10,
    ) -> PostgresEventLedgerSubscription:
        self._require_open()
        request = LedgerSubscriptionRequest(
            self._access_context,
            after_position,
            limit,
            poll_timeout_seconds,
        )
        return PostgresEventLedgerSubscription(self, request)

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            try:
                self._connection.close()
            except Exception as error:
                raise _persistence_error(
                    "failed to close PostgreSQL event ledger"
                ) from error

    def __enter__(self) -> PostgresEventLedgerV1:
        self._require_open()
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()

    @staticmethod
    def _raise_database_error(error: Exception, message: str) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise EventLedgerConflictError(
                "TBM_EVENT_LEDGER_POSTGRES_CONFLICT",
                message,
            ) from error
        raise _persistence_error(message) from error

    @staticmethod
    def _raise_projection_database_error(
        error: Exception,
        message: str,
    ) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise _schema_error(_MISSING_SCHEMA_MESSAGE) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise ProjectionCheckpointConflictError(
                "TBM_PROJECTION_HEAD_CONFLICT",
                message,
            ) from error
        raise ProjectionCheckpointError(
            "TBM_PROJECTION_POSTGRES_PERSISTENCE",
            message,
        ) from error


class PostgresEventLedgerSubscription:
    """Bounded at-least-once polling cursor over a PostgreSQL ledger."""

    def __init__(
        self,
        ledger: PostgresEventLedgerV1,
        request: LedgerSubscriptionRequest,
    ) -> None:
        self._ledger = ledger
        self._request = request
        self._cursor = request.after_position
        self._outstanding: LedgerSubscriptionPage | None = None
        self._closed = False
        self._lock = RLock()
        self._subscription_id = f"subscription_{uuid4().hex}"

    def poll(self) -> LedgerSubscriptionPage:
        with self._lock:
            if self._closed:
                raise EventLedgerInvalidRequestError(
                    "TBM_EVENT_LEDGER_REQUEST_INVALID",
                    "event ledger subscription is closed",
                )
            if self._outstanding is not None:
                return self._outstanding
            page = self._ledger.read_global(self._cursor, self._request.limit)
            delivery = LedgerSubscriptionPage(
                subscription_id=self._subscription_id,
                delivery_id=f"delivery_{uuid4().hex}",
                page=page,
                heartbeat=not page.events,
            )
            self._outstanding = delivery
            return delivery

    def acknowledge(
        self,
        delivery_id: str,
        *,
        expected_next_global_position: int | None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise EventLedgerInvalidRequestError(
                    "TBM_EVENT_LEDGER_REQUEST_INVALID",
                    "event ledger subscription is closed",
                )
            outstanding = self._outstanding
            if (
                outstanding is None
                or type(delivery_id) is not str
                or delivery_id != outstanding.delivery_id
                or expected_next_global_position
                != outstanding.page.next_global_position
            ):
                raise EventLedgerConflictError(
                    "TBM_EVENT_LEDGER_SUBSCRIPTION_CONFLICT",
                    "subscription acknowledgement does not match delivery",
                )
            if outstanding.page.events:
                self._cursor = outstanding.page.events[-1].global_position
            self._outstanding = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._outstanding = None


__all__ = [
    "POSTGRES_EVENT_LEDGER_V1_ROLLBACK_RESOURCE",
    "POSTGRES_EVENT_LEDGER_V1_SCHEMA_RESOURCE",
    "POSTGRES_EVENT_LEDGER_V1_SCHEMA_VERSION",
    "PostgresEventLedgerSubscription",
    "PostgresEventLedgerV1",
    "PostgresEventLedgerV1Error",
    "PostgresEventLedgerV1IntegrityError",
    "PostgresEventLedgerV1PersistenceError",
    "PostgresEventLedgerV1SchemaError",
]
