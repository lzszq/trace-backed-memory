from __future__ import annotations

from collections.abc import Callable
from functools import wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .managed_index_v3 import (
    MANAGED_INDEX_BUNDLE_CONTRACT_VERSION,
    MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES,
    ManagedIndexBundle,
    ManagedIndexPublication,
    dumps_managed_index_bundle,
    loads_managed_index_bundle,
)
from .postgres import _load_psycopg


POSTGRES_MANAGED_INDEX_V3_SCHEMA_VERSION = 1
_SCHEMA = "trace_backed_memory_v3_managed_index"
_BUNDLE_ID_RE = re.compile(r"^managed_index_bundle_sha256_[0-9a-f]{64}$")
_MISSING_SCHEMA_MESSAGE = "PostgreSQL managed index v3 schema is missing or incomplete"
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_EXPECTED_RELATIONS = frozenset(
    {
        "managed_index_bundles_pkey",
        "managed_index_bundles_scope_key",
        "managed_index_heads_pkey",
        "managed_index_schema_metadata_pkey",
        "schema_metadata",
        "v3_managed_index_bundles",
        "v3_managed_index_bundles_scope",
        "v3_managed_index_heads",
    }
)
_EXPECTED_FUNCTIONS = frozenset({"reject_immutable_change", "validate_head_advance"})
_EXPECTED_FUNCTION_IDENTITIES = frozenset(
    {
        ("reject_immutable_change", "", "trigger", "f", 0),
        ("validate_head_advance", "", "trigger", "f", 0),
    }
)
_EXPECTED_FUNCTION_BODIES = {
    "reject_immutable_change": (
        "BEGIN RAISE EXCEPTION "
        "'managed index immutable relation cannot be changed'; END;"
    ),
    "validate_head_advance": (
        "BEGIN IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id "
        "OR NEW.repository_id IS DISTINCT FROM OLD.repository_id "
        "OR NEW.environment_id IS DISTINCT FROM OLD.environment_id "
        "OR NEW.head_version IS DISTINCT FROM OLD.head_version + 1 "
        "OR NEW.bundle_id IS NOT DISTINCT FROM OLD.bundle_id THEN "
        "RAISE EXCEPTION 'managed index head update must be one CAS advance'; "
        "END IF; RETURN NEW; END;"
    ),
}
_EXPECTED_TRIGGERS = frozenset(
    {
        "managed_index_bundles_immutable",
        "managed_index_bundles_no_truncate",
        "managed_index_heads_cas",
        "managed_index_heads_no_delete",
        "managed_index_heads_no_truncate",
        "managed_index_schema_immutable",
        "managed_index_schema_no_truncate",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "managed_index_bundles_immutable",
            "v3_managed_index_bundles",
            "reject_immutable_change",
            27,
        ),
        (
            "managed_index_bundles_no_truncate",
            "v3_managed_index_bundles",
            "reject_immutable_change",
            34,
        ),
        (
            "managed_index_heads_cas",
            "v3_managed_index_heads",
            "validate_head_advance",
            19,
        ),
        (
            "managed_index_heads_no_delete",
            "v3_managed_index_heads",
            "reject_immutable_change",
            11,
        ),
        (
            "managed_index_heads_no_truncate",
            "v3_managed_index_heads",
            "reject_immutable_change",
            34,
        ),
        (
            "managed_index_schema_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "managed_index_schema_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "managed_index_bundle_catalog_digest_check",
        "managed_index_bundle_environment_check",
        "managed_index_bundle_id_check",
        "managed_index_bundle_payload_check",
        "managed_index_bundle_repository_check",
        "managed_index_bundle_retriever_check",
        "managed_index_bundle_retriever_version_check",
        "managed_index_bundle_tenant_check",
        "managed_index_bundles_pkey",
        "managed_index_bundles_scope_key",
        "managed_index_head_environment_check",
        "managed_index_head_repository_check",
        "managed_index_head_tenant_check",
        "managed_index_head_version_check",
        "managed_index_heads_bundle_fkey",
        "managed_index_heads_pkey",
        "managed_index_schema_contract_check",
        "managed_index_schema_metadata_pkey",
        "managed_index_schema_singleton_check",
        "managed_index_schema_version_check",
    }
)
_EXPECTED_CONSTRAINT_DEFINITIONS = frozenset(
    {
        (
            "managed_index_bundle_catalog_digest_check",
            "c",
            False,
            False,
            True,
            "CHECK (source_catalog_sha256 ~ '^sha256:[0-9a-f]{64}$'::text)",
        ),
        (
            "managed_index_bundle_environment_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(environment_id) >= 1 "
            "AND char_length(environment_id) <= 128 "
            "AND btrim(environment_id) <> ''::text)",
        ),
        (
            "managed_index_bundle_id_check",
            "c",
            False,
            False,
            True,
            "CHECK (bundle_id ~ '^managed_index_bundle_sha256_[0-9a-f]{64}$'::text)",
        ),
        (
            "managed_index_bundle_payload_check",
            "c",
            False,
            False,
            True,
            "CHECK (octet_length(payload_utf8) >= 2 "
            "AND octet_length(payload_utf8) <= 67108864)",
        ),
        (
            "managed_index_bundle_repository_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(repository_id) >= 1 "
            "AND char_length(repository_id) <= 128 "
            "AND btrim(repository_id) <> ''::text)",
        ),
        (
            "managed_index_bundle_retriever_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(retriever_id) >= 1 "
            "AND char_length(retriever_id) <= 128 "
            "AND btrim(retriever_id) <> ''::text)",
        ),
        (
            "managed_index_bundle_retriever_version_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(retriever_version) >= 1 "
            "AND char_length(retriever_version) <= 128 "
            "AND btrim(retriever_version) <> ''::text)",
        ),
        (
            "managed_index_bundle_tenant_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(tenant_id) >= 1 "
            "AND char_length(tenant_id) <= 128 "
            "AND btrim(tenant_id) <> ''::text)",
        ),
        (
            "managed_index_bundles_pkey",
            "p",
            False,
            False,
            True,
            "PRIMARY KEY (bundle_id)",
        ),
        (
            "managed_index_bundles_scope_key",
            "u",
            False,
            False,
            True,
            "UNIQUE (tenant_id, repository_id, environment_id, bundle_id)",
        ),
        (
            "managed_index_head_environment_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(environment_id) >= 1 "
            "AND char_length(environment_id) <= 128 "
            "AND btrim(environment_id) <> ''::text)",
        ),
        (
            "managed_index_head_repository_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(repository_id) >= 1 "
            "AND char_length(repository_id) <= 128 "
            "AND btrim(repository_id) <> ''::text)",
        ),
        (
            "managed_index_head_tenant_check",
            "c",
            False,
            False,
            True,
            "CHECK (char_length(tenant_id) >= 1 "
            "AND char_length(tenant_id) <= 128 "
            "AND btrim(tenant_id) <> ''::text)",
        ),
        (
            "managed_index_head_version_check",
            "c",
            False,
            False,
            True,
            "CHECK (head_version >= 1)",
        ),
        (
            "managed_index_heads_bundle_fkey",
            "f",
            False,
            False,
            True,
            "FOREIGN KEY (tenant_id, repository_id, environment_id, "
            "bundle_id) REFERENCES v3_managed_index_bundles("
            "tenant_id, repository_id, environment_id, bundle_id) "
            "ON UPDATE RESTRICT ON DELETE RESTRICT",
        ),
        (
            "managed_index_heads_pkey",
            "p",
            False,
            False,
            True,
            "PRIMARY KEY (tenant_id, repository_id, environment_id)",
        ),
        (
            "managed_index_schema_contract_check",
            "c",
            False,
            False,
            True,
            "CHECK (contract_version = 'tbm.managed-index-bundle.v3'::text)",
        ),
        (
            "managed_index_schema_metadata_pkey",
            "p",
            False,
            False,
            True,
            "PRIMARY KEY (singleton)",
        ),
        (
            "managed_index_schema_singleton_check",
            "c",
            False,
            False,
            True,
            "CHECK (singleton = 1)",
        ),
        (
            "managed_index_schema_version_check",
            "c",
            False,
            False,
            True,
            "CHECK (schema_version = 1)",
        ),
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("schema_metadata", "singleton", "integer", True, None),
        ("schema_metadata", "schema_version", "integer", True, None),
        ("schema_metadata", "contract_version", "text", True, "pg_catalog.C"),
        (
            "v3_managed_index_bundles",
            "bundle_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "tenant_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "repository_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "environment_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "retriever_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "retriever_version",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "source_catalog_sha256",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_bundles",
            "payload_utf8",
            "bytea",
            True,
            None,
        ),
        (
            "v3_managed_index_bundles",
            "appended_at",
            "timestamp with time zone",
            True,
            None,
        ),
        (
            "v3_managed_index_heads",
            "tenant_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_heads",
            "repository_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_heads",
            "environment_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        (
            "v3_managed_index_heads",
            "bundle_id",
            "text",
            True,
            "pg_catalog.C",
        ),
        ("v3_managed_index_heads", "head_version", "bigint", True, None),
    }
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresManagedIndexV3Error(RuntimeError):
    pass


class PostgresManagedIndexV3SchemaError(PostgresManagedIndexV3Error):
    pass


class PostgresManagedIndexV3ConflictError(PostgresManagedIndexV3Error):
    pass


class PostgresManagedIndexV3PersistenceError(PostgresManagedIndexV3Error):
    pass


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


class PostgresManagedIndexV3Repository:
    """Immutable PostgreSQL managed-index bundles and scope CAS heads."""

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
    ) -> PostgresManagedIndexV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresManagedIndexV3PersistenceError(
                "failed to connect to PostgreSQL managed index v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresManagedIndexV3Error(
                "PostgreSQL managed index v3 repository is closed"
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            "SELECT active.schema_version AS active_version, "
            "managed.schema_version AS managed_version, "
            "managed.contract_version AS contract_version "
            "FROM public.trace_backed_memory_schema AS active "
            "CROSS JOIN "
            "trace_backed_memory_v3_managed_index.schema_metadata AS managed "
            "WHERE active.singleton AND managed.singleton = 1 "
            "FOR SHARE OF active, managed"
        )
        rows = cursor.fetchall()
        if rows != [
            {
                "active_version": 2,
                "managed_version": POSTGRES_MANAGED_INDEX_V3_SCHEMA_VERSION,
                "contract_version": MANAGED_INDEX_BUNDLE_CONTRACT_VERSION,
            }
        ]:
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 metadata mismatch"
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
            "AND class.relkind NOT IN ('r', 'i')) "
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
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 contains unsupported catalog objects"
            )
        cursor.execute(
            "SELECT class.relname "
            "FROM pg_catalog.pg_class AS class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND class.relkind IN ('r', 'i')",
            (_SCHEMA,),
        )
        if {row["relname"] for row in cursor.fetchall()} != _EXPECTED_RELATIONS:
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 relation catalog mismatch"
            )
        cursor.execute(
            "SELECT routine.proname, routine.prosecdef, "
            "routine.provolatile, routine.proconfig, routine.prosrc, "
            "pg_catalog.pg_get_function_identity_arguments(routine.oid) "
            "AS identity_arguments, "
            "routine.prorettype::pg_catalog.regtype::text AS result_type, "
            "routine.prokind, routine.pronargs "
            "FROM pg_catalog.pg_proc AS routine "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = routine.pronamespace "
            "WHERE namespace.nspname = %s",
            (_SCHEMA,),
        )
        function_rows = cursor.fetchall()
        if (
            {row["proname"] for row in function_rows} != _EXPECTED_FUNCTIONS
            or {
                (
                    row["proname"],
                    row["identity_arguments"],
                    row["result_type"],
                    row["prokind"],
                    row["pronargs"],
                )
                for row in function_rows
            }
            != _EXPECTED_FUNCTION_IDENTITIES
            or any(
                row["prosecdef"]
                or row["provolatile"] != "v"
                or row["proconfig"] != ["search_path=pg_catalog"]
                or " ".join(cast(str, row["prosrc"]).split())
                != _EXPECTED_FUNCTION_BODIES.get(row["proname"])
                for row in function_rows
            )
        ):
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 function catalog mismatch"
            )
        cursor.execute(
            "SELECT trigger.tgname, class.relname, routine.proname, "
            "trigger.tgtype, trigger.tgenabled "
            "FROM pg_catalog.pg_trigger AS trigger "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = trigger.tgrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "JOIN pg_catalog.pg_proc AS routine "
            "ON routine.oid = trigger.tgfoid "
            "WHERE namespace.nspname = %s AND NOT trigger.tgisinternal",
            (_SCHEMA,),
        )
        trigger_rows = cursor.fetchall()
        if (
            {row["tgname"] for row in trigger_rows} != _EXPECTED_TRIGGERS
            or {
                (
                    row["tgname"],
                    row["relname"],
                    row["proname"],
                    row["tgtype"],
                )
                for row in trigger_rows
            }
            != _EXPECTED_TRIGGER_SHAPES
            or any(row["tgenabled"] != "O" for row in trigger_rows)
        ):
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 trigger catalog mismatch"
            )
        cursor.execute(
            "SELECT constraint_record.conname, "
            "constraint_record.contype, "
            "constraint_record.condeferrable, "
            "constraint_record.condeferred, "
            "constraint_record.convalidated, "
            "pg_catalog.replace("
            "pg_catalog.pg_get_constraintdef("
            "constraint_record.oid, true), "
            "'trace_backed_memory_v3_managed_index.', '') "
            "AS definition "
            "FROM pg_catalog.pg_constraint AS constraint_record "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = constraint_record.connamespace "
            "WHERE namespace.nspname = %s "
            "AND constraint_record.contype <> 'n'",
            (_SCHEMA,),
        )
        constraint_rows = cursor.fetchall()
        if {row["conname"] for row in constraint_rows} != _EXPECTED_CONSTRAINTS or {
            (
                row["conname"],
                row["contype"],
                row["condeferrable"],
                row["condeferred"],
                row["convalidated"],
                row["definition"],
            )
            for row in constraint_rows
            if row["contype"] != "n"
        } != _EXPECTED_CONSTRAINT_DEFINITIONS:
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 constraint catalog mismatch"
            )
        cursor.execute(
            "SELECT class.relname, attribute.attname, "
            "pg_catalog.format_type("
            "attribute.atttypid, attribute.atttypmod) AS data_type, "
            "attribute.attnotnull, "
            "CASE WHEN attribute.attcollation = 0 THEN NULL "
            "ELSE collation_namespace.nspname || '.' || "
            "collation_record.collname END AS collation_name "
            "FROM pg_catalog.pg_attribute AS attribute "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = attribute.attrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "LEFT JOIN pg_catalog.pg_collation AS collation_record "
            "ON collation_record.oid = attribute.attcollation "
            "LEFT JOIN pg_catalog.pg_namespace AS collation_namespace "
            "ON collation_namespace.oid = collation_record.collnamespace "
            "WHERE namespace.nspname = %s AND class.relkind = 'r' "
            "AND attribute.attnum > 0 AND NOT attribute.attisdropped",
            (_SCHEMA,),
        )
        if {
            (
                row["relname"],
                row["attname"],
                row["data_type"],
                row["attnotnull"],
                row["collation_name"],
            )
            for row in cursor.fetchall()
        } != _EXPECTED_COLUMNS:
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 column catalog mismatch"
            )
        cursor.execute(
            "SELECT pg_catalog.has_schema_privilege("
            "'public', %s, 'USAGE') AS public_usage, "
            "pg_catalog.has_schema_privilege("
            "'public', %s, 'CREATE') AS public_create",
            (_SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [{"public_usage": False, "public_create": False}]:
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 schema ACL mismatch"
            )
        cursor.execute(
            """
            SELECT
                (
                    SELECT pg_catalog.array_agg(
                        CASE
                            WHEN acl.grantee = namespace.nspowner
                                THEN 'owner'
                            ELSE 'other:' || acl.grantee::text
                        END || ':' ||
                        CASE
                            WHEN acl.grantor = namespace.nspowner
                                THEN 'owner'
                            ELSE 'other:' || acl.grantor::text
                        END || ':' || acl.privilege_type || ':' ||
                        acl.is_grantable::text
                        ORDER BY acl.grantee, acl.privilege_type
                    )
                    FROM pg_catalog.aclexplode(
                        COALESCE(
                            namespace.nspacl,
                            pg_catalog.acldefault(
                                'n'::"char",
                                namespace.nspowner
                            )
                        )
                    ) AS acl
                ) = (
                    SELECT pg_catalog.array_agg(
                        'owner:owner:' || acl.privilege_type || ':' ||
                        acl.is_grantable::text
                        ORDER BY acl.privilege_type
                    )
                    FROM pg_catalog.aclexplode(
                        pg_catalog.acldefault(
                            'n'::"char",
                            namespace.nspowner
                        )
                    ) AS acl
                    WHERE acl.grantee = namespace.nspowner
                      AND acl.grantor = namespace.nspowner
                ) AS schema_acl_exact,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS class
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = class.relnamespace
                    WHERE namespace.nspname = %s
                      AND class.relkind IN ('r', 'i')
                      AND (
                          class.relowner <> namespace.nspowner
                          OR (
                              class.relkind = 'i'
                              AND class.relacl IS NOT NULL
                          )
                          OR (
                              class.relkind = 'r'
                              AND (
                                  SELECT pg_catalog.array_agg(
                                      CASE
                                          WHEN acl.grantee = class.relowner
                                              THEN 'owner'
                                          ELSE
                                              'other:' || acl.grantee::text
                                      END || ':' ||
                                      CASE
                                          WHEN acl.grantor = class.relowner
                                              THEN 'owner'
                                          ELSE
                                              'other:' || acl.grantor::text
                                      END || ':' ||
                                      acl.privilege_type || ':' ||
                                      acl.is_grantable::text
                                      ORDER BY
                                          acl.grantee,
                                          acl.privilege_type
                                  )
                                  FROM pg_catalog.aclexplode(
                                      COALESCE(
                                          class.relacl,
                                          pg_catalog.acldefault(
                                              'r'::"char",
                                              class.relowner
                                          )
                                      )
                                  ) AS acl
                              ) IS DISTINCT FROM (
                                  SELECT pg_catalog.array_agg(
                                      'owner:owner:' ||
                                      acl.privilege_type || ':' ||
                                      acl.is_grantable::text
                                      ORDER BY acl.privilege_type
                                  )
                                  FROM pg_catalog.aclexplode(
                                      pg_catalog.acldefault(
                                          'r'::"char",
                                          class.relowner
                                      )
                                  ) AS acl
                                  WHERE acl.grantee = class.relowner
                                    AND acl.grantor = class.relowner
                              )
                          )
                      )
                ) AS relation_acl_exact,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_proc AS routine
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = routine.pronamespace
                    WHERE namespace.nspname = %s
                      AND (
                          routine.proowner <> namespace.nspowner
                          OR (
                              SELECT pg_catalog.array_agg(
                                  CASE
                                      WHEN acl.grantee = routine.proowner
                                          THEN 'owner'
                                      ELSE 'other:' || acl.grantee::text
                                  END || ':' ||
                                  CASE
                                      WHEN acl.grantor = routine.proowner
                                          THEN 'owner'
                                      ELSE 'other:' || acl.grantor::text
                                  END || ':' ||
                                  acl.privilege_type || ':' ||
                                  acl.is_grantable::text
                                  ORDER BY
                                      acl.grantee,
                                      acl.privilege_type
                              )
                              FROM pg_catalog.aclexplode(
                                  COALESCE(
                                      routine.proacl,
                                      pg_catalog.acldefault(
                                          'f'::"char",
                                          routine.proowner
                                      )
                                  )
                              ) AS acl
                          ) IS DISTINCT FROM (
                              SELECT pg_catalog.array_agg(
                                  'owner:owner:' ||
                                  acl.privilege_type || ':' ||
                                  acl.is_grantable::text
                                  ORDER BY acl.privilege_type
                              )
                              FROM pg_catalog.aclexplode(
                                  pg_catalog.acldefault(
                                      'f'::"char",
                                      routine.proowner
                                  )
                              ) AS acl
                              WHERE acl.grantee = routine.proowner
                                AND acl.grantor = routine.proowner
                          )
                      )
                ) AS function_acl_exact
            FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = %s
            """,
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [
            {
                "schema_acl_exact": True,
                "relation_acl_exact": True,
                "function_acl_exact": True,
            }
        ]:
            raise PostgresManagedIndexV3SchemaError(
                "PostgreSQL managed index v3 object ACL mismatch"
            )

    @staticmethod
    def _bundle_bytes(bundle: ManagedIndexBundle) -> bytes:
        payload = dumps_managed_index_bundle(bundle).encode("utf-8")
        if len(payload) > MANAGED_INDEX_BUNDLE_JSON_MAX_BYTES:
            raise ValueError("managed index bundle exceeds maximum bytes")
        return payload

    @staticmethod
    def _stored_bundle(row: object) -> ManagedIndexBundle:
        if type(row) is not dict:
            raise PostgresManagedIndexV3PersistenceError(
                "stored managed index bundle has invalid shape"
            )
        expected = {
            "bundle_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "retriever_id",
            "retriever_version",
            "source_catalog_sha256",
            "payload_utf8",
        }
        if set(row) != expected:
            raise PostgresManagedIndexV3PersistenceError(
                "stored managed index bundle has invalid shape"
            )
        payload = row["payload_utf8"]
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if type(payload) is not bytes:
            raise PostgresManagedIndexV3PersistenceError(
                "stored managed index bytes have invalid shape"
            )
        try:
            bundle = loads_managed_index_bundle(payload)
        except ValueError as error:
            raise PostgresManagedIndexV3PersistenceError(
                "stored managed index bundle is invalid"
            ) from error
        if (
            bundle.bundle_id != row["bundle_id"]
            or bundle.tenant_id != row["tenant_id"]
            or bundle.repository_id != row["repository_id"]
            or bundle.environment_id != row["environment_id"]
            or bundle.retriever_id != row["retriever_id"]
            or bundle.retriever_version != row["retriever_version"]
            or bundle.source_catalog_sha256 != row["source_catalog_sha256"]
            or dumps_managed_index_bundle(bundle).encode("utf-8") != payload
        ):
            raise PostgresManagedIndexV3PersistenceError(
                "stored managed index columns do not match exact bundle bytes"
            )
        return bundle

    @staticmethod
    def _validate_bundle_id(value: object) -> str:
        if type(value) is not str or _BUNDLE_ID_RE.fullmatch(value) is None:
            raise ValueError("bundle_id must be a managed index bundle ID")
        return value

    @staticmethod
    def _validate_scope(
        tenant_id: object,
        repository_id: object,
        environment_id: object,
    ) -> tuple[str, str, str]:
        values = (tenant_id, repository_id, environment_id)
        if any(
            type(value) is not str
            or not cast(str, value).strip()
            or len(cast(str, value)) > 128
            for value in values
        ):
            raise ValueError("managed index scope identifiers are invalid")
        return cast(tuple[str, str, str], values)

    @staticmethod
    def _select_bundle(
        cursor: object,
        bundle_id: str,
        *,
        lock: str = "",
    ) -> ManagedIndexBundle:
        cursor.execute(
            "SELECT bundle_id, tenant_id, repository_id, environment_id, "
            "retriever_id, retriever_version, source_catalog_sha256, "
            "payload_utf8 "
            "FROM trace_backed_memory_v3_managed_index."
            "v3_managed_index_bundles WHERE bundle_id = %s " + lock,
            (bundle_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise KeyError(bundle_id)
        if len(rows) != 1:
            raise PostgresManagedIndexV3PersistenceError(
                "managed index bundle lookup is ambiguous"
            )
        return PostgresManagedIndexV3Repository._stored_bundle(rows[0])

    @_synchronized
    def publish(
        self,
        bundle: ManagedIndexBundle,
        *,
        expected_current_bundle_id: str | None,
    ) -> ManagedIndexPublication:
        self._require_open()
        if type(bundle) is not ManagedIndexBundle:
            raise ValueError("bundle must be exactly ManagedIndexBundle")
        if expected_current_bundle_id is not None:
            self._validate_bundle_id(expected_current_bundle_id)
        payload = self._bundle_bytes(bundle)
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        "INSERT INTO trace_backed_memory_v3_managed_index."
                        "v3_managed_index_bundles "
                        "(bundle_id, tenant_id, repository_id, environment_id, "
                        "retriever_id, retriever_version, "
                        "source_catalog_sha256, payload_utf8) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            bundle.bundle_id,
                            bundle.tenant_id,
                            bundle.repository_id,
                            bundle.environment_id,
                            bundle.retriever_id,
                            bundle.retriever_version,
                            bundle.source_catalog_sha256,
                            payload,
                        ),
                    )
                    stored = self._select_bundle(
                        cursor,
                        bundle.bundle_id,
                        lock="FOR SHARE",
                    )
                    if stored != bundle:
                        raise PostgresManagedIndexV3ConflictError(
                            "managed index bundle ID already has different content"
                        )
                    scope = (
                        bundle.tenant_id,
                        bundle.repository_id,
                        bundle.environment_id,
                    )
                    cursor.execute(
                        "SELECT bundle_id, head_version "
                        "FROM trace_backed_memory_v3_managed_index."
                        "v3_managed_index_heads "
                        "WHERE tenant_id = %s AND repository_id = %s "
                        "AND environment_id = %s FOR UPDATE",
                        scope,
                    )
                    head_rows = cursor.fetchall()
                    if not head_rows:
                        if expected_current_bundle_id is not None:
                            raise PostgresManagedIndexV3ConflictError(
                                "managed index head does not match expected current bundle"
                            )
                        cursor.execute(
                            "INSERT INTO "
                            "trace_backed_memory_v3_managed_index."
                            "v3_managed_index_heads "
                            "(tenant_id, repository_id, environment_id, "
                            "bundle_id, head_version) "
                            "VALUES (%s, %s, %s, %s, 1) "
                            "ON CONFLICT "
                            "(tenant_id, repository_id, environment_id) "
                            "DO NOTHING",
                            (*scope, bundle.bundle_id),
                        )
                        inserted = cursor.rowcount == 1
                        cursor.execute(
                            "SELECT bundle_id, head_version "
                            "FROM trace_backed_memory_v3_managed_index."
                            "v3_managed_index_heads "
                            "WHERE tenant_id = %s AND repository_id = %s "
                            "AND environment_id = %s FOR UPDATE",
                            scope,
                        )
                        head_rows = cursor.fetchall()
                        if (
                            len(head_rows) != 1
                            or head_rows[0]["bundle_id"] != bundle.bundle_id
                            or head_rows[0]["head_version"] != 1
                        ):
                            raise PostgresManagedIndexV3ConflictError(
                                "managed index head changed during publication"
                            )
                        previous = None if inserted else bundle.bundle_id
                        head_version = 1
                        changed = inserted
                    else:
                        if (
                            len(head_rows) != 1
                            or set(head_rows[0]) != {"bundle_id", "head_version"}
                            or type(head_rows[0]["bundle_id"]) is not str
                            or type(head_rows[0]["head_version"]) is not int
                        ):
                            raise PostgresManagedIndexV3PersistenceError(
                                "managed index head has invalid shape"
                            )
                        current = cast(str, head_rows[0]["bundle_id"])
                        current_version = cast(
                            int,
                            head_rows[0]["head_version"],
                        )
                        if current == bundle.bundle_id:
                            previous = current
                            head_version = current_version
                            changed = False
                        else:
                            if expected_current_bundle_id != current:
                                raise PostgresManagedIndexV3ConflictError(
                                    "managed index head does not match expected current bundle"
                                )
                            cursor.execute(
                                "UPDATE "
                                "trace_backed_memory_v3_managed_index."
                                "v3_managed_index_heads "
                                "SET bundle_id = %s, "
                                "head_version = head_version + 1 "
                                "WHERE tenant_id = %s "
                                "AND repository_id = %s "
                                "AND environment_id = %s "
                                "AND bundle_id = %s AND head_version = %s",
                                (
                                    bundle.bundle_id,
                                    *scope,
                                    current,
                                    current_version,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise PostgresManagedIndexV3ConflictError(
                                    "managed index head changed during publication"
                                )
                            previous = current
                            head_version = current_version + 1
                            changed = True
                    cursor.execute(
                        "SELECT bundle_id, head_version "
                        "FROM trace_backed_memory_v3_managed_index."
                        "v3_managed_index_heads "
                        "WHERE tenant_id = %s AND repository_id = %s "
                        "AND environment_id = %s",
                        scope,
                    )
                    readback = cursor.fetchall()
                    if readback != [
                        {
                            "bundle_id": bundle.bundle_id,
                            "head_version": head_version,
                        }
                    ]:
                        raise PostgresManagedIndexV3PersistenceError(
                            "managed index publication read-back failed"
                        )
                    return ManagedIndexPublication(
                        bundle=stored,
                        previous_bundle_id=previous,
                        head_version=head_version,
                        changed=changed,
                    )
        except PostgresManagedIndexV3Error:
            raise
        except Exception as error:
            self._raise_postgres(error)

    @_synchronized
    def load(self, bundle_id: str) -> ManagedIndexBundle:
        self._require_open()
        validated = self._validate_bundle_id(bundle_id)
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._select_bundle(cursor, validated)
        except (KeyError, PostgresManagedIndexV3Error):
            raise
        except Exception as error:
            self._raise_postgres(error)

    @_synchronized
    def load_current(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        environment_id: str,
    ) -> ManagedIndexBundle:
        self._require_open()
        scope = self._validate_scope(
            tenant_id,
            repository_id,
            environment_id,
        )
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        "SELECT bundle_id "
                        "FROM trace_backed_memory_v3_managed_index."
                        "v3_managed_index_heads "
                        "WHERE tenant_id = %s AND repository_id = %s "
                        "AND environment_id = %s FOR SHARE",
                        scope,
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        raise KeyError(scope)
                    if (
                        len(rows) != 1
                        or set(rows[0]) != {"bundle_id"}
                        or type(rows[0]["bundle_id"]) is not str
                    ):
                        raise PostgresManagedIndexV3PersistenceError(
                            "managed index head has invalid shape"
                        )
                    bundle = self._select_bundle(
                        cursor,
                        cast(str, rows[0]["bundle_id"]),
                        lock="FOR SHARE",
                    )
                    if (
                        bundle.tenant_id,
                        bundle.repository_id,
                        bundle.environment_id,
                    ) != scope:
                        raise PostgresManagedIndexV3PersistenceError(
                            "managed index head scope does not match bundle"
                        )
                    return bundle
        except (KeyError, PostgresManagedIndexV3Error):
            raise
        except Exception as error:
            self._raise_postgres(error)

    def _raise_postgres(self, error: Exception) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresManagedIndexV3SchemaError(_MISSING_SCHEMA_MESSAGE) from error
        if sqlstate in {"23505", "23503", "23514", "P0001"}:
            raise PostgresManagedIndexV3ConflictError(
                "managed index persistence conflict"
            ) from error
        raise PostgresManagedIndexV3PersistenceError(
            "PostgreSQL managed index v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresManagedIndexV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "POSTGRES_MANAGED_INDEX_V3_SCHEMA_VERSION",
    "PostgresManagedIndexV3ConflictError",
    "PostgresManagedIndexV3Error",
    "PostgresManagedIndexV3PersistenceError",
    "PostgresManagedIndexV3Repository",
    "PostgresManagedIndexV3SchemaError",
]
