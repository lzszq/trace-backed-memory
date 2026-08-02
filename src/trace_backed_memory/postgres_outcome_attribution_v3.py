from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, wraps
import json
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar

from .contracts_v3 import V3ContractError
from .gate_session_v3 import GateSession
from .ledger_port_v1 import (
    EventLedgerPortError,
    LedgerAppendRequest,
    LedgerIdempotency,
)
from .outcome_event_v1 import (
    OUTCOME_ATTRIBUTION_PROPOSED_EVENT,
    OutcomeEventV1Error,
    build_outcome_attribution_event_batch,
    outcome_attribution_event_stream_id,
    parse_outcome_attribution_proposed_event,
    parse_outcome_attribution_verified_event,
    parse_run_outcome_recorded_event,
    run_outcome_event_stream_id,
)
from .outcome_v3 import (
    OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
    OutcomeAttribution,
    OutcomeContractError,
    RunOutcome,
    dumps_outcome_attribution,
    loads_outcome_attribution,
    verify_outcome_attribution,
)
from .postgres import _load_psycopg
from .postgres_gate_session_v3 import (
    PostgresGateSessionNotFoundError,
    PostgresGateSessionPersistenceError,
    PostgresGateSessionRepository,
    PostgresGateSessionSchemaError,
)
from .postgres_outcome_v3 import (
    PostgresOutcomeV3NotFoundError,
    PostgresOutcomeV3PersistenceError,
    PostgresOutcomeV3Repository,
    PostgresOutcomeV3SchemaError,
)
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION = 1
_SCHEMA = "trace_backed_memory_v3_outcome_attribution"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL OutcomeAttribution v3 schema is missing or incomplete"
)
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_EXPECTED_RELATIONS = frozenset(
    {
        "outcome_attributions",
        "outcome_attributions_by_outcome",
        "outcome_attributions_pkey",
        "schema_metadata",
        "schema_metadata_pkey",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {"reject_immutable_change", "validate_attribution_insert"}
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "attribution_metadata_immutable",
        "attribution_metadata_no_truncate",
        "outcome_attributions_immutable_change",
        "outcome_attributions_no_truncate",
        "outcome_attributions_validate_insert",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "attribution_metadata_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "attribution_metadata_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
        (
            "outcome_attributions_immutable_change",
            "outcome_attributions",
            "reject_immutable_change",
            27,
        ),
        (
            "outcome_attributions_no_truncate",
            "outcome_attributions",
            "reject_immutable_change",
            34,
        ),
        (
            "outcome_attributions_validate_insert",
            "outcome_attributions",
            "validate_attribution_insert",
            7,
        ),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "outcome_attributions_attribution_id_check",
        "outcome_attributions_claim_strength_check",
        "outcome_attributions_confidence_json_check",
        "outcome_attributions_descriptor_check",
        "outcome_attributions_effect_check",
        "outcome_attributions_evaluator_id_check",
        "outcome_attributions_evaluator_version_check",
        "outcome_attributions_evidence_artifact_sha256s_json_check",
        "outcome_attributions_memory_revision_ids_json_check",
        "outcome_attributions_method_check",
        "outcome_attributions_pkey",
        "outcome_attributions_reason_check",
        "outcome_attributions_run_outcome_fkey",
        "outcome_attributions_run_outcome_id_check",
        "outcome_attributions_usage_decision_id_check",
        "outcome_attributions_verifier_id_check",
        "schema_metadata_contract_version_check",
        "schema_metadata_pkey",
        "schema_metadata_schema_version_check",
        "schema_metadata_singleton_check",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("outcome_attributions", "attribution_id", "text", "NO", "C"),
        ("outcome_attributions", "run_outcome_id", "text", "NO", "C"),
        ("outcome_attributions", "usage_decision_id", "text", "NO", "C"),
        (
            "outcome_attributions",
            "memory_revision_ids_json",
            "text",
            "NO",
            "C",
        ),
        ("outcome_attributions", "claim_strength", "text", "NO", "C"),
        ("outcome_attributions", "effect", "text", "NO", "C"),
        ("outcome_attributions", "method", "text", "NO", "C"),
        ("outcome_attributions", "evaluator_id", "text", "NO", "C"),
        (
            "outcome_attributions",
            "evaluator_version",
            "text",
            "NO",
            "C",
        ),
        ("outcome_attributions", "verifier_id", "text", "YES", "C"),
        (
            "outcome_attributions",
            "evidence_artifact_sha256s_json",
            "text",
            "NO",
            "C",
        ),
        ("outcome_attributions", "confidence_json", "text", "NO", "C"),
        ("outcome_attributions", "reason", "text", "NO", "C"),
        (
            "outcome_attributions",
            "recorded_at",
            "timestamp with time zone",
            "NO",
            None,
        ),
        ("outcome_attributions", "descriptor", "text", "NO", "C"),
        ("schema_metadata", "singleton", "boolean", "NO", None),
        ("schema_metadata", "schema_version", "integer", "NO", None),
        ("schema_metadata", "contract_version", "text", "NO", "C"),
    }
)
_EXPECTED_CATALOG_SHA256 = (
    "c526057d37173b03356901da43c2d0e5e67a62f95d33930cc06b9be1f5c511a1"
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
           ) || '|' ||
           COALESCE(
               pg_catalog.pg_get_expr(
                   default_record.adbin, default_record.adrelid
               ),
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
               pg_catalog.pg_get_constraintdef(
                   constraint_record.oid, true
               ),
               'trace_backed_memory_v3_outcome_attribution.',
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
               'trace_backed_memory_v3_outcome_attribution.',
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
                   'trace_backed_memory_v3_outcome_attribution.',
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
    r"trace_backed_memory_v3_outcome_attribution\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresOutcomeAttributionV3Error(V3ContractError):
    """Stable base failure for the PostgreSQL attribution ledger."""


class PostgresOutcomeAttributionV3SchemaError(
    PostgresOutcomeAttributionV3Error
):
    pass


class PostgresOutcomeAttributionV3ConflictError(
    PostgresOutcomeAttributionV3Error
):
    pass


class PostgresOutcomeAttributionV3NotFoundError(
    PostgresOutcomeAttributionV3Error
):
    pass


class PostgresOutcomeAttributionV3PersistenceError(
    PostgresOutcomeAttributionV3Error
):
    pass


@dataclass(frozen=True)
class PostgresOutcomeAttributionWrite:
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


@lru_cache(maxsize=1)
def _expected_function_bodies() -> dict[str, str]:
    try:
        source = read_packaged_resource(
            "schemas/postgres-v3-outcome-attribution.sql"
        ).decode("utf-8")
    except (PackagedResourceError, UnicodeError) as error:
        raise PostgresOutcomeAttributionV3SchemaError(
            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
            "could not read canonical PostgreSQL OutcomeAttribution schema",
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresOutcomeAttributionV3SchemaError(
            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
            "canonical PostgreSQL OutcomeAttribution functions are incomplete",
        )
    return bodies


class PostgresOutcomeAttributionV3Repository:
    """Immutable PostgreSQL OutcomeAttribution ledger."""

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
    ) -> PostgresOutcomeAttributionV3Repository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "failed to connect to PostgreSQL OutcomeAttribution storage",
            ) from error
        return cls(connection, owns_connection=True)

    @property
    def outcomes(self) -> PostgresOutcomeV3Repository:
        self._require_open()
        return self._outcomes

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_CLOSED",
                "PostgreSQL OutcomeAttribution repository is closed",
            )

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
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                "PostgreSQL OutcomeAttribution catalog has invalid shape",
            )
        return frozenset(row["name"] for row in rows)

    @staticmethod
    def _schema_drift(detail: str | None = None) -> NoReturn:
        raise PostgresOutcomeAttributionV3SchemaError(
            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
            "PostgreSQL OutcomeAttribution schema definitions do not match"
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
            SELECT trigger.tgname,
                   relation.relname AS table_name,
                   procedure.proname AS function_name,
                   function_namespace.nspname AS function_schema,
                   trigger.tgenabled,
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
            any(
                not isinstance(row, Mapping) or row.get("tgenabled") != "O"
                for row in trigger_rows
            )
            or
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
            self._schema_drift(
                f"constraint missing={missing[:1]} "
                f"unexpected={unexpected[:1]}"
            )

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
            self._schema_drift()

    def _lock_schema(self, cursor: object, *, for_write: bool) -> None:
        self._outcomes._lock_schema(cursor, for_write=for_write)
        cursor.execute(
            """
            SELECT attribution.schema_version AS schema_version,
                   attribution.contract_version AS contract_version
            FROM
                trace_backed_memory_v3_outcome_attribution.schema_metadata
                    AS attribution
            WHERE attribution.singleton
            FOR SHARE OF attribution
            """
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                "PostgreSQL OutcomeAttribution metadata must contain one row",
            )
        if (
            rows[0].get("schema_version")
            != POSTGRES_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION
            or rows[0].get("contract_version")
            != OUTCOME_ATTRIBUTION_CONTRACT_VERSION
        ):
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                "PostgreSQL OutcomeAttribution metadata mismatch",
            )
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_outcome_attribution.schema_metadata, "
            "trace_backed_memory_v3_outcome_attribution."
            "outcome_attributions "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_schema_catalog(cursor)

    @staticmethod
    def _attribution_values(
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

    @classmethod
    def _attribution_from_row(
        cls,
        row: Mapping[str, object],
    ) -> OutcomeAttribution:
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "PostgreSQL OutcomeAttribution row has invalid shape",
            )
        try:
            attribution = loads_outcome_attribution(descriptor)
            recorded_at = (
                PostgresGateSessionRepository._timestamp_from_database(
                    row["recorded_at"]
                )
            )
            stored_values = (
                row["attribution_id"],
                row["run_outcome_id"],
                row["usage_decision_id"],
                row["memory_revision_ids_json"],
                row["claim_strength"],
                row["effect"],
                row["method"],
                row["evaluator_id"],
                row["evaluator_version"],
                row["verifier_id"],
                row["evidence_artifact_sha256s_json"],
                row["confidence_json"],
                row["reason"],
                recorded_at,
                descriptor,
            )
        except (
            KeyError,
            OutcomeContractError,
            PostgresGateSessionPersistenceError,
        ) as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "stored PostgreSQL OutcomeAttribution failed validation",
            ) from error
        if stored_values != cls._attribution_values(attribution):
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "PostgreSQL OutcomeAttribution columns do not match "
                "descriptor",
            )
        return attribution

    @classmethod
    def _select_optional(
        cls,
        cursor: object,
        attribution_id: str,
    ) -> OutcomeAttribution | None:
        cursor.execute(
            """
            SELECT attribution_id,
                   run_outcome_id,
                   usage_decision_id,
                   memory_revision_ids_json,
                   claim_strength,
                   effect,
                   method,
                   evaluator_id,
                   evaluator_version,
                   verifier_id,
                   evidence_artifact_sha256s_json,
                   confidence_json,
                   reason,
                   recorded_at,
                   descriptor
            FROM
                trace_backed_memory_v3_outcome_attribution.
                    outcome_attributions
            WHERE attribution_id = %s
            """,
            (attribution_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "PostgreSQL OutcomeAttribution query has invalid shape",
            )
        return cls._attribution_from_row(rows[0])

    @classmethod
    def _select(
        cls,
        cursor: object,
        attribution_id: str,
    ) -> OutcomeAttribution:
        attribution = cls._select_optional(cursor, attribution_id)
        if attribution is None:
            raise PostgresOutcomeAttributionV3NotFoundError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_NOT_FOUND",
                "OutcomeAttribution was not found",
            )
        return attribution

    def _verify_linkage(
        self,
        cursor: object,
        attribution: OutcomeAttribution,
    ) -> tuple[RunOutcome, GateSession]:
        try:
            outcome = self._outcomes._select_outcome(
                cursor,
                attribution.run_outcome_id,
            )
            session = self._outcomes._gate_sessions._select_current(
                cursor,
                outcome.session_id,
                for_update=False,
            )
            verify_outcome_attribution(attribution, outcome, session)
            return outcome, session
        except (
            OutcomeContractError,
            PostgresGateSessionNotFoundError,
            PostgresOutcomeV3NotFoundError,
        ) as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_LINKAGE",
                "OutcomeAttribution durable linkage is invalid",
            ) from error
        except (
            PostgresGateSessionPersistenceError,
            PostgresOutcomeV3PersistenceError,
        ) as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_DEPENDENCY",
                "PostgreSQL outcome dependency failed validation",
            ) from error

    def _append_attribution_events(
        self,
        cursor: object,
        attribution: OutcomeAttribution,
        outcome: RunOutcome,
        completed_session: GateSession,
    ) -> None:
        if not self._outcomes._gate_sessions._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        access = self._outcomes._gate_sessions._event_access(
            completed_session
        )
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            ledger._lock_schema(cursor, write=True)
            outcome_event = ledger._select_head_event(
                cursor,
                run_outcome_event_stream_id(outcome.run_outcome_id),
                for_update=False,
            )
            if outcome_event is None:
                raise PostgresOutcomeAttributionV3PersistenceError(
                    "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_PARENT_MISSING",
                    "OutcomeAttribution has no canonical RunOutcome parent",
                )
            record = parse_run_outcome_recorded_event(
                outcome_event,
                completed_session=completed_session,
            )
            if record.outcome != outcome:
                raise PostgresOutcomeAttributionV3PersistenceError(
                    "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_PARENT_INVALID",
                    "RunOutcome event differs from the authority row",
                )
            events = build_outcome_attribution_event_batch(
                attribution,
                outcome_event=outcome_event,
                completed_session=completed_session,
                first_global_position=(
                    ledger._select_global_position(
                        cursor,
                        for_update=True,
                    )
                    + 1
                ),
                trusted_context=access.event_trusted_context(),
            )
            stream_id = outcome_attribution_event_stream_id(
                attribution.attribution_id
            )
            ledger._append_in_transaction(
                cursor,
                LedgerAppendRequest(
                    access=access,
                    stream_id=stream_id,
                    expected_stream_version=0,
                    events=events,
                    idempotency=LedgerIdempotency(
                        events[0].idempotency_key_sha256,
                        events[0].request_sha256,
                    ),
                ),
            )
            if (
                ledger._select_head_event(
                    cursor,
                    stream_id,
                    for_update=False,
                )
                != events[-1]
            ):
                raise PostgresOutcomeAttributionV3PersistenceError(
                    "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_READBACK",
                    "OutcomeAttribution event batch read-back changed",
                )
        except (
            EventLedgerPortError,
            OutcomeEventV1Error,
        ) as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_APPEND_FAILED",
                "OutcomeAttribution event batch could not be appended atomically",
            ) from error
        finally:
            ledger.close()

    def _verify_attribution_event_history(
        self,
        cursor: object,
        attribution: OutcomeAttribution,
        outcome: RunOutcome,
        completed_session: GateSession,
    ) -> None:
        if not self._outcomes._gate_sessions._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        access = self._outcomes._gate_sessions._event_access(
            completed_session
        )
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            outcome_event = ledger._select_head_event(
                cursor,
                run_outcome_event_stream_id(outcome.run_outcome_id),
                for_update=False,
            )
            head = ledger._select_head_event(
                cursor,
                outcome_attribution_event_stream_id(
                    attribution.attribution_id
                ),
                for_update=False,
            )
            if outcome_event is None or head is None:
                raise PostgresOutcomeAttributionV3PersistenceError(
                    "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_HISTORY_MISSING",
                    "OutcomeAttribution canonical event history is missing",
                )
            record = parse_run_outcome_recorded_event(
                outcome_event,
                completed_session=completed_session,
            )
            if record.outcome != outcome:
                raise PostgresOutcomeAttributionV3PersistenceError(
                    "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_HISTORY_INVALID",
                    "OutcomeAttribution RunOutcome event is inconsistent",
                )
            if attribution.claim_strength == "association":
                proposal = parse_outcome_attribution_proposed_event(head)
                valid = (
                    head.event_type == OUTCOME_ATTRIBUTION_PROPOSED_EVENT
                    and proposal.to_attribution() == attribution
                    and proposal.run_outcome_event_id == outcome_event.event_id
                )
            else:
                previous_sha256 = head.previous_stream_event_sha256
                proposal_event = (
                    None
                    if previous_sha256 is None
                    else ledger._select_event_by_sha256(
                        cursor,
                        previous_sha256,
                    )
                )
                if proposal_event is None:
                    valid = False
                else:
                    verified = parse_outcome_attribution_verified_event(
                        head,
                        proposal_event=proposal_event,
                    )
                    proposal = parse_outcome_attribution_proposed_event(
                        proposal_event
                    )
                    valid = (
                        verified.attribution == attribution
                        and proposal.run_outcome_event_id
                        == outcome_event.event_id
                    )
            if not valid:
                raise PostgresOutcomeAttributionV3PersistenceError(
                    "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_HISTORY_INVALID",
                    "OutcomeAttribution events differ from the authority row",
                )
        except (
            EventLedgerPortError,
            OutcomeEventV1Error,
        ) as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_EVENT_HISTORY_INVALID",
                "OutcomeAttribution event history failed validation",
            ) from error
        finally:
            ledger.close()

    @classmethod
    def _insert(
        cls,
        cursor: object,
        attribution: OutcomeAttribution,
    ) -> bool:
        cursor.execute(
            """
            INSERT INTO
                trace_backed_memory_v3_outcome_attribution.
                    outcome_attributions (
                attribution_id,
                run_outcome_id,
                usage_decision_id,
                memory_revision_ids_json,
                claim_strength,
                effect,
                method,
                evaluator_id,
                evaluator_version,
                verifier_id,
                evidence_artifact_sha256s_json,
                confidence_json,
                reason,
                recorded_at,
                descriptor
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (attribution_id) DO NOTHING
            RETURNING attribution_id
            """,
            cls._attribution_values(attribution),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                "PostgreSQL OutcomeAttribution insert has invalid shape",
            )
        return bool(rows)

    @_synchronized
    def put_attribution(
        self,
        attribution: OutcomeAttribution,
    ) -> PostgresOutcomeAttributionWrite:
        self._require_open()
        if type(attribution) is not OutcomeAttribution:
            raise TypeError("attribution must be exactly OutcomeAttribution")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._outcomes._gate_sessions._prepare_event_first_write(
                        cursor
                    )
                    self._lock_schema(cursor, for_write=True)
                    existing = self._select_optional(
                        cursor,
                        attribution.attribution_id,
                    )
                    if existing is not None:
                        if existing != attribution:
                            raise PostgresOutcomeAttributionV3ConflictError(
                                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_CONFLICT",
                                "OutcomeAttribution ID has different content",
                            )
                        outcome, session = self._verify_linkage(
                            cursor,
                            existing,
                        )
                        self._verify_attribution_event_history(
                            cursor,
                            existing,
                            outcome,
                            session,
                        )
                        self._verify_schema_catalog(cursor)
                        return PostgresOutcomeAttributionWrite(
                            existing,
                            False,
                        )
                    outcome, session = self._verify_linkage(
                        cursor,
                        attribution,
                    )
                    self._append_attribution_events(
                        cursor,
                        attribution,
                        outcome,
                        session,
                    )
                    inserted = self._insert(cursor, attribution)
                    retained = self._select(
                        cursor,
                        attribution.attribution_id,
                    )
                    if retained != attribution:
                        raise PostgresOutcomeAttributionV3ConflictError(
                            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_CONFLICT",
                            "OutcomeAttribution ID has different content",
                        )
                    self._verify_linkage(cursor, retained)
                    self._verify_schema_catalog(cursor)
                    return PostgresOutcomeAttributionWrite(
                        retained,
                        inserted,
                    )
        except (
            PostgresOutcomeAttributionV3ConflictError,
            PostgresOutcomeAttributionV3NotFoundError,
            PostgresOutcomeAttributionV3PersistenceError,
            PostgresOutcomeAttributionV3SchemaError,
        ):
            raise
        except (
            PostgresGateSessionSchemaError,
            PostgresOutcomeV3SchemaError,
        ) as error:
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                "PostgreSQL outcome dependency failed schema validation",
            ) from error
        except (
            PostgresGateSessionPersistenceError,
            PostgresOutcomeV3PersistenceError,
        ) as error:
            raise PostgresOutcomeAttributionV3PersistenceError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_DEPENDENCY",
                "PostgreSQL outcome dependency failed",
            ) from error
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to store PostgreSQL OutcomeAttribution",
            )

    @_synchronized
    def get_attribution(
        self,
        attribution_id: str,
    ) -> OutcomeAttribution:
        self._require_open()
        if type(attribution_id) is not str:
            raise ValueError("attribution_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, for_write=False)
                    attribution = self._select(cursor, attribution_id)
                    self._verify_linkage(cursor, attribution)
                    return attribution
        except (
            PostgresOutcomeAttributionV3NotFoundError,
            PostgresOutcomeAttributionV3PersistenceError,
            PostgresOutcomeAttributionV3SchemaError,
        ):
            raise
        except (
            PostgresGateSessionSchemaError,
            PostgresOutcomeV3SchemaError,
        ) as error:
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                "PostgreSQL outcome dependency failed schema validation",
            ) from error
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load PostgreSQL OutcomeAttribution",
            )

    @_synchronized
    def list_attributions(
        self,
        run_outcome_id: str,
    ) -> tuple[OutcomeAttribution, ...]:
        self._require_open()
        if type(run_outcome_id) is not str:
            raise ValueError("run_outcome_id must be a string")
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._connection.cursor(row_factory=dict_row) as cursor:
                    self._lock_schema(cursor, for_write=False)
                    try:
                        self._outcomes._select_outcome(
                            cursor,
                            run_outcome_id,
                        )
                    except PostgresOutcomeV3NotFoundError as error:
                        raise PostgresOutcomeAttributionV3NotFoundError(
                            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_OUTCOME_NOT_FOUND",
                            "RunOutcome was not found",
                        ) from error
                    cursor.execute(
                        """
                        SELECT attribution_id,
                               run_outcome_id,
                               usage_decision_id,
                               memory_revision_ids_json,
                               claim_strength,
                               effect,
                               method,
                               evaluator_id,
                               evaluator_version,
                               verifier_id,
                               evidence_artifact_sha256s_json,
                               confidence_json,
                               reason,
                               recorded_at,
                               descriptor
                        FROM
                            trace_backed_memory_v3_outcome_attribution.
                                outcome_attributions
                        WHERE run_outcome_id = %s
                        ORDER BY recorded_at, attribution_id
                        """,
                        (run_outcome_id,),
                    )
                    rows = cursor.fetchall()
                    if any(not isinstance(row, Mapping) for row in rows):
                        raise PostgresOutcomeAttributionV3PersistenceError(
                            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
                            "PostgreSQL OutcomeAttribution list has "
                            "invalid shape",
                        )
                    values = tuple(
                        self._attribution_from_row(row) for row in rows
                    )
                    for attribution in values:
                        self._verify_linkage(cursor, attribution)
                    return values
        except (
            PostgresOutcomeAttributionV3NotFoundError,
            PostgresOutcomeAttributionV3PersistenceError,
            PostgresOutcomeAttributionV3SchemaError,
        ):
            raise
        except (
            PostgresGateSessionSchemaError,
            PostgresOutcomeV3SchemaError,
        ) as error:
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                "PostgreSQL outcome dependency failed schema validation",
            ) from error
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to list PostgreSQL OutcomeAttributions",
            )

    @staticmethod
    def _raise_database_error(
        error: BaseException,
        message: str,
    ) -> NoReturn:
        if getattr(error, "sqlstate", None) in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresOutcomeAttributionV3SchemaError(
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise PostgresOutcomeAttributionV3PersistenceError(
            "TBM_POSTGRES_OUTCOME_ATTRIBUTION_PERSISTENCE",
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

    def __enter__(self) -> PostgresOutcomeAttributionV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = [
    "POSTGRES_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION",
    "PostgresOutcomeAttributionV3ConflictError",
    "PostgresOutcomeAttributionV3Error",
    "PostgresOutcomeAttributionV3NotFoundError",
    "PostgresOutcomeAttributionV3PersistenceError",
    "PostgresOutcomeAttributionV3Repository",
    "PostgresOutcomeAttributionV3SchemaError",
    "PostgresOutcomeAttributionWrite",
]
