from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import re
from threading import RLock
from typing import NoReturn, ParamSpec, TypeVar, cast

from .event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
)
from .gate_evaluation_v3 import SemanticGateAttempt
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
from .postgres_semantic_gate_v3 import (
    PostgresSemanticGateV3Error,
    PostgresSemanticGateV3NotFoundError,
    PostgresSemanticGateV3Repository,
    PostgresSemanticGateV3StoreResult,
)
from .semantic_gate_artifact_v3 import (
    SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION,
    SemanticGateArtifactBinding,
    StoredSemanticGateArtifact,
    StoredSemanticGateAttemptArtifacts,
    dumps_semantic_gate_artifact_binding,
    loads_semantic_gate_artifact_binding,
    verify_semantic_gate_artifact_binding,
)
from .semantic_gate_attempt_event_v1 import (
    SemanticGateAttemptEventRef,
    SemanticGateAttemptEventV1Error,
    build_semantic_gate_attempt_event,
    parse_semantic_gate_attempt_event,
    semantic_gate_attempt_event_id,
    semantic_gate_attempt_event_ref,
    semantic_gate_attempt_stream_id,
    verify_semantic_gate_event_scope,
    verify_semantic_gate_system_parent,
)


POSTGRES_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION = 1
_SCHEMA = "trace_backed_memory_v3_semantic_gate_artifacts"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL Semantic Gate artifact v3 schema is missing or incomplete"
)
_EXPECTED_CATALOG_SHA256 = (
    "7ced114997e4de774570a52bcf89b7b5fd32f9f047e455507f337f7b098e726a"
)
_POSTGRES_SEMANTIC_GATE_ARTIFACT_CATALOG_SHA256_QUERY = (
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
_ATTEMPT_ID_RE = re.compile(r"semantic_attempt_sha256_[0-9a-f]{64}")
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresSemanticGateArtifactV3Error(RuntimeError):
    pass


class PostgresSemanticGateArtifactV3SchemaError(
    PostgresSemanticGateArtifactV3Error
):
    pass


class PostgresSemanticGateArtifactV3ConflictError(
    PostgresSemanticGateArtifactV3Error
):
    pass


class PostgresSemanticGateArtifactV3NotFoundError(
    PostgresSemanticGateArtifactV3Error
):
    pass


class PostgresSemanticGateArtifactV3PersistenceError(
    PostgresSemanticGateArtifactV3Error
):
    pass


@dataclass(frozen=True)
class PostgresSemanticGateArtifactV3StoreResult:
    attempt: PostgresSemanticGateV3StoreResult
    prompt_artifact_inserted: bool
    prompt_binding_inserted: bool
    response_artifact_inserted: bool
    response_binding_inserted: bool


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


class PostgresSemanticGateArtifactV3Repository:
    """Atomic SemanticGateAttempt and exact artifact-byte PostgreSQL store."""

    def __init__(self, connection: object, *, owns_connection: bool = False):
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._semantic_repository = PostgresSemanticGateV3Repository(connection)
        self._lock = RLock()
        self._closed = False
        self._event_first = False
        self._event_context: ContextVar[EventTrustedContext | None] = ContextVar(
            f"tbm_postgres_semantic_gate_event_context_{id(self)}",
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
            raise PostgresSemanticGateArtifactV3ConflictError(
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
    ) -> PostgresSemanticGateArtifactV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresSemanticGateArtifactV3PersistenceError(
                "failed to connect to PostgreSQL Semantic Gate artifact storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresSemanticGateArtifactV3Error(
                "PostgreSQL Semantic Gate artifact repository is closed"
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
            raise PostgresSemanticGateArtifactV3SchemaError(
                "PostgreSQL Semantic Gate artifact schema contains "
                "unsupported policies, rules, or relation kinds"
            )
        cursor.execute(
            _POSTGRES_SEMANTIC_GATE_ARTIFACT_CATALOG_SHA256_QUERY,
            (_SCHEMA,) * 7,
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0].get("catalog_sha256") != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresSemanticGateArtifactV3SchemaError(
                "PostgreSQL Semantic Gate artifact catalog does not match"
            )

    def _lock_schema(self, cursor: object, *, for_write: bool) -> str:
        cursor.execute(
            "SELECT pg_catalog.current_setting('search_path') AS search_path"
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or type(rows[0].get("search_path")) is not str:
            raise PostgresSemanticGateArtifactV3SchemaError(
                "PostgreSQL search_path has invalid shape"
            )
        original_search_path = cast(str, rows[0]["search_path"])
        cursor.execute(
            "SELECT pg_catalog.set_config("
            "'search_path', 'pg_catalog', true)"
        )
        cursor.execute(
            "SELECT active.schema_version AS active_version, "
            "semantic.schema_version AS semantic_version, "
            "semantic.contract_version AS semantic_contract, "
            "artifact.schema_version AS artifact_version, "
            "artifact.contract_version AS artifact_contract "
            "FROM public.trace_backed_memory_schema AS active "
            "CROSS JOIN trace_backed_memory_v3_semantic_gate.schema_metadata "
            "AS semantic "
            f"CROSS JOIN {_SCHEMA}.schema_metadata AS artifact "
            "WHERE active.singleton AND semantic.singleton = 1 "
            "AND artifact.singleton "
            "FOR SHARE OF active, semantic, artifact"
        )
        if cursor.fetchall() != [{
            "active_version": 2,
            "semantic_version": 1,
            "semantic_contract": "tbm.semantic-gate-attempt.v3",
            "artifact_version": POSTGRES_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION,
            "artifact_contract": SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION,
        }]:
            raise PostgresSemanticGateArtifactV3SchemaError(
                "PostgreSQL Semantic Gate artifact metadata mismatch"
            )
        cursor.execute(
            f"LOCK TABLE {_SCHEMA}.schema_metadata, "
            f"{_SCHEMA}.semantic_gate_artifacts, "
            f"{_SCHEMA}.semantic_gate_artifact_bindings "
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
    def _validate_stored(
        attempt: SemanticGateAttempt,
        stored: StoredSemanticGateArtifact,
        *,
        expected_role: str,
    ) -> None:
        if type(stored) is not StoredSemanticGateArtifact:
            raise ValueError(
                f"{expected_role} must be exactly StoredSemanticGateArtifact"
            )
        if stored.binding.artifact_role != expected_role:
            raise ValueError(f"{expected_role} artifact has the wrong role")
        if stored.binding.artifact.classification not in {
            "public",
            "internal",
        }:
            raise ValueError(
                "PostgreSQL Semantic Gate artifact storage does not provide "
                "encryption at rest"
            )
        if stored.binding.artifact.encryption_key_id is not None:
            raise ValueError(
                "PostgreSQL Semantic Gate artifact storage cannot claim "
                "an encryption key"
            )
        if not verify_semantic_gate_artifact_binding(
            stored.binding,
            attempt,
            stored.content,
        ):
            raise ValueError(
                f"{expected_role} artifact does not match Semantic Gate attempt"
            )

    @classmethod
    def _validate_bundle(
        cls,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> None:
        if type(attempt) is not SemanticGateAttempt:
            raise ValueError("attempt must be exactly SemanticGateAttempt")
        cls._validate_stored(attempt, prompt, expected_role="prompt")
        if attempt.status == "succeeded":
            if response is None:
                raise ValueError(
                    "succeeded Semantic Gate attempt requires response artifact"
                )
            cls._validate_stored(
                attempt,
                response,
                expected_role="response",
            )
        elif response is not None:
            raise ValueError(
                "failed Semantic Gate attempt forbids response artifact"
            )

    @staticmethod
    def _artifact_values(
        stored: StoredSemanticGateArtifact,
    ) -> tuple[object, ...]:
        artifact = stored.binding.artifact
        return (
            artifact.artifact_id,
            artifact.content_sha256,
            artifact.size_bytes,
            artifact.media_type,
            artifact.classification,
            artifact.created_at,
            artifact.encryption_key_id,
            artifact.redaction_policy_id,
            stored.content,
        )

    @staticmethod
    def _binding_values(
        binding: SemanticGateArtifactBinding,
    ) -> tuple[object, ...]:
        return (
            binding.attempt_id,
            binding.artifact_role,
            binding.artifact.artifact_id,
            binding.artifact.content_sha256,
            dumps_semantic_gate_artifact_binding(binding),
        )

    def _stored_artifact(
        self,
        row: Mapping[str, object],
        attempt: SemanticGateAttempt,
        *,
        expected_role: str,
    ) -> StoredSemanticGateArtifact:
        descriptor = row.get("descriptor")
        content_value = row.get("content")
        if type(descriptor) is not str:
            self._persistence("stored artifact descriptor has invalid shape")
        if isinstance(content_value, memoryview):
            content = content_value.tobytes()
        elif type(content_value) is bytes:
            content = content_value
        else:
            self._persistence("stored artifact content has invalid shape")
        try:
            binding = loads_semantic_gate_artifact_binding(descriptor)
            stored = StoredSemanticGateArtifact(binding, content)
        except ValueError as error:
            raise PostgresSemanticGateArtifactV3PersistenceError(
                "stored Semantic Gate artifact failed validation"
            ) from error
        expected = self._binding_values(binding) + self._artifact_values(stored)[2:]
        actual = (
            row.get("attempt_id"),
            row.get("artifact_role"),
            row.get("artifact_id"),
            row.get("content_sha256"),
            descriptor,
            row.get("size_bytes"),
            row.get("media_type"),
            row.get("classification"),
            row.get("created_at"),
            row.get("encryption_key_id"),
            row.get("redaction_policy_id"),
            content,
        )
        if actual != expected:
            self._persistence(
                "stored Semantic Gate artifact columns do not match descriptor"
            )
        if (
            binding.artifact_role != expected_role
            or not verify_semantic_gate_artifact_binding(
                binding,
                attempt,
                content,
            )
        ):
            self._persistence(
                "stored Semantic Gate artifact does not match attempt"
            )
        return stored

    def _select_stored(
        self,
        cursor: object,
        attempt: SemanticGateAttempt,
        role: str,
        *,
        for_update: bool = False,
    ) -> StoredSemanticGateArtifact | None:
        cursor.execute(
            "SELECT binding.attempt_id, binding.artifact_role, "
            "binding.artifact_id, binding.content_sha256, "
            "binding.descriptor, artifact.size_bytes, artifact.media_type, "
            "artifact.classification, artifact.created_at, "
            "artifact.encryption_key_id, artifact.redaction_policy_id, "
            "artifact.content "
            f"FROM {_SCHEMA}.semantic_gate_artifact_bindings AS binding "
            f"JOIN {_SCHEMA}.semantic_gate_artifacts AS artifact "
            "ON artifact.artifact_id = binding.artifact_id "
            "AND artifact.content_sha256 = binding.content_sha256 "
            "WHERE binding.attempt_id = %s AND binding.artifact_role = %s"
            + (" FOR UPDATE OF binding, artifact" if for_update else ""),
            (attempt.attempt_id, role),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            self._persistence("stored Semantic Gate artifact is not unique")
        return self._stored_artifact(
            rows[0],
            attempt,
            expected_role=role,
        )

    def _put_artifact(
        self,
        cursor: object,
        stored: StoredSemanticGateArtifact,
    ) -> bool:
        values = self._artifact_values(stored)
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.semantic_gate_artifacts ("
            "artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING artifact_id",
            values,
        )
        inserted = cursor.fetchone() is not None
        cursor.execute(
            "SELECT artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content "
            f"FROM {_SCHEMA}.semantic_gate_artifacts "
            "WHERE artifact_id = %s OR content_sha256 = %s FOR SHARE",
            (stored.binding.artifact.artifact_id,
             stored.binding.artifact.content_sha256),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "Semantic Gate artifact identity has conflicting content"
            )
        row = rows[0]
        content = row.get("content")
        if isinstance(content, memoryview):
            content = content.tobytes()
        actual = (
            row.get("artifact_id"),
            row.get("content_sha256"),
            row.get("size_bytes"),
            row.get("media_type"),
            row.get("classification"),
            row.get("created_at"),
            row.get("encryption_key_id"),
            row.get("redaction_policy_id"),
            content,
        )
        if actual != values:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "Semantic Gate artifact identity has conflicting content"
            )
        return inserted

    def _put_binding(
        self,
        cursor: object,
        binding: SemanticGateArtifactBinding,
    ) -> bool:
        values = self._binding_values(binding)
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.semantic_gate_artifact_bindings ("
            "attempt_id, artifact_role, artifact_id, content_sha256, descriptor"
            ") VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING attempt_id",
            values,
        )
        inserted = cursor.fetchone() is not None
        cursor.execute(
            "SELECT attempt_id, artifact_role, artifact_id, "
            "content_sha256, descriptor "
            f"FROM {_SCHEMA}.semantic_gate_artifact_bindings "
            "WHERE attempt_id = %s AND artifact_role = %s FOR SHARE",
            (binding.attempt_id, binding.artifact_role),
        )
        rows = cursor.fetchall()
        actual = None
        if len(rows) == 1:
            row = rows[0]
            actual = (
                row.get("attempt_id"),
                row.get("artifact_role"),
                row.get("artifact_id"),
                row.get("content_sha256"),
                row.get("descriptor"),
            )
        if actual != values:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "Semantic Gate artifact binding has conflicting content"
            )
        return inserted

    def _event_access(
        self,
    ) -> tuple[EventTrustedContext, LedgerAccessContext]:
        trusted_context = self._event_context.get()
        if trusted_context is None:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "event-first Semantic Gate mutation requires trusted event context"
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
    def _select_event_by_id(
        cursor: object,
        ledger: object,
        event_id: str,
    ) -> CanonicalEvent | None:
        cursor.execute(
            "SELECT event_id, event_sha256, partition_sha256, "
            "organization_id, tenant_id, repository_id, environment_id, "
            "stream_id, stream_version, global_position, "
            "previous_stream_event_sha256, classification, "
            "artifact_ref_count, canonical_event FROM "
            "trace_backed_memory_v3_event_ledger.events "
            "WHERE event_id = %s",
            (event_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise PostgresSemanticGateArtifactV3PersistenceError(
                "Semantic Gate attempt event lookup is ambiguous"
            )
        return ledger._stored_event(cursor, rows[0])

    @staticmethod
    def _verify_retained_event(
        event: CanonicalEvent,
        expected_ref: SemanticGateAttemptEventRef,
        trusted_context: EventTrustedContext,
    ) -> None:
        try:
            verify_semantic_gate_event_scope(event, trusted_context)
            retained_ref = parse_semantic_gate_attempt_event(event)
        except Exception as error:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "retained Semantic Gate attempt event failed validation"
            ) from error
        if retained_ref != expected_ref:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "Semantic Gate attempt event has conflicting immutable content"
            )

    def _prepare_event_first_write(self, cursor: object) -> None:
        if not self._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        trusted_context = self._event_context.get()
        if trusted_context is None:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "event-first Semantic Gate mutation requires trusted event context"
            )
        access = LedgerAccessContext(
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
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            ledger._lock_schema(cursor, write=True)
            ledger._select_global_position(cursor, for_update=True)
        finally:
            ledger.close()

    def _append_attempt_event(
        self,
        cursor: object,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> None:
        if not self._event_first:
            return
        from .postgres_event_ledger_v1 import PostgresEventLedgerV1

        trusted_context, access = self._event_access()
        ledger = PostgresEventLedgerV1(self._connection, access)
        try:
            ledger._lock_schema(cursor, write=True)
            expected_ref = semantic_gate_attempt_event_ref(
                attempt,
                prompt,
                response,
            )
            system_gate_event = None
            if attempt.sequence == 1:
                system_gate_event = self._select_event_by_id(
                    cursor,
                    ledger,
                    expected_ref.causation_event_id,
                )
                verify_semantic_gate_system_parent(
                    attempt,
                    system_gate_event,
                    trusted_context,
                )
            retained = self._select_event_by_id(
                cursor,
                ledger,
                semantic_gate_attempt_event_id(attempt.attempt_id),
            )
            if retained is not None:
                self._verify_retained_event(
                    retained,
                    expected_ref,
                    trusted_context,
                )
                return
            stream_id = semantic_gate_attempt_stream_id(
                attempt.system_gate_evaluation_id
            )
            previous_event = ledger._select_head_event(
                cursor,
                stream_id,
                for_update=False,
            )
            if attempt.sequence == 1:
                if previous_event is not None:
                    raise PostgresSemanticGateArtifactV3ConflictError(
                        "Semantic Gate attempt stream already has a head"
                    )
            elif (
                previous_event is None
                or previous_event.event_id
                != semantic_gate_attempt_event_id(
                    cast(str, attempt.previous_attempt_id)
                )
                or previous_event.stream_version != attempt.sequence - 1
            ):
                raise PostgresSemanticGateArtifactV3ConflictError(
                    "Semantic Gate retry does not extend the event stream"
                )
            event = build_semantic_gate_attempt_event(
                attempt,
                prompt,
                response,
                system_gate_event=system_gate_event,
                previous_event=previous_event,
                global_position=(
                    ledger._select_global_position(cursor, for_update=True) + 1
                ),
                trusted_context=trusted_context,
            )
            ledger._append_in_transaction(
                cursor,
                LedgerAppendRequest(
                    access=access,
                    stream_id=stream_id,
                    expected_stream_version=attempt.sequence - 1,
                    events=(event,),
                    idempotency=LedgerIdempotency(
                        event.idempotency_key_sha256,
                        event.request_sha256,
                    ),
                ),
            )
        finally:
            ledger.close()

    @_synchronized
    def store_attempt_with_artifacts(
        self,
        attempt: SemanticGateAttempt,
        prompt: StoredSemanticGateArtifact,
        response: StoredSemanticGateArtifact | None,
    ) -> PostgresSemanticGateArtifactV3StoreResult:
        self._require_open()
        self._validate_bundle(attempt, prompt, response)
        try:
            with self._connection.transaction():
                with self._cursor() as event_cursor:
                    self._prepare_event_first_write(event_cursor)
                    self._append_attempt_event(
                        event_cursor,
                        attempt,
                        prompt,
                        response,
                    )
                attempt_result = self._semantic_repository.store_attempt(attempt)
                with self._secured_cursor(for_write=True) as cursor:
                    prompt_artifact_inserted = self._put_artifact(
                        cursor,
                        prompt,
                    )
                    prompt_binding_inserted = self._put_binding(
                        cursor,
                        prompt.binding,
                    )
                    response_artifact_inserted = False
                    response_binding_inserted = False
                    if response is not None:
                        response_artifact_inserted = self._put_artifact(
                            cursor,
                            response,
                        )
                        response_binding_inserted = self._put_binding(
                            cursor,
                            response.binding,
                        )
                    loaded_prompt = self._select_stored(
                        cursor,
                        attempt,
                        "prompt",
                    )
                    loaded_response = self._select_stored(
                        cursor,
                        attempt,
                        "response",
                    )
                    if loaded_prompt != prompt or loaded_response != response:
                        self._persistence(
                            "Semantic Gate artifact read-back does not match"
                        )
            return PostgresSemanticGateArtifactV3StoreResult(
                attempt_result,
                prompt_artifact_inserted,
                prompt_binding_inserted,
                response_artifact_inserted,
                response_binding_inserted,
            )
        except (
            SemanticGateAttemptEventV1Error,
            EventLedgerPortError,
        ) as error:
            raise PostgresSemanticGateArtifactV3ConflictError(
                "Semantic Gate attempt event conflicts with immutable storage"
            ) from error
        except (
            PostgresSemanticGateArtifactV3Error,
            PostgresSemanticGateV3Error,
            ValueError,
        ):
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_attempt_with_artifacts(
        self,
        attempt_id: str,
    ) -> StoredSemanticGateAttemptArtifacts:
        self._require_open()
        if (
            type(attempt_id) is not str
            or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None
        ):
            raise ValueError(
                "attempt_id must be a v3 Semantic Gate attempt ID"
            )
        try:
            with self._connection.transaction():
                attempt = self._semantic_repository.load_attempt(attempt_id)
                with self._secured_cursor(for_write=False) as cursor:
                    prompt = self._select_stored(cursor, attempt, "prompt")
                    response = self._select_stored(cursor, attempt, "response")
                    if prompt is None:
                        raise PostgresSemanticGateArtifactV3NotFoundError(
                            "Semantic Gate prompt artifact was not found"
                        )
                    if attempt.status == "succeeded" and response is None:
                        self._persistence(
                            "succeeded attempt is missing response artifact"
                        )
                    if attempt.status == "failed" and response is not None:
                        self._persistence(
                            "failed attempt has a response artifact"
                        )
                    return StoredSemanticGateAttemptArtifacts(
                        attempt,
                        prompt,
                        response,
                    )
        except (
            PostgresSemanticGateArtifactV3Error,
            PostgresSemanticGateV3Error,
            ValueError,
        ):
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_attempt_chain(
        self,
        evaluation_id: str,
    ) -> tuple[SemanticGateAttempt, ...]:
        """Load the exact chain, or an empty tuple before its first attempt."""

        self._require_open()
        try:
            return self._semantic_repository.load_chain(evaluation_id)
        except PostgresSemanticGateV3NotFoundError:
            return ()

    def _persistence(self, message: str) -> NoReturn:
        raise PostgresSemanticGateArtifactV3PersistenceError(message)

    def _raise_database(self, error: Exception) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresSemanticGateArtifactV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise PostgresSemanticGateArtifactV3ConflictError(
                "Semantic Gate artifact conflicts with immutable "
                "PostgreSQL storage"
            ) from error
        raise PostgresSemanticGateArtifactV3PersistenceError(
            "PostgreSQL Semantic Gate artifact operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresSemanticGateArtifactV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "POSTGRES_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION",
    "PostgresSemanticGateArtifactV3ConflictError",
    "PostgresSemanticGateArtifactV3Error",
    "PostgresSemanticGateArtifactV3NotFoundError",
    "PostgresSemanticGateArtifactV3PersistenceError",
    "PostgresSemanticGateArtifactV3Repository",
    "PostgresSemanticGateArtifactV3SchemaError",
    "PostgresSemanticGateArtifactV3StoreResult",
]
