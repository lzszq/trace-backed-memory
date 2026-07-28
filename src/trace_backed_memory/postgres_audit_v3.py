from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .audit_v3 import (
    AUDIT_JSON_MAX_BYTES,
    AUDIT_MAX_SEQUENCE,
    AuditContractError,
    AuditEvent,
    RecoveryAction,
    dumps_audit_event,
    dumps_recovery_action,
    loads_audit_event,
    loads_recovery_action,
    verify_audit_event_parent,
)
from .contracts_v3 import V3ContractError
from .postgres import _load_psycopg
from .resources import PackagedResourceError, read_packaged_resource
from .sqlite_audit_v3 import AuditStreamHead


POSTGRES_AUDIT_V3_SCHEMA_VERSION = 1
POSTGRES_AUDIT_V3_MAX_PAGE_SIZE = 1000
_SCHEMA = "trace_backed_memory_v3_audit"
_MISSING_SCHEMA_MESSAGE = "PostgreSQL audit v3 schema is missing or incomplete"
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_EXPECTED_RELATIONS = frozenset(
    {
        "audit_events",
        "audit_events_pkey",
        "audit_events_recovery_action_key",
        "audit_events_recovery_pair_key",
        "audit_events_session",
        "audit_events_stream_event_key",
        "audit_events_stream_sequence_key",
        "audit_events_type",
        "audit_schema_metadata_pkey",
        "audit_stream_heads",
        "audit_stream_heads_pkey",
        "recovery_actions",
        "recovery_actions_event_id_key",
        "recovery_actions_pkey",
        "recovery_actions_request_key",
        "recovery_actions_session",
        "schema_metadata",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {
        "reject_immutable_change",
        "validate_head_insert",
        "validate_event_insert",
        "validate_head_update",
        "validate_stream_consistency",
        "validate_recovery_pair",
    }
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "audit_schema_metadata_immutable",
        "audit_schema_metadata_no_truncate",
        "audit_stream_heads_initial",
        "audit_stream_heads_advance",
        "audit_stream_heads_immutable_delete",
        "audit_stream_heads_no_truncate",
        "audit_stream_heads_consistent",
        "audit_events_append",
        "audit_events_immutable",
        "audit_events_no_truncate",
        "audit_events_consistent",
        "recovery_actions_immutable",
        "recovery_actions_no_truncate",
        "recovery_actions_pair",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "audit_schema_metadata_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "audit_schema_metadata_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
        ("audit_stream_heads_initial", "audit_stream_heads", "validate_head_insert", 7),
        (
            "audit_stream_heads_advance",
            "audit_stream_heads",
            "validate_head_update",
            19,
        ),
        (
            "audit_stream_heads_immutable_delete",
            "audit_stream_heads",
            "reject_immutable_change",
            11,
        ),
        (
            "audit_stream_heads_no_truncate",
            "audit_stream_heads",
            "reject_immutable_change",
            34,
        ),
        (
            "audit_stream_heads_consistent",
            "audit_stream_heads",
            "validate_stream_consistency",
            21,
        ),
        ("audit_events_append", "audit_events", "validate_event_insert", 7),
        ("audit_events_immutable", "audit_events", "reject_immutable_change", 27),
        ("audit_events_no_truncate", "audit_events", "reject_immutable_change", 34),
        ("audit_events_consistent", "audit_events", "validate_stream_consistency", 5),
        (
            "recovery_actions_immutable",
            "recovery_actions",
            "reject_immutable_change",
            27,
        ),
        (
            "recovery_actions_no_truncate",
            "recovery_actions",
            "reject_immutable_change",
            34,
        ),
        ("recovery_actions_pair", "recovery_actions", "validate_recovery_pair", 5),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "audit_events_actor_id_check",
        "audit_events_actor_type_check",
        "audit_events_consistent",
        "audit_events_descriptor_check",
        "audit_events_event_id_check",
        "audit_events_event_type_check",
        "audit_events_occurred_at_check",
        "audit_events_parent_fkey",
        "audit_events_payload_sha256_check",
        "audit_events_pkey",
        "audit_events_previous_event_id_check",
        "audit_events_reason_code_check",
        "audit_events_recovery_action_fkey",
        "audit_events_recovery_action_id_check",
        "audit_events_recovery_action_key",
        "audit_events_recovery_pair_key",
        "audit_events_recovery_shape_check",
        "audit_events_repository_id_check",
        "audit_events_run_id_check",
        "audit_events_sequence_check",
        "audit_events_session_id_check",
        "audit_events_stream_event_key",
        "audit_events_stream_fkey",
        "audit_events_stream_sequence_key",
        "audit_events_tenant_id_check",
        "audit_events_trace_id_check",
        "audit_schema_metadata_contract_version_check",
        "audit_schema_metadata_pkey",
        "audit_schema_metadata_schema_version_check",
        "audit_schema_metadata_singleton_check",
        "audit_stream_heads_consistent",
        "audit_stream_heads_event_id_check",
        "audit_stream_heads_pkey",
        "audit_stream_heads_repository_id_check",
        "audit_stream_heads_run_id_check",
        "audit_stream_heads_sequence_check",
        "audit_stream_heads_session_id_check",
        "audit_stream_heads_shape_check",
        "audit_stream_heads_stream_id_check",
        "audit_stream_heads_tenant_id_check",
        "audit_stream_heads_trace_id_check",
        "recovery_actions_descriptor_check",
        "recovery_actions_event_fkey",
        "recovery_actions_event_id_key",
        "recovery_actions_executor_id_check",
        "recovery_actions_finished_at_check",
        "recovery_actions_id_check",
        "recovery_actions_pair",
        "recovery_actions_pkey",
        "recovery_actions_request_key",
        "recovery_actions_request_sha256_check",
        "recovery_actions_result_check",
        "recovery_actions_run_id_check",
        "recovery_actions_session_id_check",
        "recovery_actions_trace_id_check",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("audit_events", "event_id", "text", "NO", "C"),
        ("audit_events", "stream_id", "text", "NO", "C"),
        ("audit_events", "sequence", "integer", "NO", None),
        ("audit_events", "previous_event_id", "text", "YES", "C"),
        ("audit_events", "tenant_id", "text", "NO", "C"),
        ("audit_events", "repository_id", "text", "NO", "C"),
        ("audit_events", "session_id", "text", "NO", "C"),
        ("audit_events", "trace_id", "text", "NO", "C"),
        ("audit_events", "run_id", "text", "NO", "C"),
        ("audit_events", "actor_type", "text", "NO", "C"),
        ("audit_events", "actor_id", "text", "NO", "C"),
        ("audit_events", "event_type", "text", "NO", "C"),
        ("audit_events", "recovery_action_id", "text", "YES", "C"),
        ("audit_events", "reason_code", "text", "NO", "C"),
        ("audit_events", "payload_sha256", "text", "NO", "C"),
        ("audit_events", "occurred_at", "text", "NO", "C"),
        ("audit_events", "descriptor", "text", "NO", "C"),
        ("audit_stream_heads", "stream_id", "text", "NO", "C"),
        ("audit_stream_heads", "tenant_id", "text", "NO", "C"),
        ("audit_stream_heads", "repository_id", "text", "NO", "C"),
        ("audit_stream_heads", "session_id", "text", "NO", "C"),
        ("audit_stream_heads", "trace_id", "text", "NO", "C"),
        ("audit_stream_heads", "run_id", "text", "NO", "C"),
        ("audit_stream_heads", "current_sequence", "integer", "NO", None),
        ("audit_stream_heads", "current_event_id", "text", "YES", "C"),
        ("recovery_actions", "recovery_action_id", "text", "NO", "C"),
        ("recovery_actions", "event_id", "text", "NO", "C"),
        ("recovery_actions", "session_id", "text", "NO", "C"),
        ("recovery_actions", "trace_id", "text", "NO", "C"),
        ("recovery_actions", "run_id", "text", "NO", "C"),
        ("recovery_actions", "result", "text", "NO", "C"),
        ("recovery_actions", "executor_id", "text", "NO", "C"),
        ("recovery_actions", "request_sha256", "text", "NO", "C"),
        ("recovery_actions", "finished_at", "text", "NO", "C"),
        ("recovery_actions", "descriptor", "text", "NO", "C"),
        ("schema_metadata", "singleton", "boolean", "NO", None),
        ("schema_metadata", "schema_version", "integer", "NO", None),
        ("schema_metadata", "contract_version", "text", "NO", "C"),
    }
)
_EXPECTED_CATALOG_SHA256 = (
    "96c8c201f2a6d7431d1fe547634496a4d458cd7ba0a4b2003a6393d3995a1d41"
)
_CATALOG_SHA256_QUERY = """
WITH descriptors AS (
    SELECT 'schema|' || namespace.nspname || '|' ||
           pg_catalog.has_schema_privilege(
               'public', namespace.oid, 'USAGE'
           )::text || '|' ||
           pg_catalog.has_schema_privilege(
               'public', namespace.oid, 'CREATE'
           )::text AS descriptor
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = %s
    UNION ALL
    SELECT 'relation|' || class.relname || '|' || class.relkind::text || '|' ||
           class.relpersistence::text || '|' ||
           COALESCE(access_method.amname, '-') || '|' ||
           class.relrowsecurity::text || '|' ||
           class.relforcerowsecurity::text || '|' ||
           class.relreplident::text || '|' || class.relispartition::text || '|' ||
           (class.relowner = namespace.nspowner)::text || '|' ||
           CASE WHEN class.relkind IN ('r', 'p') THEN
               pg_catalog.has_table_privilege(
                   'public', class.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )::text
           ELSE 'false' END || '|' ||
           COALESCE(pg_catalog.array_to_string(class.reloptions, ','), '-')
    FROM pg_catalog.pg_class AS class
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    LEFT JOIN pg_catalog.pg_am AS access_method
      ON access_method.oid = class.relam
    WHERE namespace.nspname = %s
      AND class.relkind IN ('r', 'i', 'p')
    UNION ALL
    SELECT 'column|' || class.relname || '|' || attribute.attname || '|' ||
           attribute.attnum::text || '|' ||
           pg_catalog.format_type(
               attribute.atttypid, attribute.atttypmod
           ) || '|' ||
           attribute.attnotnull::text || '|' ||
           attribute.attidentity::text || '|' ||
           attribute.attgenerated::text || '|' ||
           attribute.attstorage::text || '|' ||
           attribute.attcompression::text || '|' ||
           COALESCE(
               collation_namespace.nspname || '.' || collation_record.collname,
               '-'
           ) ||
           '|' ||
           COALESCE(
               pg_catalog.pg_get_expr(default_record.adbin, default_record.adrelid),
               '-'
           )
    FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS class ON class.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    LEFT JOIN pg_catalog.pg_attrdef AS default_record
      ON default_record.adrelid = attribute.attrelid
     AND default_record.adnum = attribute.attnum
    LEFT JOIN pg_catalog.pg_collation AS collation_record
      ON collation_record.oid = attribute.attcollation
    LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
      ON collation_namespace.oid = collation_record.collnamespace
    WHERE namespace.nspname = %s
      AND class.relkind IN ('r', 'p')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
    UNION ALL
    SELECT 'constraint|' || constraint_record.conname || '|' ||
           constraint_record.contype::text || '|' ||
           constraint_record.condeferrable::text || '|' ||
           constraint_record.condeferred::text || '|' ||
           constraint_record.convalidated::text || '|' ||
           constraint_record.conislocal::text || '|' ||
           constraint_record.coninhcount::text || '|' ||
           constraint_record.connoinherit::text || '|' ||
           constraint_record.confupdtype::text || '|' ||
           constraint_record.confdeltype::text || '|' ||
           constraint_record.confmatchtype::text || '|' ||
           pg_catalog.replace(
               pg_catalog.pg_get_constraintdef(constraint_record.oid, true),
               'trace_backed_memory_v3_audit.',
               ''
           )
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = constraint_record.connamespace
    WHERE namespace.nspname = %s
      AND constraint_record.contype <> 'n'
    UNION ALL
    SELECT 'index|' || index_class.relname || '|' ||
           index_record.indisunique::text || '|' ||
           index_record.indisprimary::text || '|' ||
           index_record.indisexclusion::text || '|' ||
           index_record.indimmediate::text || '|' ||
           index_record.indisvalid::text || '|' ||
           index_record.indisready::text || '|' ||
           index_record.indislive::text || '|' ||
           index_record.indisreplident::text || '|' ||
           index_record.indnkeyatts::text || '|' ||
           index_record.indnatts::text || '|' ||
           pg_catalog.replace(
               pg_catalog.pg_get_indexdef(index_record.indexrelid),
               'trace_backed_memory_v3_audit.',
               ''
           )
    FROM pg_catalog.pg_index AS index_record
    JOIN pg_catalog.pg_class AS index_class
      ON index_class.oid = index_record.indexrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = index_class.relnamespace
    WHERE namespace.nspname = %s
    UNION ALL
    SELECT 'function|' || procedure.proname || '|' ||
           procedure.prokind::text || '|' || language.lanname || '|' ||
           procedure.prorettype::pg_catalog.regtype::text || '|' ||
           procedure.prosecdef::text || '|' ||
           procedure.proleakproof::text || '|' ||
           procedure.proisstrict::text || '|' ||
           procedure.provolatile::text || '|' ||
           procedure.proparallel::text || '|' ||
           procedure.pronargs::text || '|' ||
           procedure.pronargdefaults::text || '|' ||
           procedure.proargtypes::text || '|' ||
           COALESCE(procedure.proallargtypes::text, '-') || '|' ||
           COALESCE(procedure.proargmodes::text, '-') || '|' ||
           COALESCE(procedure.proargnames::text, '-') || '|' ||
           pg_catalog.pg_get_function_identity_arguments(procedure.oid) || '|' ||
           (procedure.proowner = namespace.nspowner)::text || '|' ||
           pg_catalog.has_function_privilege(
               'public', procedure.oid, 'EXECUTE'
           )::text || '|' ||
           COALESCE(
               pg_catalog.array_to_string(procedure.proconfig, ','),
               '-'
           ) || '|' ||
           pg_catalog.replace(
               pg_catalog.btrim(procedure.prosrc),
               E'\\r\\n',
               E'\\n'
           )
    FROM pg_catalog.pg_proc AS procedure
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = procedure.pronamespace
    JOIN pg_catalog.pg_language AS language
      ON language.oid = procedure.prolang
    WHERE namespace.nspname = %s
    UNION ALL
    SELECT 'trigger|' || trigger.tgname || '|' || class.relname || '|' ||
           function_namespace.nspname || '.' || procedure.proname || '|' ||
           trigger.tgtype::text || '|' || trigger.tgenabled::text || '|' ||
           trigger.tgdeferrable::text || '|' ||
           trigger.tginitdeferred::text || '|' ||
           pg_catalog.encode(trigger.tgargs, 'hex') || '|' ||
           COALESCE(
               pg_catalog.replace(
                   pg_catalog.pg_get_expr(trigger.tgqual, trigger.tgrelid),
                   'trace_backed_memory_v3_audit.',
                   ''
               ),
               '-'
           )
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    JOIN pg_catalog.pg_proc AS procedure
      ON procedure.oid = trigger.tgfoid
    JOIN pg_catalog.pg_namespace AS function_namespace
      ON function_namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = %s
      AND NOT trigger.tgisinternal
)
SELECT pg_catalog.encode(
    pg_catalog.sha256(
        pg_catalog.convert_to(
            pg_catalog.string_agg(descriptor, E'\\n' ORDER BY descriptor),
            'UTF8'
        )
    ),
    'hex'
) AS catalog_sha256
FROM descriptors
"""
_FUNCTION_BODY_PATTERN = re.compile(
    r"CREATE FUNCTION\s+"
    r"trace_backed_memory_v3_audit\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


@lru_cache(maxsize=1)
def _expected_function_bodies() -> dict[str, str]:
    try:
        source = read_packaged_resource("schemas/postgres-v3-audit.sql").decode("utf-8")
    except (PackagedResourceError, UnicodeError) as error:
        raise PostgresAuditV3SchemaError(
            "TBM_POSTGRES_AUDIT_SCHEMA",
            "could not read canonical PostgreSQL audit schema",
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresAuditV3SchemaError(
            "TBM_POSTGRES_AUDIT_SCHEMA",
            "canonical PostgreSQL audit functions are incomplete",
        )
    return bodies


class PostgresAuditV3Error(V3ContractError):
    """Stable base failure for the isolated PostgreSQL audit ledger."""


class PostgresAuditV3SchemaError(PostgresAuditV3Error):
    pass


class PostgresAuditV3ConflictError(PostgresAuditV3Error):
    pass


class PostgresAuditV3PersistenceError(PostgresAuditV3Error):
    pass


@dataclass(frozen=True)
class PostgresAuditV3AppendResult:
    event_id: str
    event_inserted: bool
    recovery_action_id: str
    recovery_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


class PostgresAuditV3Repository:
    """Opt-in immutable PostgreSQL audit and recovery evidence ledger."""

    def __init__(self, connection: object, *, owns_connection: bool = False) -> None:
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False
        self._lock = RLock()

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresAuditV3Repository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "failed to connect to PostgreSQL",
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_CLOSED",
                "PostgreSQL audit repository is closed",
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
            PostgresAuditV3Repository._schema_drift()
        return frozenset(cast(str, row["name"]) for row in rows)

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            """
            SELECT active.schema_version AS active_schema_version,
                   audit.schema_version AS audit_schema_version,
                   audit.contract_version AS contract_version
            FROM public.trace_backed_memory_schema AS active
            CROSS JOIN trace_backed_memory_v3_audit.schema_metadata AS audit
            WHERE active.singleton AND audit.singleton
            FOR SHARE OF active, audit
            """
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or rows[0].get("active_schema_version") != 2
            or rows[0].get("audit_schema_version") != 1
            or rows[0].get("contract_version") != "tbm.audit-event.v3"
        ):
            raise PostgresAuditV3SchemaError(
                "TBM_POSTGRES_AUDIT_SCHEMA",
                "PostgreSQL audit schema metadata mismatch",
            )
        relations = self._names(
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
        triggers = self._names(
            cursor,
            """
            SELECT trigger.tgname AS name
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = %s AND NOT trigger.tgisinternal
            """,
        )
        constraints = self._names(
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
        if (
            relations != _EXPECTED_RELATIONS
            or functions != _EXPECTED_FUNCTIONS
            or triggers != _EXPECTED_TRIGGERS
            or constraints != _EXPECTED_CONSTRAINTS
        ):
            self._schema_drift()
        cursor.execute(
            """
            SELECT trigger.tgname, relation.relname AS table_name,
                   procedure.proname AS function_name,
                   function_namespace.nspname AS function_schema,
                   trigger.tgenabled, trigger.tgtype
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
                if isinstance(row, Mapping) and row.get("function_schema") == _SCHEMA
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if (
            any(
                not isinstance(row, Mapping) or row.get("tgenabled") != "O"
                for row in trigger_rows
            )
            or len(trigger_shapes) != len(trigger_rows)
            or trigger_shapes != _EXPECTED_TRIGGER_SHAPES
        ):
            self._schema_drift()
        cursor.execute(
            """
            SELECT procedure.proname AS name, procedure.prosrc AS body,
                   language.lanname AS language,
                   procedure.prorettype::pg_catalog.regtype::text
                       AS return_type,
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
        function_rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping)
            or type(row.get("name")) is not str
            or type(row.get("body")) is not str
            or row.get("language") != "plpgsql"
            or row.get("return_type") != "trigger"
            or row.get("proconfig") != ["search_path=pg_catalog"]
            for row in function_rows
        ):
            self._schema_drift()
        stored_bodies = {
            cast(str, row["name"]): cast(str, row["body"]).replace("\r\n", "\n").strip()
            for row in function_rows
        }
        if stored_bodies != _expected_function_bodies():
            self._schema_drift()
        cursor.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable,
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
                if isinstance(row, Mapping)
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if len(columns) != len(column_rows) or columns != _EXPECTED_COLUMNS:
            self._schema_drift()
        cursor.execute(_CATALOG_SHA256_QUERY, (_SCHEMA,) * 7)
        fingerprint_rows = cursor.fetchall()
        if (
            len(fingerprint_rows) != 1
            or not isinstance(fingerprint_rows[0], Mapping)
            or fingerprint_rows[0].get("catalog_sha256") != _EXPECTED_CATALOG_SHA256
        ):
            self._schema_drift()

    @staticmethod
    def _schema_drift() -> NoReturn:
        raise PostgresAuditV3SchemaError(
            "TBM_POSTGRES_AUDIT_SCHEMA",
            "PostgreSQL audit schema definitions do not match",
        )

    @staticmethod
    def _mapping_values(
        row: Mapping[str, object],
        fields: tuple[str, ...],
    ) -> tuple[object, ...]:
        try:
            return tuple(row[field] for field in fields)
        except KeyError as error:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit row has an invalid shape",
            ) from error

    @staticmethod
    def _event_values(event: AuditEvent) -> tuple[object, ...]:
        recovery_ids = tuple(
            reference.record_id
            for reference in event.references
            if reference.kind == "recovery_action"
        )
        recovery_action_id = recovery_ids[0] if len(recovery_ids) == 1 else None
        return (
            event.event_id,
            event.stream_id,
            event.sequence,
            event.previous_event_id,
            event.tenant_id,
            event.repository_id,
            event.session_id,
            event.trace_id,
            event.run_id,
            event.actor_type,
            event.actor_id,
            event.event_type,
            recovery_action_id,
            event.reason_code,
            event.payload_sha256,
            event.occurred_at,
            dumps_audit_event(event),
        )

    @classmethod
    def _stored_event(cls, row: Mapping[str, object]) -> AuditEvent:
        fields = (
            "event_id",
            "stream_id",
            "sequence",
            "previous_event_id",
            "tenant_id",
            "repository_id",
            "session_id",
            "trace_id",
            "run_id",
            "actor_type",
            "actor_id",
            "event_type",
            "recovery_action_id",
            "reason_code",
            "payload_sha256",
            "occurred_at",
            "descriptor",
        )
        values = cls._mapping_values(row, fields)
        if type(values[-1]) is not str:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit event descriptor has an invalid shape",
            )
        try:
            event = loads_audit_event(cast(str, values[-1]))
        except AuditContractError as error:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit event descriptor failed validation",
            ) from error
        if values != cls._event_values(event):
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit event columns do not match descriptor",
            )
        return event

    @staticmethod
    def _recovery_values(
        recovery: RecoveryAction,
        event_id: str,
    ) -> tuple[object, ...]:
        return (
            recovery.recovery_action_id,
            event_id,
            recovery.session_id,
            recovery.trace_id,
            recovery.run_id,
            recovery.result,
            recovery.executor_id,
            recovery.request_sha256,
            recovery.finished_at,
            dumps_recovery_action(recovery),
        )

    @classmethod
    def _stored_recovery(
        cls,
        row: Mapping[str, object],
    ) -> tuple[RecoveryAction, str]:
        fields = (
            "recovery_action_id",
            "event_id",
            "session_id",
            "trace_id",
            "run_id",
            "result",
            "executor_id",
            "request_sha256",
            "finished_at",
            "descriptor",
        )
        values = cls._mapping_values(row, fields)
        if type(values[-1]) is not str or type(values[1]) is not str:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL recovery action row has an invalid shape",
            )
        try:
            recovery = loads_recovery_action(cast(str, values[-1]))
        except AuditContractError as error:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL recovery action descriptor failed validation",
            ) from error
        event_id = cast(str, values[1])
        if values != cls._recovery_values(recovery, event_id):
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL recovery action columns do not match descriptor",
            )
        return recovery, event_id

    @staticmethod
    def _event_select() -> str:
        return (
            "event_id, stream_id, sequence, previous_event_id, tenant_id, "
            "repository_id, session_id, trace_id, run_id, actor_type, "
            "actor_id, event_type, recovery_action_id, reason_code, "
            "payload_sha256, occurred_at, descriptor"
        )

    @staticmethod
    def _validate_event_id(event_id: object) -> str:
        if (
            type(event_id) is not str
            or re.fullmatch(r"audit_event_sha256_[0-9a-f]{64}", event_id) is None
        ):
            raise ValueError("event_id must be a canonical audit event ID")
        return event_id

    @staticmethod
    def _validate_recovery_id(recovery_action_id: object) -> str:
        if (
            type(recovery_action_id) is not str
            or re.fullmatch(
                r"recovery_action_sha256_[0-9a-f]{64}",
                recovery_action_id,
            )
            is None
        ):
            raise ValueError(
                "recovery_action_id must be a canonical recovery action ID"
            )
        return recovery_action_id

    @staticmethod
    def _validate_identifier(value: object, name: str) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"{name} must be a bounded identifier")
        return value

    def _select_event(
        self,
        cursor: object,
        event_id: str,
    ) -> AuditEvent | None:
        cursor.execute(
            f"""
            SELECT {self._event_select()}
            FROM trace_backed_memory_v3_audit.audit_events
            WHERE event_id = %s
            """,
            (event_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit event row has an invalid shape",
            )
        return self._stored_event(rows[0])

    def _select_head(
        self,
        cursor: object,
        stream_id: str,
        *,
        for_update: bool,
    ) -> Mapping[str, object] | None:
        cursor.execute(
            """
            SELECT stream_id, tenant_id, repository_id, session_id, trace_id,
                   run_id, current_sequence, current_event_id
            FROM trace_backed_memory_v3_audit.audit_stream_heads
            WHERE stream_id = %s
            """
            + (" FOR UPDATE" if for_update else ""),
            (stream_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit stream head has an invalid shape",
            )
        row = rows[0]
        fields = (
            "stream_id",
            "tenant_id",
            "repository_id",
            "session_id",
            "trace_id",
            "run_id",
            "current_sequence",
            "current_event_id",
        )
        self._mapping_values(row, fields)
        return row

    @staticmethod
    def _validate_recovery_event(
        recovery: RecoveryAction,
        event: AuditEvent,
    ) -> None:
        expected_type = (
            "recovery_succeeded"
            if recovery.result == "succeeded"
            else "recovery_failed"
        )
        references = tuple(
            reference.record_id
            for reference in event.references
            if reference.kind == "recovery_action"
        )
        from ._timestamps import parse_rfc3339

        if (
            event.event_type != expected_type
            or references != (recovery.recovery_action_id,)
            or event.actor_id != recovery.executor_id
            or event.session_id != recovery.session_id
            or event.trace_id != recovery.trace_id
            or event.run_id != recovery.run_id
            or parse_rfc3339(event.occurred_at) < parse_rfc3339(recovery.finished_at)
        ):
            raise PostgresAuditV3ConflictError(
                "TBM_POSTGRES_AUDIT_CONFLICT",
                "recovery action and audit event linkage differs",
            )

    def _put_event(self, cursor: object, event: AuditEvent) -> bool:
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_audit.audit_stream_heads (
                stream_id, tenant_id, repository_id, session_id, trace_id,
                run_id, current_sequence, current_event_id
            ) VALUES (%s, %s, %s, %s, %s, %s, 0, NULL)
            ON CONFLICT (stream_id) DO NOTHING
            """,
            (
                event.stream_id,
                event.tenant_id,
                event.repository_id,
                event.session_id,
                event.trace_id,
                event.run_id,
            ),
        )
        head = self._select_head(
            cursor,
            event.stream_id,
            for_update=True,
        )
        if head is None:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit stream head disappeared",
            )
        existing = self._select_event(cursor, event.event_id)
        if existing is not None:
            if self._event_values(existing) != self._event_values(event):
                raise PostgresAuditV3ConflictError(
                    "TBM_POSTGRES_AUDIT_CONFLICT",
                    "audit event ID has conflicting immutable content",
                )
            return False
        identity = (
            "tenant_id",
            "repository_id",
            "session_id",
            "trace_id",
            "run_id",
        )
        if any(head[name] != getattr(event, name) for name in identity):
            raise PostgresAuditV3ConflictError(
                "TBM_POSTGRES_AUDIT_CONFLICT",
                "audit event identity differs from stream head",
            )
        sequence = head["current_sequence"]
        current_event_id = head["current_event_id"]
        if type(sequence) is not int or not 0 <= sequence < AUDIT_MAX_SEQUENCE:
            raise PostgresAuditV3PersistenceError(
                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                "PostgreSQL audit stream sequence has an invalid shape",
            )
        if sequence == 0:
            if event.sequence != 1 or event.previous_event_id is not None:
                raise PostgresAuditV3ConflictError(
                    "TBM_POSTGRES_AUDIT_CONFLICT",
                    "audit stream must begin at sequence one",
                )
        else:
            if type(current_event_id) is not str:
                raise PostgresAuditV3PersistenceError(
                    "TBM_POSTGRES_AUDIT_PERSISTENCE",
                    "PostgreSQL audit stream event ID has an invalid shape",
                )
            parent = self._select_event(cursor, current_event_id)
            if parent is None:
                raise PostgresAuditV3PersistenceError(
                    "TBM_POSTGRES_AUDIT_PERSISTENCE",
                    "PostgreSQL audit stream head references a missing event",
                )
            try:
                verify_audit_event_parent(event, parent)
            except AuditContractError as error:
                raise PostgresAuditV3ConflictError(
                    "TBM_POSTGRES_AUDIT_CONFLICT",
                    "audit event does not extend the current stream",
                ) from error
        descriptor = dumps_audit_event(event)
        if len(descriptor.encode()) > AUDIT_JSON_MAX_BYTES:
            raise ValueError("audit event descriptor exceeds storage limit")
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_audit.audit_events (
                event_id, stream_id, sequence, previous_event_id, tenant_id,
                repository_id, session_id, trace_id, run_id, actor_type,
                actor_id, event_type, recovery_action_id, reason_code,
                payload_sha256, occurred_at, descriptor
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            """,
            self._event_values(event),
        )
        cursor.execute(
            """
            UPDATE trace_backed_memory_v3_audit.audit_stream_heads
            SET current_sequence = %s, current_event_id = %s
            WHERE stream_id = %s
              AND current_sequence = %s
              AND current_event_id IS NOT DISTINCT FROM %s
            """,
            (
                event.sequence,
                event.event_id,
                event.stream_id,
                sequence,
                current_event_id,
            ),
        )
        if cursor.rowcount != 1:
            raise PostgresAuditV3ConflictError(
                "TBM_POSTGRES_AUDIT_CONFLICT",
                "audit stream changed during append",
            )
        return True

    def _put_recovery(
        self,
        cursor: object,
        recovery: RecoveryAction,
        event: AuditEvent,
    ) -> bool:
        cursor.execute(
            """
            SELECT recovery_action_id, event_id, session_id, trace_id, run_id,
                   result, executor_id, request_sha256, finished_at, descriptor
            FROM trace_backed_memory_v3_audit.recovery_actions
            WHERE recovery_action_id = %s
            """,
            (recovery.recovery_action_id,),
        )
        rows = cursor.fetchall()
        expected = self._recovery_values(recovery, event.event_id)
        if rows:
            if len(rows) != 1 or not isinstance(rows[0], Mapping):
                raise PostgresAuditV3PersistenceError(
                    "TBM_POSTGRES_AUDIT_PERSISTENCE",
                    "PostgreSQL recovery action row has an invalid shape",
                )
            stored, event_id = self._stored_recovery(rows[0])
            if self._recovery_values(stored, event_id) != expected:
                raise PostgresAuditV3ConflictError(
                    "TBM_POSTGRES_AUDIT_CONFLICT",
                    "recovery action ID has conflicting immutable content",
                )
            return False
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_audit.recovery_actions (
                recovery_action_id, event_id, session_id, trace_id, run_id,
                result, executor_id, request_sha256, finished_at, descriptor
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            expected,
        )
        return True

    @_synchronized
    def append_event(self, event: AuditEvent) -> bool:
        self._require_open()
        if type(event) is not AuditEvent:
            raise ValueError("event must be exactly AuditEvent")
        if event.event_type in {"recovery_succeeded", "recovery_failed"}:
            raise ValueError("recovery events must be appended with append_recovery")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._put_event(cursor, event)
        except (PostgresAuditV3ConflictError, PostgresAuditV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to append audit event")

    @_synchronized
    def append_recovery(
        self,
        recovery: RecoveryAction,
        event: AuditEvent,
    ) -> PostgresAuditV3AppendResult:
        self._require_open()
        if type(recovery) is not RecoveryAction or type(event) is not AuditEvent:
            raise ValueError("recovery and event must be exact audit records")
        self._validate_recovery_event(recovery, event)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    event_inserted = self._put_event(cursor, event)
                    recovery_inserted = self._put_recovery(
                        cursor,
                        recovery,
                        event,
                    )
            return PostgresAuditV3AppendResult(
                event_id=event.event_id,
                event_inserted=event_inserted,
                recovery_action_id=recovery.recovery_action_id,
                recovery_inserted=recovery_inserted,
            )
        except (PostgresAuditV3ConflictError, PostgresAuditV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to append recovery evidence",
            )

    @_synchronized
    def load_event(self, event_id: str) -> AuditEvent:
        self._require_open()
        self._validate_event_id(event_id)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    event = self._select_event(cursor, event_id)
                    if event is None:
                        raise KeyError(event_id)
                    return event
        except (KeyError, PostgresAuditV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to load audit event")

    @_synchronized
    def load_recovery(
        self,
        recovery_action_id: str,
    ) -> tuple[RecoveryAction, AuditEvent]:
        self._require_open()
        self._validate_recovery_id(recovery_action_id)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        """
                        SELECT recovery_action_id, event_id, session_id,
                               trace_id, run_id, result, executor_id,
                               request_sha256, finished_at, descriptor
                        FROM trace_backed_memory_v3_audit.recovery_actions
                        WHERE recovery_action_id = %s
                        """,
                        (recovery_action_id,),
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise KeyError(recovery_action_id)
                    if len(rows) != 1 or not isinstance(rows[0], Mapping):
                        raise PostgresAuditV3PersistenceError(
                            "TBM_POSTGRES_AUDIT_PERSISTENCE",
                            "PostgreSQL recovery row has an invalid shape",
                        )
                    recovery, event_id = self._stored_recovery(rows[0])
                    event = self._select_event(cursor, event_id)
                    if event is None:
                        raise PostgresAuditV3PersistenceError(
                            "TBM_POSTGRES_AUDIT_PERSISTENCE",
                            "recovery action references a missing event",
                        )
                    try:
                        self._validate_recovery_event(recovery, event)
                    except PostgresAuditV3ConflictError as error:
                        raise PostgresAuditV3PersistenceError(
                            "TBM_POSTGRES_AUDIT_PERSISTENCE",
                            "recovery action linkage failed validation",
                        ) from error
                    return recovery, event
        except (KeyError, PostgresAuditV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load recovery evidence",
            )

    @_synchronized
    def stream_head(self, stream_id: str) -> AuditStreamHead | None:
        self._require_open()
        self._validate_identifier(stream_id, "stream_id")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    row = self._select_head(
                        cursor,
                        stream_id,
                        for_update=False,
                    )
                    if row is None:
                        return None
                    sequence = row["current_sequence"]
                    event_id = row["current_event_id"]
                    if (
                        type(sequence) is not int
                        or not 1 <= sequence <= AUDIT_MAX_SEQUENCE
                        or type(event_id) is not str
                    ):
                        raise PostgresAuditV3PersistenceError(
                            "TBM_POSTGRES_AUDIT_PERSISTENCE",
                            "PostgreSQL audit stream head has an invalid shape",
                        )
                    event = self._select_event(cursor, event_id)
                    if (
                        event is None
                        or event.stream_id != row["stream_id"]
                        or event.sequence != sequence
                        or any(
                            getattr(event, name) != row[name]
                            for name in (
                                "tenant_id",
                                "repository_id",
                                "session_id",
                                "trace_id",
                                "run_id",
                            )
                        )
                    ):
                        raise PostgresAuditV3PersistenceError(
                            "TBM_POSTGRES_AUDIT_PERSISTENCE",
                            "audit stream head does not match current event",
                        )
                    return AuditStreamHead(
                        stream_id=cast(str, row["stream_id"]),
                        tenant_id=cast(str, row["tenant_id"]),
                        repository_id=cast(str, row["repository_id"]),
                        session_id=cast(str, row["session_id"]),
                        trace_id=cast(str, row["trace_id"]),
                        run_id=cast(str, row["run_id"]),
                        current_sequence=sequence,
                        current_event_id=event_id,
                    )
        except PostgresAuditV3SchemaError:
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to load stream head")

    @_synchronized
    def list_events(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        self._require_open()
        self._validate_identifier(stream_id, "stream_id")
        if (
            type(after_sequence) is not int
            or not 0 <= after_sequence <= AUDIT_MAX_SEQUENCE
        ):
            raise ValueError("after_sequence must be a bounded integer")
        if type(limit) is not int or not 1 <= limit <= POSTGRES_AUDIT_V3_MAX_PAGE_SIZE:
            raise ValueError(
                f"limit must be between 1 and {POSTGRES_AUDIT_V3_MAX_PAGE_SIZE}"
            )
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        f"""
                        SELECT {self._event_select()}
                        FROM trace_backed_memory_v3_audit.audit_events
                        WHERE stream_id = %s AND sequence > %s
                        ORDER BY sequence
                        LIMIT %s
                        """,
                        (stream_id, after_sequence, limit),
                    )
                    rows = cursor.fetchall()
                    if any(not isinstance(row, Mapping) for row in rows):
                        raise PostgresAuditV3PersistenceError(
                            "TBM_POSTGRES_AUDIT_PERSISTENCE",
                            "PostgreSQL audit event row has an invalid shape",
                        )
                    events = tuple(
                        self._stored_event(cast(Mapping[str, object], row))
                        for row in rows
                    )
                    parent: AuditEvent | None = None
                    if events and events[0].sequence > 1:
                        cursor.execute(
                            f"""
                            SELECT {self._event_select()}
                            FROM trace_backed_memory_v3_audit.audit_events
                            WHERE stream_id = %s AND sequence = %s
                            """,
                            (stream_id, events[0].sequence - 1),
                        )
                        parent_rows = cursor.fetchall()
                        if len(parent_rows) != 1 or not isinstance(
                            parent_rows[0], Mapping
                        ):
                            raise PostgresAuditV3PersistenceError(
                                "TBM_POSTGRES_AUDIT_PERSISTENCE",
                                "audit stream contains a sequence gap",
                            )
                        parent = self._stored_event(parent_rows[0])
                    for event in events:
                        if parent is not None:
                            try:
                                verify_audit_event_parent(event, parent)
                            except AuditContractError as error:
                                raise PostgresAuditV3PersistenceError(
                                    "TBM_POSTGRES_AUDIT_PERSISTENCE",
                                    "audit stream failed parent validation",
                                ) from error
                        parent = event
                    return events
        except PostgresAuditV3SchemaError:
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to list audit events")

    @staticmethod
    def _raise_database_error(
        error: BaseException,
        message: str,
    ) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresAuditV3SchemaError(
                "TBM_POSTGRES_AUDIT_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        if type(sqlstate) is str and (sqlstate.startswith("23") or sqlstate == "P0001"):
            raise PostgresAuditV3ConflictError(
                "TBM_POSTGRES_AUDIT_CONFLICT",
                message,
            ) from error
        raise PostgresAuditV3PersistenceError(
            "TBM_POSTGRES_AUDIT_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresAuditV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
