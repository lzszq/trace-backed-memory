from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, wraps
import re
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
from .contracts_v3 import V3ContractError
from .postgres import _load_psycopg
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_AUTHORIZATION_V3_SCHEMA_VERSION = 1
POSTGRES_AUTHORIZATION_V3_MAX_PAGE_SIZE = 1000
_SCHEMA = "trace_backed_memory_v3_authorization"
_MISSING_SCHEMA_MESSAGE = "PostgreSQL authorization v3 schema is missing or incomplete"
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_DECISION_ID_RE = re.compile(r"authz_sha256_[0-9a-f]{64}")
_EXPECTED_RELATIONS = frozenset(
    {
        "authorization_decisions",
        "authorization_decisions_pkey",
        "authorization_decisions_policy",
        "authorization_decisions_principal",
        "authorization_decisions_request_key",
        "authorization_policies",
        "authorization_policies_pkey",
        "authorization_policies_version_key",
        "authorization_schema_metadata_pkey",
        "schema_metadata",
    }
)
_EXPECTED_FUNCTIONS = frozenset({"reject_immutable_change"})
_EXPECTED_TRIGGERS = frozenset(
    {
        "authorization_decisions_immutable",
        "authorization_decisions_no_truncate",
        "authorization_policies_immutable",
        "authorization_policies_no_truncate",
        "authorization_schema_metadata_immutable",
        "authorization_schema_metadata_no_truncate",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "authorization_decisions_immutable",
            "authorization_decisions",
            "reject_immutable_change",
            27,
        ),
        (
            "authorization_decisions_no_truncate",
            "authorization_decisions",
            "reject_immutable_change",
            34,
        ),
        (
            "authorization_policies_immutable",
            "authorization_policies",
            "reject_immutable_change",
            27,
        ),
        (
            "authorization_policies_no_truncate",
            "authorization_policies",
            "reject_immutable_change",
            34,
        ),
        (
            "authorization_schema_metadata_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "authorization_schema_metadata_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "authorization_decisions_agent_client_id_check",
        "authorization_decisions_decided_at_check",
        "authorization_decisions_descriptor_check",
        "authorization_decisions_event_id_check",
        "authorization_decisions_permission_check",
        "authorization_decisions_pkey",
        "authorization_decisions_policy_fkey",
        "authorization_decisions_principal_id_check",
        "authorization_decisions_reason_check",
        "authorization_decisions_repository_id_check",
        "authorization_decisions_request_id_check",
        "authorization_decisions_request_key",
        "authorization_decisions_request_sha256_check",
        "authorization_decisions_tenant_id_check",
        "authorization_policies_descriptor_check",
        "authorization_policies_pkey",
        "authorization_policies_sha256_check",
        "authorization_policies_version_check",
        "authorization_policies_version_key",
        "authorization_schema_metadata_contract_version_check",
        "authorization_schema_metadata_pkey",
        "authorization_schema_metadata_schema_version_check",
        "authorization_schema_metadata_singleton_check",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("authorization_decisions", "authorization_event_id", "text", "NO", "C"),
        ("authorization_decisions", "request_id", "text", "NO", "C"),
        ("authorization_decisions", "request_sha256", "text", "NO", "C"),
        ("authorization_decisions", "policy_sha256", "text", "NO", "C"),
        ("authorization_decisions", "principal_id", "text", "NO", "C"),
        ("authorization_decisions", "agent_client_id", "text", "NO", "C"),
        ("authorization_decisions", "tenant_id", "text", "YES", "C"),
        ("authorization_decisions", "repository_id", "text", "YES", "C"),
        ("authorization_decisions", "permission", "text", "NO", "C"),
        ("authorization_decisions", "allowed", "boolean", "NO", None),
        ("authorization_decisions", "reason", "text", "NO", "C"),
        ("authorization_decisions", "decided_at", "text", "NO", "C"),
        ("authorization_decisions", "descriptor", "text", "NO", "C"),
        ("authorization_policies", "policy_sha256", "text", "NO", "C"),
        ("authorization_policies", "policy_version", "text", "NO", "C"),
        ("authorization_policies", "descriptor", "text", "NO", "C"),
        ("schema_metadata", "singleton", "boolean", "NO", None),
        ("schema_metadata", "schema_version", "integer", "NO", None),
        ("schema_metadata", "contract_version", "text", "NO", "C"),
    }
)
_EXPECTED_CATALOG_SHA256 = (
    "7050f2ce5f457431c41fdab0fcfacf5a746054a854484dccd6b5195e850c466b"
)
_CATALOG_SHA256_QUERY = """
WITH descriptors AS (
    SELECT 'schema|' || namespace.nspname || '|' ||
           (namespace.nspowner <> 0)::text || '|' ||
           COALESCE(
               (
                   SELECT pg_catalog.string_agg(
                       CASE
                           WHEN acl.grantee = 0 THEN 'public'
                           WHEN acl.grantee = namespace.nspowner THEN 'owner'
                           ELSE 'other:' || acl.grantee::text
                       END || ':' || acl.privilege_type || ':' ||
                       acl.is_grantable::text,
                       ',' ORDER BY acl.grantee, acl.privilege_type
                   )
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           namespace.nspacl,
                           pg_catalog.acldefault(
                               'n'::"char", namespace.nspowner
                           )
                       )
                   ) AS acl
               ),
               '-'
           ) || '|' ||
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
           class.relreplident::text || '|' ||
           class.relispartition::text || '|' ||
           (class.relowner = namespace.nspowner)::text || '|' ||
           COALESCE(
               (
                   SELECT pg_catalog.string_agg(
                       CASE
                           WHEN acl.grantee = 0 THEN 'public'
                           WHEN acl.grantee = class.relowner THEN 'owner'
                           ELSE 'other:' || acl.grantee::text
                       END || ':' || acl.privilege_type || ':' ||
                       acl.is_grantable::text,
                       ',' ORDER BY acl.grantee, acl.privilege_type
                   )
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           class.relacl,
                           pg_catalog.acldefault(
                               CASE
                                   WHEN class.relkind = 'S' THEN 'S'
                                   ELSE 'r'
                               END::"char",
                               class.relowner
                           )
                       )
                   ) AS acl
               ),
               '-'
           ) || '|' ||
           COALESCE(
               pg_catalog.array_to_string(class.reloptions, ','),
               '-'
           )
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
           ) || '|' || attribute.attnotnull::text || '|' ||
           attribute.attidentity::text || '|' ||
           attribute.attgenerated::text || '|' ||
           attribute.attstorage::text || '|' ||
           attribute.attcompression::text || '|' ||
           COALESCE(
               (
                   SELECT pg_catalog.string_agg(
                       CASE
                           WHEN acl.grantee = 0 THEN 'public'
                           WHEN acl.grantee = class.relowner THEN 'owner'
                           ELSE 'other:' || acl.grantee::text
                       END || ':' || acl.privilege_type || ':' ||
                       acl.is_grantable::text,
                       ',' ORDER BY acl.grantee, acl.privilege_type
                   )
                   FROM pg_catalog.aclexplode(attribute.attacl) AS acl
               ),
               '-'
           ) || '|' ||
           COALESCE(
               collation_namespace.nspname || '.' ||
               collation_record.collname,
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
    SELECT 'constraint|' || class.relname || '|' ||
           constraint_record.conname || '|' ||
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
               'trace_backed_memory_v3_authorization.',
               ''
           ) AS descriptor
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_class AS class
      ON class.oid = constraint_record.conrelid
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
               'trace_backed_memory_v3_authorization.',
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
           pg_catalog.has_function_privilege(
               'public', procedure.oid, 'EXECUTE'
           )::text || '|' ||
           COALESCE(
               (
                   SELECT pg_catalog.string_agg(
                       CASE
                           WHEN acl.grantee = 0 THEN 'public'
                           WHEN acl.grantee = procedure.proowner THEN 'owner'
                           ELSE 'other:' || acl.grantee::text
                       END || ':' || acl.privilege_type || ':' ||
                       acl.is_grantable::text,
                       ',' ORDER BY acl.grantee, acl.privilege_type
                   )
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault(
                               'f'::"char", procedure.proowner
                           )
                       )
                   ) AS acl
               ),
               '-'
           ) || '|' ||
           COALESCE(
               pg_catalog.array_to_string(procedure.proconfig, ','),
               '-'
           ) || '|' || pg_catalog.btrim(procedure.prosrc)
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
                   'trace_backed_memory_v3_authorization.',
                   ''
               ),
               '-'
           )
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = class.relnamespace
    JOIN pg_catalog.pg_proc AS procedure ON procedure.oid = trigger.tgfoid
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
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresAuthorizationV3Error(V3ContractError):
    pass


class PostgresAuthorizationV3SchemaError(PostgresAuthorizationV3Error):
    pass


class PostgresAuthorizationV3ConflictError(PostgresAuthorizationV3Error):
    pass


class PostgresAuthorizationV3PersistenceError(PostgresAuthorizationV3Error):
    pass


@dataclass(frozen=True)
class PostgresAuthorizationV3AppendResult:
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


@lru_cache(maxsize=1)
def _expected_function_bodies() -> dict[str, str]:
    try:
        sql = read_packaged_resource("schemas/postgres-v3-authorization.sql").decode(
            "utf-8"
        )
    except (OSError, UnicodeError, PackagedResourceError) as error:
        raise PostgresAuthorizationV3SchemaError(
            "TBM_POSTGRES_AUTHORIZATION_SCHEMA",
            "could not validate canonical PostgreSQL authorization functions",
        ) from error
    pattern = re.compile(
        r"CREATE FUNCTION\s+"
        r"trace_backed_memory_v3_authorization\.([a-z0-9_]+)\(\)"
        r".*?\bAS \$\$(.*?)\$\$;",
        re.DOTALL,
    )
    bodies = {
        name: body.replace("\r\n", "\n").strip() for name, body in pattern.findall(sql)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresAuthorizationV3SchemaError(
            "TBM_POSTGRES_AUTHORIZATION_SCHEMA",
            "canonical PostgreSQL authorization functions are incomplete",
        )
    return bodies


class PostgresAuthorizationV3Repository:
    """Immutable PostgreSQL authority for policies and authorization decisions."""

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
    ) -> PostgresAuthorizationV3Repository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresAuthorizationV3PersistenceError(
                "TBM_POSTGRES_AUTHORIZATION_PERSISTENCE",
                "failed to connect to PostgreSQL",
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresAuthorizationV3PersistenceError(
                "TBM_POSTGRES_AUTHORIZATION_CLOSED",
                "PostgreSQL authorization repository is closed",
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    @staticmethod
    def _catalog_names(cursor: object, query: str) -> frozenset[str]:
        cursor.execute(query, (_SCHEMA,))
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping) or type(row.get("name")) is not str
            for row in rows
        ):
            PostgresAuthorizationV3Repository._schema_drift()
        return frozenset(cast(str, row["name"]) for row in rows)

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            """
            SELECT active.schema_version AS active_schema_version,
                   authz.schema_version AS authorization_schema_version,
                   authz.contract_version AS contract_version
            FROM public.trace_backed_memory_schema AS active
            CROSS JOIN
                trace_backed_memory_v3_authorization.schema_metadata
                    AS authz
            WHERE active.singleton AND authz.singleton
            FOR SHARE OF active, authz
            """
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or rows[0].get("active_schema_version") != 2
            or rows[0].get("authorization_schema_version")
            != POSTGRES_AUTHORIZATION_V3_SCHEMA_VERSION
            or rows[0].get("contract_version") != "tbm.authorization.v3"
        ):
            raise PostgresAuthorizationV3SchemaError(
                "TBM_POSTGRES_AUTHORIZATION_SCHEMA",
                "PostgreSQL authorization schema metadata mismatch",
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
        if (
            relations != _EXPECTED_RELATIONS
            or functions != _EXPECTED_FUNCTIONS
            or triggers != _EXPECTED_TRIGGERS
            or constraints != _EXPECTED_CONSTRAINTS
        ):
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
            or len(trigger_shapes) != len(trigger_rows)
            or trigger_shapes != _EXPECTED_TRIGGER_SHAPES
        ):
            self._schema_drift()

        cursor.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, collation_name
            FROM information_schema.columns
            WHERE table_schema = %s
            """,
            (_SCHEMA,),
        )
        try:
            columns = frozenset(
                (
                    row["table_name"],
                    row["column_name"],
                    row["data_type"],
                    row["is_nullable"],
                    row["collation_name"],
                )
                for row in cursor.fetchall()
            )
        except (KeyError, TypeError):
            self._schema_drift()
        if columns != _EXPECTED_COLUMNS:
            self._schema_drift()

        cursor.execute(
            """
            SELECT procedure.proname, procedure.proconfig, procedure.prosrc,
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
        if len(function_rows) != len(_EXPECTED_FUNCTIONS):
            self._schema_drift()
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
            _CATALOG_SHA256_QUERY,
            (
                _SCHEMA,
                _SCHEMA,
                _SCHEMA,
                _SCHEMA,
                _SCHEMA,
                _SCHEMA,
                _SCHEMA,
            ),
        )
        catalog_rows = cursor.fetchall()
        if (
            len(catalog_rows) != 1
            or not isinstance(catalog_rows[0], Mapping)
            or catalog_rows[0].get("catalog_sha256") != _EXPECTED_CATALOG_SHA256
        ):
            self._schema_drift()

    @staticmethod
    def _schema_drift() -> NoReturn:
        raise PostgresAuthorizationV3SchemaError(
            "TBM_POSTGRES_AUTHORIZATION_SCHEMA",
            "PostgreSQL authorization schema definitions do not match",
        )

    @staticmethod
    def _policy_values(
        policy: AuthorizationPolicyBundle,
    ) -> tuple[str, str, str]:
        descriptor = dumps_authorization_policy(policy)
        if len(descriptor.encode("utf-8")) > AUTHORIZATION_JSON_MAX_BYTES:
            raise ValueError("authorization policy descriptor exceeds storage limit")
        return policy.policy_sha256, policy.policy_version, descriptor

    @classmethod
    def _stored_policy(cls, row: Mapping[str, object]) -> AuthorizationPolicyBundle:
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            cls._persistence("PostgreSQL authorization policy row has invalid shape")
        try:
            policy = loads_authorization_policy(cast(str, descriptor))
        except AuthorizationContractError as error:
            raise PostgresAuthorizationV3PersistenceError(
                "TBM_POSTGRES_AUTHORIZATION_PERSISTENCE",
                "PostgreSQL authorization policy descriptor failed validation",
            ) from error
        if (
            row.get("policy_sha256"),
            row.get("policy_version"),
            descriptor,
        ) != cls._policy_values(policy):
            cls._persistence(
                "PostgreSQL authorization policy columns do not match descriptor"
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
            decision.allowed,
            decision.reason,
            canonical_rfc3339(decision.decided_at),
            descriptor,
        )

    @classmethod
    def _stored_decision(cls, row: Mapping[str, object]) -> AuthorizationDecision:
        descriptor = row.get("descriptor")
        if type(descriptor) is not str:
            cls._persistence("PostgreSQL authorization decision row has invalid shape")
        try:
            decision = loads_authorization_decision(cast(str, descriptor))
        except AuthorizationContractError as error:
            raise PostgresAuthorizationV3PersistenceError(
                "TBM_POSTGRES_AUTHORIZATION_PERSISTENCE",
                "PostgreSQL authorization decision descriptor failed validation",
            ) from error
        names = (
            "authorization_event_id",
            "request_id",
            "request_sha256",
            "policy_sha256",
            "principal_id",
            "agent_client_id",
            "tenant_id",
            "repository_id",
            "permission",
            "allowed",
            "reason",
            "decided_at",
            "descriptor",
        )
        if tuple(row.get(name) for name in names) != cls._decision_values(decision):
            cls._persistence(
                "PostgreSQL authorization decision columns do not match descriptor"
            )
        return decision

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise PostgresAuthorizationV3PersistenceError(
            "TBM_POSTGRES_AUTHORIZATION_PERSISTENCE", message
        )

    @staticmethod
    def _select_one(
        cursor: object,
        query: str,
        parameters: tuple[object, ...],
    ) -> Mapping[str, object] | None:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
        if len(rows) > 1 or any(not isinstance(row, Mapping) for row in rows):
            PostgresAuthorizationV3Repository._persistence(
                "PostgreSQL authorization identity is not unique"
            )
        return cast(Mapping[str, object], rows[0]) if rows else None

    @staticmethod
    def _policy_select() -> str:
        return (
            "SELECT policy_sha256, policy_version, descriptor "
            "FROM trace_backed_memory_v3_authorization.authorization_policies "
        )

    @staticmethod
    def _decision_select() -> str:
        return (
            "SELECT authorization_event_id, request_id, request_sha256, "
            "policy_sha256, principal_id, agent_client_id, tenant_id, "
            "repository_id, permission, allowed, reason, decided_at, descriptor "
            "FROM trace_backed_memory_v3_authorization.authorization_decisions "
        )

    def _put_policy(self, cursor: object, policy: AuthorizationPolicyBundle) -> bool:
        values = self._policy_values(policy)
        cursor.execute(
            """
            INSERT INTO
                trace_backed_memory_v3_authorization.authorization_policies
                (policy_sha256, policy_version, descriptor)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING policy_sha256
            """,
            values,
        )
        inserted = cursor.fetchall()
        if inserted:
            return True
        row = self._select_one(
            cursor,
            self._policy_select() + "WHERE policy_sha256 = %s FOR SHARE",
            (policy.policy_sha256,),
        )
        if row is None or self._policy_values(self._stored_policy(row)) != values:
            raise PostgresAuthorizationV3ConflictError(
                "TBM_POSTGRES_AUTHORIZATION_CONFLICT",
                "authorization policy conflicts with stored identity",
            )
        return False

    def _put_decision(self, cursor: object, decision: AuthorizationDecision) -> bool:
        values = self._decision_values(decision)
        cursor.execute(
            """
            INSERT INTO
                trace_backed_memory_v3_authorization.authorization_decisions
                (
                    authorization_event_id, request_id, request_sha256,
                    policy_sha256, principal_id, agent_client_id, tenant_id,
                    repository_id, permission, allowed, reason, decided_at,
                    descriptor
                )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            RETURNING authorization_event_id
            """,
            values,
        )
        inserted = cursor.fetchall()
        if inserted:
            return True
        row = self._select_one(
            cursor,
            self._decision_select()
            + "WHERE authorization_event_id = %s OR request_id = %s "
            "ORDER BY authorization_event_id FOR SHARE",
            (decision.authorization_event_id, decision.request_id),
        )
        if row is None or self._decision_values(self._stored_decision(row)) != values:
            raise PostgresAuthorizationV3ConflictError(
                "TBM_POSTGRES_AUTHORIZATION_CONFLICT",
                "authorization decision conflicts with stored request identity",
            )
        return False

    @_synchronized
    def store_policy(self, policy: AuthorizationPolicyBundle) -> bool:
        self._require_open()
        if type(policy) is not AuthorizationPolicyBundle:
            raise ValueError("policy must be exactly AuthorizationPolicyBundle")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._put_policy(cursor, policy)
        except (PostgresAuthorizationV3Error, ValueError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to store authorization policy")

    @_synchronized
    def authorize_and_record(
        self,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        *,
        decided_at: str,
    ) -> tuple[AuthorizationDecision, PostgresAuthorizationV3AppendResult]:
        self._require_open()
        if type(policy) is not AuthorizationPolicyBundle:
            raise ValueError("policy must be exactly AuthorizationPolicyBundle")
        if type(request) is not AuthorizationRequest:
            raise ValueError("request must be exactly AuthorizationRequest")
        decision = authorize(policy, request, decided_at=decided_at)
        return decision, self.append_decision(policy, request, decision)

    @_synchronized
    def append_decision(
        self,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
    ) -> PostgresAuthorizationV3AppendResult:
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
            raise PostgresAuthorizationV3ConflictError(
                "TBM_POSTGRES_AUTHORIZATION_CONFLICT",
                "authorization decision does not verify against its request",
            ) from error
        if decision.policy_sha256 != policy.policy_sha256:
            raise PostgresAuthorizationV3ConflictError(
                "TBM_POSTGRES_AUTHORIZATION_CONFLICT",
                "authorization decision references a different policy",
            )
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    policy_inserted = self._put_policy(cursor, policy)
                    decision_inserted = self._put_decision(cursor, decision)
            return PostgresAuthorizationV3AppendResult(
                policy_sha256=policy.policy_sha256,
                policy_inserted=policy_inserted,
                authorization_event_id=decision.authorization_event_id,
                decision_inserted=decision_inserted,
            )
        except (PostgresAuthorizationV3Error, ValueError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to append authorization decision")

    @_synchronized
    def load_policy(self, policy_sha256: str) -> AuthorizationPolicyBundle:
        self._require_open()
        if type(policy_sha256) is not str or not _DIGEST_RE.fullmatch(policy_sha256):
            raise ValueError("policy_sha256 must be a canonical digest")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    row = self._select_one(
                        cursor,
                        self._policy_select() + "WHERE policy_sha256 = %s",
                        (policy_sha256,),
                    )
                    if row is None:
                        raise KeyError(policy_sha256)
                    return self._stored_policy(row)
        except (KeyError, PostgresAuthorizationV3Error):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to load authorization policy")

    @_synchronized
    def load_decision(self, authorization_event_id: str) -> AuthorizationDecision:
        self._require_open()
        if type(authorization_event_id) is not str or not _DECISION_ID_RE.fullmatch(
            authorization_event_id
        ):
            raise ValueError("authorization_event_id must be canonical")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    row = self._select_one(
                        cursor,
                        self._decision_select() + "WHERE authorization_event_id = %s",
                        (authorization_event_id,),
                    )
                    if row is None:
                        raise KeyError(authorization_event_id)
                    return self._stored_decision(row)
        except (KeyError, PostgresAuthorizationV3Error):
            raise
        except psycopg.Error as error:
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
            or not 1 <= limit <= POSTGRES_AUTHORIZATION_V3_MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {POSTGRES_AUTHORIZATION_V3_MAX_PAGE_SIZE}"
            )
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        self._decision_select() + "WHERE policy_sha256 = %s "
                        "ORDER BY decided_at, authorization_event_id LIMIT %s",
                        (policy_sha256, limit),
                    )
                    rows = cursor.fetchall()
                    if any(not isinstance(row, Mapping) for row in rows):
                        self._persistence(
                            "PostgreSQL authorization decision row has invalid shape"
                        )
                    return tuple(
                        self._stored_decision(cast(Mapping[str, object], row))
                        for row in rows
                    )
        except PostgresAuthorizationV3Error:
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to list authorization decisions")

    @staticmethod
    def _raise_database_error(error: BaseException, message: str) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresAuthorizationV3SchemaError(
                "TBM_POSTGRES_AUTHORIZATION_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        if type(sqlstate) is str and (sqlstate.startswith("23") or sqlstate == "P0001"):
            raise PostgresAuthorizationV3ConflictError(
                "TBM_POSTGRES_AUTHORIZATION_CONFLICT",
                "authorization record conflicts with stored identity",
            ) from error
        raise PostgresAuthorizationV3PersistenceError(
            "TBM_POSTGRES_AUTHORIZATION_PERSISTENCE", message
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresAuthorizationV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
