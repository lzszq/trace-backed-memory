from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .contracts_v3 import V3ContractError
from .postgres import _load_psycopg
from .replay_v3 import (
    ARTIFACT_MAX_BYTES,
    INJECTION_ARTIFACT_MAX_BYTES,
    REPLAY_JSON_MAX_BYTES,
    ContentAddressedArtifact,
    DataClassification,
    DecisionReplayManifest,
    InjectionArtifact,
    ReplayContractError,
    StoredReplayArtifact,
    dumps_decision_replay_manifest,
    dumps_injection_artifact,
    loads_decision_replay_manifest,
    loads_injection_artifact,
    verify_artifact_content,
)
from .resources import PackagedResourceError, read_packaged_resource


POSTGRES_REPLAY_V3_SCHEMA_VERSION = 1
_SCHEMA = "trace_backed_memory_v3_replay"
_MISSING_SCHEMA_MESSAGE = "PostgreSQL replay v3 schema is missing or incomplete"
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_ARTIFACT_ID_RE = re.compile(r"artifact_sha256_[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_USAGE_DECISION_ID_RE = re.compile(r"usage_decision_sha256_[0-9a-f]{64}")
_EXPECTED_RELATIONS = frozenset(
    {
        "replay_artifacts",
        "replay_artifacts_content_sha256_key",
        "replay_artifacts_pkey",
        "replay_injections",
        "replay_injections_decision",
        "replay_injections_linkage_key",
        "replay_injections_pkey",
        "replay_manifests",
        "replay_manifests_decision",
        "replay_manifests_pkey",
        "replay_schema_metadata_pkey",
        "schema_metadata",
    }
)
_EXPECTED_FUNCTIONS = frozenset(
    {"reject_immutable_change", "validate_injection_artifact"}
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "replay_artifacts_immutable",
        "replay_artifacts_no_truncate",
        "replay_injections_immutable",
        "replay_injections_no_truncate",
        "replay_injections_validate_artifact",
        "replay_manifests_immutable",
        "replay_manifests_no_truncate",
        "replay_schema_metadata_immutable",
        "replay_schema_metadata_no_truncate",
    }
)
_EXPECTED_TRIGGER_SHAPES = frozenset(
    {
        (
            "replay_artifacts_immutable",
            "replay_artifacts",
            "reject_immutable_change",
            27,
        ),
        (
            "replay_artifacts_no_truncate",
            "replay_artifacts",
            "reject_immutable_change",
            34,
        ),
        (
            "replay_injections_immutable",
            "replay_injections",
            "reject_immutable_change",
            27,
        ),
        (
            "replay_injections_no_truncate",
            "replay_injections",
            "reject_immutable_change",
            34,
        ),
        (
            "replay_injections_validate_artifact",
            "replay_injections",
            "validate_injection_artifact",
            7,
        ),
        (
            "replay_manifests_immutable",
            "replay_manifests",
            "reject_immutable_change",
            27,
        ),
        (
            "replay_manifests_no_truncate",
            "replay_manifests",
            "reject_immutable_change",
            34,
        ),
        (
            "replay_schema_metadata_immutable",
            "schema_metadata",
            "reject_immutable_change",
            27,
        ),
        (
            "replay_schema_metadata_no_truncate",
            "schema_metadata",
            "reject_immutable_change",
            34,
        ),
    }
)
_EXPECTED_CONSTRAINTS = frozenset(
    {
        "replay_artifacts_artifact_id_check",
        "replay_artifacts_classification_check",
        "replay_artifacts_content_check",
        "replay_artifacts_content_sha256_check",
        "replay_artifacts_content_sha256_key",
        "replay_artifacts_created_at_check",
        "replay_artifacts_derived_id_check",
        "replay_artifacts_encryption_key_id_check",
        "replay_artifacts_media_type_check",
        "replay_artifacts_pkey",
        "replay_artifacts_redaction_policy_id_check",
        "replay_artifacts_size_bytes_check",
        "replay_injections_artifact_fkey",
        "replay_injections_decision_id_check",
        "replay_injections_descriptor_check",
        "replay_injections_linkage_key",
        "replay_injections_pkey",
        "replay_injections_session_id_check",
        "replay_injections_usage_decision_id_check",
        "replay_manifests_completeness_check",
        "replay_manifests_decision_id_check",
        "replay_manifests_descriptor_check",
        "replay_manifests_injection_fkey",
        "replay_manifests_injection_shape",
        "replay_manifests_manifest_sha256_check",
        "replay_manifests_pkey",
        "replay_manifests_session_id_check",
        "replay_manifests_usage_decision_id_check",
        "replay_schema_metadata_contract_version_check",
        "replay_schema_metadata_pkey",
        "replay_schema_metadata_schema_version_check",
        "replay_schema_metadata_singleton_check",
    }
)
_EXPECTED_COLUMNS = frozenset(
    {
        ("replay_artifacts", "artifact_id", "text", "NO", "C"),
        ("replay_artifacts", "classification", "text", "NO", "C"),
        ("replay_artifacts", "content", "bytea", "NO", None),
        ("replay_artifacts", "content_sha256", "text", "NO", "C"),
        ("replay_artifacts", "created_at", "text", "NO", "C"),
        ("replay_artifacts", "encryption_key_id", "text", "YES", "C"),
        ("replay_artifacts", "media_type", "text", "NO", "C"),
        ("replay_artifacts", "redaction_policy_id", "text", "YES", "C"),
        ("replay_artifacts", "size_bytes", "integer", "NO", None),
        ("replay_injections", "artifact_id", "text", "NO", "C"),
        ("replay_injections", "decision_id", "text", "NO", "C"),
        ("replay_injections", "descriptor", "text", "NO", "C"),
        ("replay_injections", "session_id", "text", "NO", "C"),
        ("replay_injections", "usage_decision_id", "text", "NO", "C"),
        ("replay_manifests", "completeness", "text", "NO", "C"),
        ("replay_manifests", "decision_id", "text", "NO", "C"),
        ("replay_manifests", "descriptor", "text", "NO", "C"),
        (
            "replay_manifests",
            "injection_artifact_id",
            "text",
            "YES",
            "C",
        ),
        ("replay_manifests", "manifest_sha256", "text", "NO", "C"),
        ("replay_manifests", "session_id", "text", "NO", "C"),
        ("replay_manifests", "usage_decision_id", "text", "NO", "C"),
        ("schema_metadata", "contract_version", "text", "NO", "C"),
        ("schema_metadata", "schema_version", "integer", "NO", None),
        ("schema_metadata", "singleton", "boolean", "NO", None),
    }
)
_FUNCTION_BODY_PATTERN = re.compile(
    r"CREATE FUNCTION\s+"
    r"trace_backed_memory_v3_replay\.([a-z_]+)\(\)"
    r".*?AS \$\$(.*?)\$\$;",
    re.DOTALL,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresReplayV3Error(V3ContractError):
    """Stable base failure for the isolated PostgreSQL replay ledger."""


class PostgresReplayV3SchemaError(PostgresReplayV3Error):
    pass


class PostgresReplayV3ConflictError(PostgresReplayV3Error):
    pass


class PostgresReplayV3PersistenceError(PostgresReplayV3Error):
    pass


@dataclass(frozen=True)
class PostgresReplayV3StoreResult:
    artifact_id: str
    artifact_inserted: bool
    injection_inserted: bool
    manifest_sha256: str | None
    manifest_inserted: bool


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
        source = read_packaged_resource("schemas/postgres-v3-replay.sql").decode(
            "utf-8"
        )
    except (PackagedResourceError, UnicodeError) as error:
        raise PostgresReplayV3SchemaError(
            "TBM_POSTGRES_REPLAY_SCHEMA",
            "could not read canonical PostgreSQL replay schema",
        ) from error
    bodies = {
        name: body.replace("\r\n", "\n").strip()
        for name, body in _FUNCTION_BODY_PATTERN.findall(source)
    }
    if frozenset(bodies) != _EXPECTED_FUNCTIONS:
        raise PostgresReplayV3SchemaError(
            "TBM_POSTGRES_REPLAY_SCHEMA",
            "canonical PostgreSQL replay functions are incomplete",
        )
    return bodies


class PostgresReplayV3Repository:
    """Immutable exact-byte replay records in an isolated PostgreSQL schema."""

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

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresReplayV3Repository:
        psycopg, dict_row, _Jsonb = _load_psycopg()
        try:
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except psycopg.Error as error:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "failed to connect to PostgreSQL",
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_CLOSED",
                "PostgreSQL replay repository is closed",
            )

    @staticmethod
    def _catalog_names(cursor: object, query: str) -> frozenset[str]:
        cursor.execute(query, (_SCHEMA,))
        rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping) or type(row.get("name")) is not str
            for row in rows
        ):
            PostgresReplayV3Repository._schema_drift()
        return frozenset(cast(str, row["name"]) for row in rows)

    def _lock_schema(self, cursor: object) -> None:
        cursor.execute(
            """
            SELECT active.schema_version AS active_schema_version,
                   replay.schema_version AS replay_schema_version,
                   replay.contract_version AS contract_version
            FROM public.trace_backed_memory_schema AS active
            CROSS JOIN trace_backed_memory_v3_replay.schema_metadata AS replay
            WHERE active.singleton AND replay.singleton
            FOR SHARE OF active, replay
            """
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or rows[0].get("active_schema_version") != 2
            or rows[0].get("replay_schema_version") != POSTGRES_REPLAY_V3_SCHEMA_VERSION
            or rows[0].get("contract_version") != "tbm.replay.v3"
        ):
            raise PostgresReplayV3SchemaError(
                "TBM_POSTGRES_REPLAY_SCHEMA",
                "PostgreSQL replay schema metadata mismatch",
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

    @staticmethod
    def _schema_drift() -> NoReturn:
        raise PostgresReplayV3SchemaError(
            "TBM_POSTGRES_REPLAY_SCHEMA",
            "PostgreSQL replay schema definitions do not match",
        )

    @staticmethod
    def _artifact_values(
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> tuple[object, ...]:
        return PostgresReplayV3Repository._artifact_descriptor_values(
            artifact
        ) + (content,)

    @staticmethod
    def _artifact_descriptor_values(
        artifact: ContentAddressedArtifact,
    ) -> tuple[object, ...]:
        payload = artifact.to_dict()
        return (
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.size_bytes,
            artifact.media_type,
            artifact.classification,
            payload["created_at"],
            artifact.encryption_key_id,
            artifact.redaction_policy_id,
        )

    @staticmethod
    def _injection_values(
        injection: InjectionArtifact,
    ) -> tuple[object, ...]:
        return (
            injection.artifact.artifact_id,
            injection.session_id,
            injection.decision_id,
            injection.usage_decision_id,
            dumps_injection_artifact(injection),
        )

    @staticmethod
    def _manifest_values(
        manifest: DecisionReplayManifest,
    ) -> tuple[object, ...]:
        return (
            manifest.manifest_sha256,
            manifest.session_id,
            manifest.decision_id,
            manifest.usage_decision_id,
            manifest.injection_artifact_id,
            manifest.completeness,
            dumps_decision_replay_manifest(manifest),
        )

    @staticmethod
    def _mapping_values(
        row: Mapping[str, object],
        fields: tuple[str, ...],
    ) -> tuple[object, ...]:
        try:
            return tuple(row[field] for field in fields)
        except KeyError as error:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay row has an invalid shape",
            ) from error

    @classmethod
    def _stored_artifact_descriptor(
        cls,
        row: Mapping[str, object],
    ) -> ContentAddressedArtifact:
        fields = (
            "artifact_id",
            "content_sha256",
            "size_bytes",
            "media_type",
            "classification",
            "created_at",
            "encryption_key_id",
            "redaction_policy_id",
        )
        values = cls._mapping_values(row, fields)
        try:
            artifact = ContentAddressedArtifact(
                artifact_id=cast(str, values[0]),
                content_sha256=cast(str, values[1]),
                size_bytes=cast(int, values[2]),
                media_type=cast(str, values[3]),
                classification=cast(DataClassification, values[4]),
                created_at=cast(str, values[5]),
                encryption_key_id=cast(str | None, values[6]),
                redaction_policy_id=cast(str | None, values[7]),
            )
        except ReplayContractError as error:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact metadata failed validation",
            ) from error
        if values != cls._artifact_descriptor_values(artifact):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact descriptor columns do not match",
            )
        return artifact

    @classmethod
    def _stored_artifact(
        cls,
        row: Mapping[str, object],
    ) -> StoredReplayArtifact:
        artifact = cls._stored_artifact_descriptor(row)
        content = row.get("content")
        if isinstance(content, memoryview):
            content = content.tobytes()
        if type(content) is not bytes:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact bytes have an invalid shape",
            )
        if artifact.classification not in {"public", "internal"}:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay cannot load sensitive artifact bytes",
            )
        expected = cls._artifact_values(artifact, content)
        actual = cls._mapping_values(
            row,
            (
                "artifact_id",
                "content_sha256",
                "size_bytes",
                "media_type",
                "classification",
                "created_at",
                "encryption_key_id",
                "redaction_policy_id",
            ),
        ) + (content,)
        if actual != expected or not verify_artifact_content(artifact, content):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact columns or bytes do not match",
            )
        return StoredReplayArtifact(artifact=artifact, content=content)

    @classmethod
    def _stored_injection(
        cls,
        row: Mapping[str, object],
    ) -> InjectionArtifact:
        values = cls._mapping_values(
            row,
            (
                "artifact_id",
                "session_id",
                "decision_id",
                "usage_decision_id",
                "descriptor",
            ),
        )
        if type(values[4]) is not str:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection row has an invalid shape",
            )
        try:
            injection = loads_injection_artifact(values[4])
        except ReplayContractError as error:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection descriptor failed validation",
            ) from error
        if values != cls._injection_values(injection):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection columns do not match descriptor",
            )
        return injection

    @classmethod
    def _stored_manifest(
        cls,
        row: Mapping[str, object],
    ) -> DecisionReplayManifest:
        values = cls._mapping_values(
            row,
            (
                "manifest_sha256",
                "session_id",
                "decision_id",
                "usage_decision_id",
                "injection_artifact_id",
                "completeness",
                "descriptor",
            ),
        )
        if type(values[6]) is not str:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay manifest row has an invalid shape",
            )
        try:
            manifest = loads_decision_replay_manifest(values[6])
        except ReplayContractError as error:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay manifest descriptor failed validation",
            ) from error
        if values != cls._manifest_values(manifest):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay manifest columns do not match descriptor",
            )
        return manifest

    @staticmethod
    def _select_one(
        cursor: object,
        query: str,
        parameters: tuple[object, ...],
    ) -> Mapping[str, object] | None:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
        if len(rows) > 1 or (rows and not isinstance(rows[0], Mapping)):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay query returned an invalid result",
            )
        return cast(Mapping[str, object] | None, rows[0] if rows else None)

    def _put_artifact(
        self,
        cursor: object,
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> bool:
        if type(artifact) is not ContentAddressedArtifact:
            raise ValueError("artifact must be exactly ContentAddressedArtifact")
        if type(content) is not bytes:
            raise ValueError("content must be bytes")
        if artifact.classification not in {"public", "internal"}:
            raise ValueError(
                "PostgreSQL replay requires an encryption provider for "
                "sensitive artifacts"
            )
        if not verify_artifact_content(artifact, content):
            raise ValueError("content does not match artifact")
        values = self._artifact_values(artifact, content)
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_replay.replay_artifacts (
                artifact_id, content_sha256, size_bytes, media_type,
                classification, created_at, encryption_key_id,
                redaction_policy_id, content
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING artifact_id
            """,
            values,
        )
        inserted = bool(cursor.fetchall())
        stored = self._select_one(
            cursor,
            """
            SELECT artifact_id, content_sha256, size_bytes, media_type,
                   classification, created_at, encryption_key_id,
                   redaction_policy_id, content
            FROM trace_backed_memory_v3_replay.replay_artifacts
            WHERE artifact_id = %s
            """,
            (artifact.artifact_id,),
        )
        if stored is None:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact disappeared after insert",
            )
        loaded = self._stored_artifact(stored)
        if (
            self._artifact_values(
                loaded.artifact,
                loaded.content,
            )
            != values
        ):
            raise PostgresReplayV3ConflictError(
                "TBM_POSTGRES_REPLAY_CONFLICT",
                "PostgreSQL replay artifact has conflicting content",
            )
        return inserted

    def _put_injection(
        self,
        cursor: object,
        injection: InjectionArtifact,
    ) -> bool:
        values = self._injection_values(injection)
        if len(cast(str, values[4]).encode("utf-8")) > REPLAY_JSON_MAX_BYTES:
            raise ValueError("injection descriptor exceeds storage limit")
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_replay.replay_injections (
                artifact_id, session_id, decision_id, usage_decision_id,
                descriptor
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (artifact_id) DO NOTHING
            RETURNING artifact_id
            """,
            values,
        )
        inserted = bool(cursor.fetchall())
        stored = self._select_one(
            cursor,
            """
            SELECT artifact_id, session_id, decision_id, usage_decision_id,
                   descriptor
            FROM trace_backed_memory_v3_replay.replay_injections
            WHERE artifact_id = %s
            """,
            (injection.artifact.artifact_id,),
        )
        if stored is None:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection disappeared after insert",
            )
        loaded = self._stored_injection(stored)
        if self._injection_values(loaded) != values:
            raise PostgresReplayV3ConflictError(
                "TBM_POSTGRES_REPLAY_CONFLICT",
                "PostgreSQL replay injection has conflicting content",
            )
        return inserted

    def _put_manifest(
        self,
        cursor: object,
        manifest: DecisionReplayManifest,
    ) -> bool:
        values = self._manifest_values(manifest)
        if len(cast(str, values[6]).encode("utf-8")) > REPLAY_JSON_MAX_BYTES:
            raise ValueError("replay manifest exceeds storage limit")
        if manifest.injection_artifact_id is not None:
            injection_row = self._select_one(
                cursor,
                """
                SELECT artifact_id, session_id, decision_id,
                       usage_decision_id, descriptor
                FROM trace_backed_memory_v3_replay.replay_injections
                WHERE artifact_id = %s
                """,
                (manifest.injection_artifact_id,),
            )
            if injection_row is None:
                raise PostgresReplayV3ConflictError(
                    "TBM_POSTGRES_REPLAY_CONFLICT",
                    "replay manifest references an unknown injection",
                )
            injection = self._stored_injection(injection_row)
            if (
                manifest.session_id != injection.session_id
                or manifest.decision_id != injection.decision_id
                or manifest.usage_decision_id != injection.usage_decision_id
            ):
                raise PostgresReplayV3ConflictError(
                    "TBM_POSTGRES_REPLAY_CONFLICT",
                    "replay manifest linkage conflicts with injection",
                )
        cursor.execute(
            """
            INSERT INTO trace_backed_memory_v3_replay.replay_manifests (
                manifest_sha256, session_id, decision_id, usage_decision_id,
                injection_artifact_id, completeness, descriptor
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (manifest_sha256) DO NOTHING
            RETURNING manifest_sha256
            """,
            values,
        )
        inserted = bool(cursor.fetchall())
        stored = self._select_one(
            cursor,
            """
            SELECT manifest_sha256, session_id, decision_id,
                   usage_decision_id, injection_artifact_id, completeness,
                   descriptor
            FROM trace_backed_memory_v3_replay.replay_manifests
            WHERE manifest_sha256 = %s
            """,
            (manifest.manifest_sha256,),
        )
        if stored is None:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay manifest disappeared after insert",
            )
        loaded = self._stored_manifest(stored)
        if self._manifest_values(loaded) != values:
            raise PostgresReplayV3ConflictError(
                "TBM_POSTGRES_REPLAY_CONFLICT",
                "PostgreSQL replay manifest has conflicting content",
            )
        return inserted

    def _load_artifact_descriptor(
        self,
        cursor: object,
        artifact_id: str,
    ) -> ContentAddressedArtifact:
        stored = self._select_one(
            cursor,
            """
            SELECT artifact_id, content_sha256, size_bytes, media_type,
                   classification, created_at, encryption_key_id,
                   redaction_policy_id,
                   pg_catalog.octet_length(content) AS content_size
            FROM trace_backed_memory_v3_replay.replay_artifacts
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        )
        if stored is None:
            raise KeyError(artifact_id)
        size_bytes = stored.get("size_bytes")
        content_size = stored.get("content_size")
        if (
            type(size_bytes) is not int
            or type(content_size) is not int
            or not 0 <= cast(int, size_bytes) <= ARTIFACT_MAX_BYTES
            or content_size != size_bytes
        ):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact exceeds bounded load contract",
            )
        artifact = self._stored_artifact_descriptor(stored)
        if artifact.size_bytes != content_size:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact size differs from stored content",
            )
        if artifact.classification not in {"public", "internal"}:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay cannot preflight sensitive artifact bytes",
            )
        return artifact

    def _load_artifact(
        self,
        cursor: object,
        artifact_id: str,
    ) -> StoredReplayArtifact:
        self._load_artifact_descriptor(cursor, artifact_id)
        stored = self._select_one(
            cursor,
            """
            SELECT artifact_id, content_sha256, size_bytes, media_type,
                   classification, created_at, encryption_key_id,
                   redaction_policy_id, content
            FROM trace_backed_memory_v3_replay.replay_artifacts
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        )
        if stored is None:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay artifact disappeared during load",
            )
        return self._stored_artifact(stored)

    def _load_injection(
        self,
        cursor: object,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]:
        size = self._select_one(
            cursor,
            """
            SELECT pg_catalog.octet_length(descriptor) AS descriptor_size
            FROM trace_backed_memory_v3_replay.replay_injections
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        )
        if size is None:
            raise KeyError(artifact_id)
        descriptor_size = size.get("descriptor_size")
        if (
            type(descriptor_size) is not int
            or not 1 <= cast(int, descriptor_size) <= REPLAY_JSON_MAX_BYTES
        ):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection exceeds bounded load contract",
            )
        stored = self._select_one(
            cursor,
            """
            SELECT artifact_id, session_id, decision_id, usage_decision_id,
                   descriptor
            FROM trace_backed_memory_v3_replay.replay_injections
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        )
        if stored is None:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection disappeared during load",
            )
        injection = self._stored_injection(stored)
        artifact = self._load_artifact(cursor, artifact_id)
        if injection.artifact != artifact.artifact:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection artifact linkage differs",
            )
        if artifact.artifact.size_bytes > INJECTION_ARTIFACT_MAX_BYTES:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay injection bytes exceed bound",
            )
        return injection, artifact.content

    def _load_manifest(
        self,
        cursor: object,
        manifest_sha256: str,
    ) -> DecisionReplayManifest:
        size = self._select_one(
            cursor,
            """
            SELECT pg_catalog.octet_length(descriptor) AS descriptor_size
            FROM trace_backed_memory_v3_replay.replay_manifests
            WHERE manifest_sha256 = %s
            """,
            (manifest_sha256,),
        )
        if size is None:
            raise KeyError(manifest_sha256)
        descriptor_size = size.get("descriptor_size")
        if (
            type(descriptor_size) is not int
            or not 1 <= cast(int, descriptor_size) <= REPLAY_JSON_MAX_BYTES
        ):
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay manifest exceeds bounded load contract",
            )
        stored = self._select_one(
            cursor,
            """
            SELECT manifest_sha256, session_id, decision_id,
                   usage_decision_id, injection_artifact_id,
                   completeness, descriptor
            FROM trace_backed_memory_v3_replay.replay_manifests
            WHERE manifest_sha256 = %s
            """,
            (manifest_sha256,),
        )
        if stored is None:
            raise PostgresReplayV3PersistenceError(
                "TBM_POSTGRES_REPLAY_PERSISTENCE",
                "PostgreSQL replay manifest disappeared during load",
            )
        manifest = self._stored_manifest(stored)
        if manifest.injection_artifact_id is not None:
            injection_size = self._select_one(
                cursor,
                """
                SELECT pg_catalog.octet_length(descriptor)
                           AS descriptor_size
                FROM trace_backed_memory_v3_replay.replay_injections
                WHERE artifact_id = %s
                """,
                (manifest.injection_artifact_id,),
            )
            if injection_size is None:
                raise PostgresReplayV3PersistenceError(
                    "TBM_POSTGRES_REPLAY_PERSISTENCE",
                    "PostgreSQL replay manifest references an unknown injection",
                )
            stored_size = injection_size.get("descriptor_size")
            if (
                type(stored_size) is not int
                or not 1 <= cast(int, stored_size) <= REPLAY_JSON_MAX_BYTES
            ):
                raise PostgresReplayV3PersistenceError(
                    "TBM_POSTGRES_REPLAY_PERSISTENCE",
                    "PostgreSQL replay injection exceeds bounded load contract",
                )
            injection_row = self._select_one(
                cursor,
                """
                SELECT artifact_id, session_id, decision_id,
                       usage_decision_id, descriptor
                FROM trace_backed_memory_v3_replay.replay_injections
                WHERE artifact_id = %s
                """,
                (manifest.injection_artifact_id,),
            )
            if injection_row is None:
                raise PostgresReplayV3PersistenceError(
                    "TBM_POSTGRES_REPLAY_PERSISTENCE",
                    "PostgreSQL replay injection disappeared during "
                    "manifest load",
                )
            injection = self._stored_injection(injection_row)
            if (
                manifest.session_id != injection.session_id
                or manifest.decision_id != injection.decision_id
                or manifest.usage_decision_id != injection.usage_decision_id
            ):
                raise PostgresReplayV3PersistenceError(
                    "TBM_POSTGRES_REPLAY_PERSISTENCE",
                    "PostgreSQL replay manifest linkage differs from injection",
                )
        return manifest

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    @_synchronized
    def store_artifact(
        self,
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> bool:
        self._require_open()
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._put_artifact(cursor, artifact, content)
        except (
            PostgresReplayV3ConflictError,
            PostgresReplayV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to store replay artifact")

    @_synchronized
    def store_injection(
        self,
        injection: InjectionArtifact,
        content: bytes,
    ) -> PostgresReplayV3StoreResult:
        self._require_open()
        if type(injection) is not InjectionArtifact:
            raise ValueError("injection must be exactly InjectionArtifact")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    artifact_inserted = self._put_artifact(
                        cursor,
                        injection.artifact,
                        content,
                    )
                    injection_inserted = self._put_injection(cursor, injection)
            return PostgresReplayV3StoreResult(
                artifact_id=injection.artifact.artifact_id,
                artifact_inserted=artifact_inserted,
                injection_inserted=injection_inserted,
                manifest_sha256=None,
                manifest_inserted=False,
            )
        except (
            PostgresReplayV3ConflictError,
            PostgresReplayV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to store replay injection",
            )

    @_synchronized
    def store_manifest(self, manifest: DecisionReplayManifest) -> bool:
        self._require_open()
        if type(manifest) is not DecisionReplayManifest:
            raise ValueError("manifest must be exactly DecisionReplayManifest")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._put_manifest(cursor, manifest)
        except (
            PostgresReplayV3ConflictError,
            PostgresReplayV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to store replay manifest")

    @_synchronized
    def store_bundle(
        self,
        injection: InjectionArtifact,
        content: bytes,
        manifest: DecisionReplayManifest,
    ) -> PostgresReplayV3StoreResult:
        self._require_open()
        if (
            type(injection) is not InjectionArtifact
            or type(manifest) is not DecisionReplayManifest
        ):
            raise ValueError("injection and manifest must be exact replay records")
        if (
            manifest.injection_artifact_id != injection.artifact.artifact_id
            or manifest.session_id != injection.session_id
            or manifest.decision_id != injection.decision_id
            or manifest.usage_decision_id != injection.usage_decision_id
        ):
            raise ValueError("manifest and injection linkage must match exactly")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    artifact_inserted = self._put_artifact(
                        cursor,
                        injection.artifact,
                        content,
                    )
                    injection_inserted = self._put_injection(cursor, injection)
                    manifest_inserted = self._put_manifest(cursor, manifest)
            return PostgresReplayV3StoreResult(
                artifact_id=injection.artifact.artifact_id,
                artifact_inserted=artifact_inserted,
                injection_inserted=injection_inserted,
                manifest_sha256=manifest.manifest_sha256,
                manifest_inserted=manifest_inserted,
            )
        except (
            PostgresReplayV3ConflictError,
            PostgresReplayV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to store replay bundle")

    @_synchronized
    def store_complete_bundle(
        self,
        supporting_artifacts: tuple[StoredReplayArtifact, ...],
        injection: InjectionArtifact,
        content: bytes,
        manifest: DecisionReplayManifest,
    ) -> PostgresReplayV3StoreResult:
        """Atomically retain supporting bytes plus one injection/manifest."""

        self._require_open()
        _validate_complete_bundle_inputs(
            supporting_artifacts,
            injection,
            manifest,
        )
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    for stored in supporting_artifacts:
                        self._put_artifact(
                            cursor,
                            stored.artifact,
                            stored.content,
                        )
                    artifact_inserted = self._put_artifact(
                        cursor,
                        injection.artifact,
                        content,
                    )
                    injection_inserted = self._put_injection(
                        cursor,
                        injection,
                    )
                    manifest_inserted = self._put_manifest(cursor, manifest)
            return PostgresReplayV3StoreResult(
                artifact_id=injection.artifact.artifact_id,
                artifact_inserted=artifact_inserted,
                injection_inserted=injection_inserted,
                manifest_sha256=manifest.manifest_sha256,
                manifest_inserted=manifest_inserted,
            )
        except (
            PostgresReplayV3ConflictError,
            PostgresReplayV3SchemaError,
        ):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to store complete replay bundle",
            )

    @_synchronized
    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> ContentAddressedArtifact:
        self._require_open()
        _validate_artifact_id(artifact_id)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._load_artifact_descriptor(
                        cursor,
                        artifact_id,
                    )
        except (KeyError, PostgresReplayV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load replay artifact descriptor",
            )

    @_synchronized
    def load_artifact(self, artifact_id: str) -> StoredReplayArtifact:
        self._require_open()
        _validate_artifact_id(artifact_id)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._load_artifact(cursor, artifact_id)
        except (KeyError, PostgresReplayV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to load replay artifact")

    @_synchronized
    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]:
        self._require_open()
        _validate_artifact_id(artifact_id)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._load_injection(cursor, artifact_id)
        except (KeyError, PostgresReplayV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to load replay injection")

    @_synchronized
    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> DecisionReplayManifest:
        self._require_open()
        _validate_digest(manifest_sha256, "manifest_sha256")
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    return self._load_manifest(cursor, manifest_sha256)
        except (KeyError, PostgresReplayV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(error, "failed to load replay manifest")

    @_synchronized
    def load_manifest_for_session(
        self,
        session_id: str,
        decision_id: str,
        usage_decision_id: str,
        injection_artifact_id: str,
    ) -> DecisionReplayManifest:
        """Resolve one exact retained manifest without accepting a content ID."""

        self._require_open()
        _validate_identifier(session_id, "session_id")
        _validate_identifier(decision_id, "decision_id")
        _validate_identifier(usage_decision_id, "usage_decision_id")
        _validate_artifact_id(injection_artifact_id)
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        try:
            with self._connection.transaction():
                with self._cursor() as cursor:
                    self._lock_schema(cursor)
                    cursor.execute(
                        """
                        SELECT manifest_sha256
                        FROM trace_backed_memory_v3_replay.replay_manifests
                        WHERE session_id = %s
                          AND decision_id = %s
                          AND usage_decision_id = %s
                          AND injection_artifact_id = %s
                        ORDER BY manifest_sha256
                        LIMIT 2
                        """,
                        (
                            session_id,
                            decision_id,
                            usage_decision_id,
                            injection_artifact_id,
                        ),
                    )
                    matches = cursor.fetchall()
                    if not matches:
                        raise KeyError(session_id)
                    if (
                        len(matches) != 1
                        or not isinstance(matches[0], Mapping)
                        or type(matches[0].get("manifest_sha256")) is not str
                    ):
                        raise PostgresReplayV3PersistenceError(
                            "TBM_POSTGRES_REPLAY_PERSISTENCE",
                            "PostgreSQL replay session manifest linkage "
                            "is ambiguous",
                        )
                    manifest = self._load_manifest(
                        cursor,
                        cast(str, matches[0]["manifest_sha256"]),
                    )
                    if (
                        manifest.session_id != session_id
                        or manifest.decision_id != decision_id
                        or manifest.usage_decision_id != usage_decision_id
                        or manifest.injection_artifact_id
                        != injection_artifact_id
                    ):
                        raise PostgresReplayV3PersistenceError(
                            "TBM_POSTGRES_REPLAY_PERSISTENCE",
                            "PostgreSQL replay session manifest linkage "
                            "is invalid",
                        )
                    return manifest
        except (KeyError, PostgresReplayV3SchemaError):
            raise
        except psycopg.Error as error:
            self._raise_database_error(
                error,
                "failed to load replay manifest for session",
            )

    @staticmethod
    def _raise_database_error(
        error: BaseException,
        message: str,
    ) -> NoReturn:
        if getattr(error, "sqlstate", None) in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresReplayV3SchemaError(
                "TBM_POSTGRES_REPLAY_SCHEMA",
                _MISSING_SCHEMA_MESSAGE,
            ) from error
        raise PostgresReplayV3PersistenceError(
            "TBM_POSTGRES_REPLAY_PERSISTENCE",
            message,
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresReplayV3Repository:
        self._require_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _validate_complete_bundle_inputs(
    supporting_artifacts: object,
    injection: object,
    manifest: object,
) -> None:
    if (
        type(supporting_artifacts) is not tuple
        or any(type(item) is not StoredReplayArtifact for item in supporting_artifacts)
        or type(injection) is not InjectionArtifact
        or type(manifest) is not DecisionReplayManifest
    ):
        raise ValueError(
            "supporting artifacts, injection, and manifest must be exact replay records"
        )
    stored = cast(tuple[StoredReplayArtifact, ...], supporting_artifacts)
    artifact_ids = tuple(item.artifact.artifact_id for item in stored)
    usage_decision_id = manifest.usage_decision_id
    expected_usage_artifact_id = "artifact_sha256_" + usage_decision_id.removeprefix(
        "usage_decision_sha256_"
    )
    expected_artifact_ids = [expected_usage_artifact_id]
    for name, digest in manifest.components:
        if name == "injection_artifact":
            continue
        if digest is None:
            raise ValueError("complete replay bundle cannot omit component artifacts")
        artifact_id = "artifact_sha256_" + digest.removeprefix("sha256:")
        if artifact_id not in expected_artifact_ids:
            expected_artifact_ids.append(artifact_id)
    if (
        not stored
        or _USAGE_DECISION_ID_RE.fullmatch(usage_decision_id) is None
        or artifact_ids != tuple(expected_artifact_ids)
        or len(set(artifact_ids)) != len(artifact_ids)
        or injection.artifact.artifact_id in artifact_ids
        or manifest.injection_artifact_id != injection.artifact.artifact_id
        or manifest.session_id != injection.session_id
        or manifest.decision_id != injection.decision_id
        or manifest.usage_decision_id != injection.usage_decision_id
    ):
        raise ValueError(
            "complete replay bundle usage and injection linkage or component "
            "set is invalid"
        )


def _validate_artifact_id(value: object) -> None:
    if type(value) is not str or _ARTIFACT_ID_RE.fullmatch(value) is None:
        raise ValueError("artifact_id must use artifact_sha256_<64 lowercase hex>")


def _validate_identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value) > 128
    ):
        raise ValueError(f"{field_name} must be a nonblank bounded identifier")


def _validate_digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical sha256 digest")


__all__ = [
    "POSTGRES_REPLAY_V3_SCHEMA_VERSION",
    "PostgresReplayV3ConflictError",
    "PostgresReplayV3Error",
    "PostgresReplayV3PersistenceError",
    "PostgresReplayV3Repository",
    "PostgresReplayV3SchemaError",
    "PostgresReplayV3StoreResult",
]
