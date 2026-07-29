from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .artifact_v3 import (
    ARTIFACT_AUTHORITY_CONTRACT_VERSION,
    ArtifactRetention,
    EncryptedArtifactRecord,
)
from .postgres import _load_psycopg
from .postgres_authorization_v3 import _CATALOG_SHA256_QUERY
from .replay_v3 import ContentAddressedArtifact, DataClassification


POSTGRES_ARTIFACT_V3_SCHEMA_VERSION = 1
_SCHEMA = "trace_backed_memory_v3_artifacts"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL Artifact Authority schema is missing or incomplete"
)
_EXPECTED_CATALOG_SHA256 = (
    "26f1ce0e8e2d9e0b61149a49bfd9f2c1d0d4516034e775b8593b85ac24047b6b"
)
_POSTGRES_ARTIFACT_CATALOG_SHA256_QUERY = (
    _CATALOG_SHA256_QUERY.replace(
        "trace_backed_memory_v3_authorization.",
        f"{_SCHEMA}.",
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
_ARTIFACT_ID_RE = re.compile(r"artifact_sha256_[0-9a-f]{64}")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresArtifactV3Error(RuntimeError):
    pass


class PostgresArtifactV3SchemaError(PostgresArtifactV3Error):
    pass


class PostgresArtifactV3ConflictError(PostgresArtifactV3Error):
    pass


class PostgresArtifactV3NotFoundError(PostgresArtifactV3Error):
    pass


class PostgresArtifactV3PersistenceError(PostgresArtifactV3Error):
    pass


@dataclass(frozen=True)
class PostgresArtifactV3StoreResult:
    artifact_id: str
    artifact_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


class PostgresArtifactV3Repository:
    """Immutable isolated PostgreSQL storage for encrypted artifacts."""

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
    ) -> PostgresArtifactV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresArtifactV3PersistenceError(
                "failed to connect to PostgreSQL Artifact Authority"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresArtifactV3Error(
                "PostgreSQL Artifact Authority repository is closed"
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    def _verify_schema_catalog(self, cursor: object) -> None:
        cursor.execute(
            "SELECT "
            "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy AS policy "
            "JOIN pg_catalog.pg_class AS class ON class.oid = policy.polrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s) AS policy_count, "
            "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite AS rule "
            "JOIN pg_catalog.pg_class AS class ON class.oid = rule.ev_class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND rule.rulename <> '_RETURN') AS rule_count, "
            "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_class AS class "
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
            raise PostgresArtifactV3SchemaError(
                "PostgreSQL Artifact Authority contains unsupported objects"
            )
        cursor.execute(
            _POSTGRES_ARTIFACT_CATALOG_SHA256_QUERY,
            (_SCHEMA,) * 7,
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0].get("catalog_sha256") != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresArtifactV3SchemaError(
                "PostgreSQL Artifact Authority catalog does not match"
            )

    def _lock_schema(self, cursor: object, *, for_write: bool) -> str:
        cursor.execute(
            "SELECT pg_catalog.current_setting('search_path') AS search_path"
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or type(rows[0].get("search_path")) is not str:
            raise PostgresArtifactV3SchemaError(
                "PostgreSQL search_path has invalid shape"
            )
        original_search_path = cast(str, rows[0]["search_path"])
        cursor.execute(
            "SELECT pg_catalog.set_config("
            "'search_path', 'pg_catalog', true)"
        )
        cursor.execute(
            "SELECT active.schema_version AS active_version, "
            "artifact.schema_version AS artifact_version, "
            "artifact.contract_version AS artifact_contract "
            "FROM public.trace_backed_memory_schema AS active "
            f"CROSS JOIN {_SCHEMA}.schema_metadata AS artifact "
            "WHERE active.singleton AND artifact.singleton "
            "FOR SHARE OF active, artifact"
        )
        if cursor.fetchall() != [{
            "active_version": 2,
            "artifact_version": POSTGRES_ARTIFACT_V3_SCHEMA_VERSION,
            "artifact_contract": ARTIFACT_AUTHORITY_CONTRACT_VERSION,
        }]:
            raise PostgresArtifactV3SchemaError(
                "PostgreSQL Artifact Authority metadata mismatch"
            )
        cursor.execute(
            f"LOCK TABLE {_SCHEMA}.schema_metadata, "
            f"{_SCHEMA}.encrypted_artifacts "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_schema_catalog(cursor)
        return original_search_path

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
    def _values(record: EncryptedArtifactRecord) -> tuple[object, ...]:
        artifact = record.artifact
        return (
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.size_bytes,
            artifact.media_type,
            artifact.classification,
            artifact.created_at,
            artifact.redaction_policy_id,
            record.tenant_id,
            record.repository_id,
            record.environment_id,
            record.write_authorization_event_id,
            record.encryption_provider_id,
            record.encryption_algorithm,
            artifact.encryption_key_id,
            record.encryption_key_id,
            record.nonce,
            record.ciphertext,
            record.ciphertext_sha256,
            record.retention.retain_until,
            record.retention.legal_hold,
            record.stored_at,
        )

    @classmethod
    def _record(
        cls,
        row: Mapping[str, object],
    ) -> EncryptedArtifactRecord:
        nonce = row.get("nonce")
        ciphertext = row.get("ciphertext")
        if isinstance(nonce, memoryview):
            nonce = nonce.tobytes()
        if isinstance(ciphertext, memoryview):
            ciphertext = ciphertext.tobytes()
        if type(nonce) is not bytes or type(ciphertext) is not bytes:
            raise PostgresArtifactV3PersistenceError(
                "PostgreSQL Artifact Authority row has invalid byte columns"
            )
        try:
            artifact = ContentAddressedArtifact(
                artifact_id=cast(str, row.get("artifact_id")),
                content_sha256=cast(str, row.get("content_sha256")),
                size_bytes=cast(int, row.get("size_bytes")),
                media_type=cast(str, row.get("media_type")),
                classification=cast(
                    DataClassification,
                    row.get("classification"),
                ),
                created_at=cast(str, row.get("created_at")),
                encryption_key_id=cast(
                    str,
                    row.get("artifact_encryption_key_id"),
                ),
                redaction_policy_id=cast(
                    str | None,
                    row.get("redaction_policy_id"),
                ),
            )
            record = EncryptedArtifactRecord(
                artifact=artifact,
                tenant_id=cast(str, row.get("tenant_id")),
                repository_id=cast(str, row.get("repository_id")),
                environment_id=cast(str, row.get("environment_id")),
                write_authorization_event_id=cast(
                    str,
                    row.get("write_authorization_event_id"),
                ),
                encryption_provider_id=cast(
                    str,
                    row.get("encryption_provider_id"),
                ),
                encryption_algorithm=cast(
                    str,
                    row.get("encryption_algorithm"),
                ),
                encryption_key_id=cast(
                    str,
                    row.get("encryption_key_id"),
                ),
                nonce=nonce,
                ciphertext=ciphertext,
                ciphertext_sha256=cast(
                    str,
                    row.get("ciphertext_sha256"),
                ),
                retention=ArtifactRetention(
                    retain_until=cast(
                        str | None,
                        row.get("retain_until"),
                    ),
                    legal_hold=cast(bool, row.get("legal_hold")),
                ),
                stored_at=cast(str, row.get("stored_at")),
            )
        except (TypeError, ValueError) as error:
            raise PostgresArtifactV3PersistenceError(
                "PostgreSQL Artifact Authority record failed validation"
            ) from error
        if cls._values(record) != (
            row.get("artifact_id"),
            row.get("content_sha256"),
            row.get("size_bytes"),
            row.get("media_type"),
            row.get("classification"),
            row.get("created_at"),
            row.get("redaction_policy_id"),
            row.get("tenant_id"),
            row.get("repository_id"),
            row.get("environment_id"),
            row.get("write_authorization_event_id"),
            row.get("encryption_provider_id"),
            row.get("encryption_algorithm"),
            row.get("artifact_encryption_key_id"),
            row.get("encryption_key_id"),
            nonce,
            ciphertext,
            row.get("ciphertext_sha256"),
            row.get("retain_until"),
            row.get("legal_hold"),
            row.get("stored_at"),
        ):
            raise PostgresArtifactV3PersistenceError(
                "PostgreSQL Artifact Authority columns do not match"
            )
        return record

    @staticmethod
    def _select_sql(*, for_share: bool) -> str:
        return (
            "SELECT artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, redaction_policy_id, tenant_id, "
            "repository_id, environment_id, write_authorization_event_id, "
            "encryption_provider_id, encryption_algorithm, "
            "artifact_encryption_key_id, encryption_key_id, nonce, "
            "ciphertext, ciphertext_sha256, retain_until, legal_hold, "
            f"stored_at FROM {_SCHEMA}.encrypted_artifacts "
            "WHERE artifact_id = %s"
            + (" FOR SHARE" if for_share else "")
        )

    def _select(
        self,
        cursor: object,
        artifact_id: str,
        *,
        for_share: bool,
    ) -> EncryptedArtifactRecord | None:
        cursor.execute(
            self._select_sql(for_share=for_share),
            (artifact_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise PostgresArtifactV3PersistenceError(
                "PostgreSQL Artifact Authority identity is not unique"
            )
        return self._record(rows[0])

    @_synchronized
    def put(
        self,
        record: EncryptedArtifactRecord,
    ) -> PostgresArtifactV3StoreResult:
        self._require_open()
        if type(record) is not EncryptedArtifactRecord:
            raise ValueError(
                "record must be exactly EncryptedArtifactRecord"
            )
        values = self._values(record)
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=True) as cursor:
                    cursor.execute(
                        f"INSERT INTO {_SCHEMA}.encrypted_artifacts ("
                        "artifact_id, content_sha256, size_bytes, media_type, "
                        "classification, created_at, redaction_policy_id, "
                        "tenant_id, repository_id, environment_id, "
                        "write_authorization_event_id, "
                        "encryption_provider_id, encryption_algorithm, "
                        "artifact_encryption_key_id, encryption_key_id, "
                        "nonce, ciphertext, ciphertext_sha256, retain_until, "
                        "legal_hold, stored_at"
                        ") VALUES ("
                        + ", ".join("%s" for _ in values)
                        + ") ON CONFLICT DO NOTHING RETURNING artifact_id",
                        values,
                    )
                    inserted = cursor.fetchone() is not None
                    cursor.execute(
                        self._select_sql(for_share=True).replace(
                            "WHERE artifact_id = %s",
                            "WHERE artifact_id = %s OR content_sha256 = %s",
                        ),
                        (
                            record.artifact.artifact_id,
                            record.artifact.content_sha256,
                        ),
                    )
                    rows = cursor.fetchall()
                    if len(rows) != 1:
                        raise PostgresArtifactV3ConflictError(
                            "artifact identity has conflicting immutable content"
                        )
                    if self._record(rows[0]) != record:
                        raise PostgresArtifactV3ConflictError(
                            "artifact identity has conflicting immutable content"
                        )
            return PostgresArtifactV3StoreResult(
                artifact_id=record.artifact.artifact_id,
                artifact_inserted=inserted,
            )
        except (PostgresArtifactV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load(self, artifact_id: str) -> EncryptedArtifactRecord:
        record = self.find(artifact_id)
        if record is None:
            raise PostgresArtifactV3NotFoundError(
                "encrypted artifact was not found"
            )
        return record

    @_synchronized
    def find(self, artifact_id: str) -> EncryptedArtifactRecord | None:
        self._require_open()
        if (
            type(artifact_id) is not str
            or _ARTIFACT_ID_RE.fullmatch(artifact_id) is None
        ):
            raise ValueError("artifact_id must be a v3 artifact ID")
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    return self._select(
                        cursor,
                        artifact_id,
                        for_share=False,
                    )
        except (PostgresArtifactV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_database(error)

    def _raise_database(self, error: Exception) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresArtifactV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise PostgresArtifactV3ConflictError(
                "artifact conflicts with immutable PostgreSQL storage"
            ) from error
        raise PostgresArtifactV3PersistenceError(
            "PostgreSQL Artifact Authority operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresArtifactV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "POSTGRES_ARTIFACT_V3_SCHEMA_VERSION",
    "PostgresArtifactV3ConflictError",
    "PostgresArtifactV3Error",
    "PostgresArtifactV3NotFoundError",
    "PostgresArtifactV3PersistenceError",
    "PostgresArtifactV3Repository",
    "PostgresArtifactV3SchemaError",
    "PostgresArtifactV3StoreResult",
]
