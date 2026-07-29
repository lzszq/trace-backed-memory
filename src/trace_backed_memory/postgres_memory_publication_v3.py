from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import json
from threading import RLock
from typing import Literal, ParamSpec, TypeVar, cast

from ._ingestion import parse_bounded_json
from .authorization_v3 import (
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    dumps_authorization_decision,
    dumps_authorization_policy,
    loads_authorization_decision,
    loads_authorization_policy,
)
from .evidence_v3 import (
    StructuredRegressionEvidence,
    dumps_structured_regression_evidence,
)
from .fix_evidence_v3 import FixEvidence, dumps_fix_evidence
from .memory_publication_v3 import (
    MemoryRevisionActivation,
    MemoryRevisionApproval,
    activate_memory_revision,
    approve_memory_revision,
    dumps_memory_revision_activation,
    dumps_memory_revision_approval,
    loads_memory_revision_activation,
    loads_memory_revision_approval,
)
from .memory_revision_v3 import MemoryRevision, dumps_memory_revision
from .postgres import _load_psycopg
from .postgres_memory_revision_v3 import (
    _EXPECTED_CATALOG_SHA256 as _REVISION_EXPECTED_CATALOG_SHA256,
    _MEMORY_REVISION_CATALOG_SHA256_QUERY,
)


POSTGRES_MEMORY_PUBLICATION_V3_SCHEMA_VERSION = 1
POSTGRES_MEMORY_PUBLICATION_V3_CONTRACT_VERSION = (
    "tbm.memory-publication.v3"
)
_SCHEMA = "trace_backed_memory_v3_memory_publication"
_EXPECTED_CATALOG_SHA256 = (
    "3d6013e536ca33bd48198fef1f37257dde1863fd6597dd409d3bb37c310c5c83"
)
_PUBLICATION_CATALOG_SHA256_QUERY = _MEMORY_REVISION_CATALOG_SHA256_QUERY
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_CONFLICT_SQLSTATES = frozenset({"23503", "23505", "23514", "P0001"})
_P = ParamSpec("_P")
_R = TypeVar("_R")
AttestationKind = Literal["approval", "activation"]
AttestationVerifier = Callable[[AttestationKind, str, str, str], bool]


class PostgresMemoryPublicationV3Error(RuntimeError):
    pass


class PostgresMemoryPublicationV3SchemaError(
    PostgresMemoryPublicationV3Error
):
    pass


class PostgresMemoryPublicationV3ConflictError(
    PostgresMemoryPublicationV3Error
):
    pass


class PostgresMemoryPublicationV3NotFoundError(
    PostgresMemoryPublicationV3Error
):
    pass


class PostgresMemoryPublicationV3PersistenceError(
    PostgresMemoryPublicationV3Error
):
    pass


class PostgresMemoryPublicationV3AttestationError(
    PostgresMemoryPublicationV3Error
):
    pass


@dataclass(frozen=True)
class PostgresMemoryPublicationV3ApprovalResult:
    approval: MemoryRevisionApproval
    inserted: bool
    attestation_verified_by: str


@dataclass(frozen=True)
class PostgresMemoryPublicationV3ActivationResult:
    activation: MemoryRevisionActivation
    inserted: bool
    attestation_verified_by: str


@dataclass(frozen=True)
class PostgresMemoryPublicationV3Head:
    tenant_id: str
    repository_id: str | None
    memory_id: str
    current_revision_number: int
    current_revision_id: str
    current_activation_id: str


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _request_descriptor(request: AuthorizationRequest) -> str:
    if type(request) is not AuthorizationRequest:
        raise ValueError("request must be exactly AuthorizationRequest")
    return _canonical_json(request.to_dict())


def _loads_request(document: str) -> AuthorizationRequest:
    try:
        value = parse_bounded_json(
            document,
            description="stored authorization request",
            max_nodes=64,
            max_depth=4,
        )
        if type(value) is not dict or set(value) != {
            "request_id",
            "principal_id",
            "agent_client_id",
            "tenant_id",
            "repository_reference",
            "permission",
            "requested_at",
        }:
            raise ValueError
        return AuthorizationRequest(**value)
    except (TypeError, ValueError) as error:
        raise PostgresMemoryPublicationV3PersistenceError(
            "stored authorization request is invalid"
        ) from error


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a non-empty bounded identifier")
    return value


def _approval_provenance_matches(
    stored: tuple[
        MemoryRevisionApproval,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ],
    approval: MemoryRevisionApproval,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    verifier_id: str,
) -> bool:
    return (
        stored[0] == approval
        and dumps_authorization_policy(stored[1])
        == dumps_authorization_policy(policy)
        and _request_descriptor(stored[2]) == _request_descriptor(request)
        and dumps_authorization_decision(stored[3])
        == dumps_authorization_decision(decision)
        and stored[4] == verifier_id
    )


def _activation_provenance_matches(
    stored: tuple[
        MemoryRevisionActivation,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ],
    activation: MemoryRevisionActivation,
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    verifier_id: str,
) -> bool:
    return (
        stored[0] == activation
        and dumps_authorization_policy(stored[1])
        == dumps_authorization_policy(policy)
        and _request_descriptor(stored[2]) == _request_descriptor(request)
        and dumps_authorization_decision(stored[3])
        == dumps_authorization_decision(decision)
        and stored[4] == verifier_id
    )


class PostgresMemoryPublicationV3Repository:
    """Durable PostgreSQL approval/activation authority over v3 proposals."""

    def __init__(
        self,
        connection: object,
        *,
        attestation_verifier: AttestationVerifier,
        attestation_verifier_id: str,
        owns_connection: bool = False,
    ) -> None:
        if connection is None:
            raise ValueError("connection is required")
        if not callable(attestation_verifier):
            raise ValueError("attestation_verifier must be callable")
        self._connection = connection
        self._attestation_verifier = attestation_verifier
        self._attestation_verifier_id = _identifier(
            attestation_verifier_id,
            "attestation_verifier_id",
        )
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        *,
        attestation_verifier: AttestationVerifier,
        attestation_verifier_id: str,
        **kwargs: object,
    ) -> PostgresMemoryPublicationV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresMemoryPublicationV3PersistenceError(
                "failed to connect to PostgreSQL memory publication v3 storage"
            ) from error
        return cls(
            connection,
            attestation_verifier=attestation_verifier,
            attestation_verifier_id=attestation_verifier_id,
            owns_connection=True,
        )

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresMemoryPublicationV3Error(
                "PostgreSQL memory publication v3 repository is closed"
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    def _verify_catalog(self, cursor: object) -> None:
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
            "AND class.relkind NOT IN ('r', 'i', 'p')) "
            "AS unsupported_relation_count",
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [{
            "policy_count": 0,
            "rule_count": 0,
            "unsupported_relation_count": 0,
        }]:
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL memory publication has unsupported catalog objects"
            )
        cursor.execute(
            _PUBLICATION_CATALOG_SHA256_QUERY,
            (_SCHEMA,) * 7,
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0].get("catalog_sha256") != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL memory publication catalog does not match"
            )

    def _verify_revision_catalog(self, cursor: object) -> None:
        cursor.execute(
            _MEMORY_REVISION_CATALOG_SHA256_QUERY,
            ("trace_backed_memory_v3_memory_revision",) * 7,
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0].get("catalog_sha256")
            != _REVISION_EXPECTED_CATALOG_SHA256
        ):
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL memory revision dependency catalog mismatch"
            )

    def _lock_schema(self, cursor: object, *, for_write: bool) -> str:
        cursor.execute(
            "SELECT pg_catalog.current_setting('search_path') AS search_path"
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or type(rows[0].get("search_path")) is not str:
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL search_path has invalid shape"
            )
        original = cast(str, rows[0]["search_path"])
        cursor.execute(
            "SELECT pg_catalog.set_config("
            "'search_path', 'pg_catalog', true)"
        )
        cursor.execute(
            "SELECT schema_version AS active_version "
            "FROM public.trace_backed_memory_schema "
            "WHERE singleton FOR SHARE"
        )
        if cursor.fetchall() != [{"active_version": 2}]:
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL active schema metadata mismatch"
            )
        cursor.execute(
            "SELECT schema_version AS revision_version, contract_version "
            "FROM trace_backed_memory_v3_memory_revision.schema_metadata "
            "WHERE singleton = 1 FOR SHARE"
        )
        if cursor.fetchall() != [{
            "revision_version": 1,
            "contract_version": "tbm.memory-revision.v3",
        }]:
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL memory revision dependency mismatch"
            )
        cursor.execute(
            "SELECT schema_version AS publication_version, "
            "contract_version FROM "
            "trace_backed_memory_v3_memory_publication.schema_metadata "
            "WHERE singleton = 1 FOR SHARE"
        )
        if cursor.fetchall() != [{
            "publication_version": (
                POSTGRES_MEMORY_PUBLICATION_V3_SCHEMA_VERSION
            ),
            "contract_version": (
                POSTGRES_MEMORY_PUBLICATION_V3_CONTRACT_VERSION
            ),
        }]:
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL memory publication metadata mismatch"
            )
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_memory_revision.schema_metadata, "
            "trace_backed_memory_v3_memory_revision.v3_fix_evidence, "
            "trace_backed_memory_v3_memory_revision.v3_regression_evidence, "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_proposals, "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_regression_evidence, "
            "trace_backed_memory_v3_memory_publication.schema_metadata, "
            "trace_backed_memory_v3_memory_publication."
            "v3_memory_revision_approvals, "
            "trace_backed_memory_v3_memory_publication."
            "v3_memory_revision_activations, "
            "trace_backed_memory_v3_memory_publication."
            "v3_memory_revision_activation_heads "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_revision_catalog(cursor)
        self._verify_catalog(cursor)
        return original

    @contextmanager
    def _secured_cursor(self, *, for_write: bool) -> Iterator[object]:
        with self._cursor() as cursor:
            original = self._lock_schema(cursor, for_write=for_write)
            try:
                yield cursor
            except Exception:
                raise
            else:
                self._verify_revision_catalog(cursor)
                self._verify_catalog(cursor)
                cursor.execute(
                    "SELECT pg_catalog.set_config("
                    "'search_path', %s, true)",
                    (original,),
                )

    def _verify_attestation(
        self,
        kind: AttestationKind,
        actor_id: str,
        client_id: str,
        digest: str,
    ) -> None:
        try:
            verified = self._attestation_verifier(
                kind,
                actor_id,
                client_id,
                digest,
            )
        except BaseException as error:
            raise PostgresMemoryPublicationV3AttestationError(
                f"{kind} attestation verification failed"
            ) from error
        if verified is not True:
            raise PostgresMemoryPublicationV3AttestationError(
                f"{kind} attestation was not verified"
            )

    @staticmethod
    def _require_proposal_bundle(
        cursor: object,
        revision: MemoryRevision,
        previous_revision: MemoryRevision | None,
        fix_evidence_by_id: Mapping[str, FixEvidence],
        regression_evidence_by_id: Mapping[
            str,
            StructuredRegressionEvidence,
        ],
    ) -> None:
        cursor.execute(
            "SELECT descriptor FROM "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_proposals "
            "WHERE revision_id = %s FOR SHARE",
            (revision.revision_id,),
        )
        if cursor.fetchall() != [{
            "descriptor": dumps_memory_revision(revision)
        }]:
            raise PostgresMemoryPublicationV3NotFoundError(
                "exact memory revision proposal is not stored"
            )
        if previous_revision is not None:
            cursor.execute(
                "SELECT descriptor FROM "
                "trace_backed_memory_v3_memory_revision."
                "v3_memory_revision_proposals "
                "WHERE revision_id = %s FOR SHARE",
                (previous_revision.revision_id,),
            )
            if cursor.fetchall() != [{
                "descriptor": dumps_memory_revision(previous_revision)
            }]:
                raise PostgresMemoryPublicationV3NotFoundError(
                    "exact previous memory revision proposal is not stored"
                )
        if revision.fix_evidence_id is not None:
            fix = fix_evidence_by_id.get(revision.fix_evidence_id)
            if type(fix) is not FixEvidence:
                raise PostgresMemoryPublicationV3NotFoundError(
                    "exact fix evidence is not supplied"
                )
            cursor.execute(
                "SELECT descriptor FROM "
                "trace_backed_memory_v3_memory_revision.v3_fix_evidence "
                "WHERE evidence_id = %s FOR SHARE",
                (revision.fix_evidence_id,),
            )
            if cursor.fetchall() != [{
                "descriptor": dumps_fix_evidence(fix)
            }]:
                raise PostgresMemoryPublicationV3NotFoundError(
                    "exact fix evidence is not stored"
                )
        cursor.execute(
            "SELECT evidence_id FROM "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_regression_evidence "
            "WHERE revision_id = %s ORDER BY ordinal FOR SHARE",
            (revision.revision_id,),
        )
        stored_ids = tuple(row["evidence_id"] for row in cursor.fetchall())
        if stored_ids != revision.regression_evidence_ids:
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored regression evidence links do not match proposal"
            )
        for evidence_id in stored_ids:
            evidence = regression_evidence_by_id.get(evidence_id)
            if type(evidence) is not StructuredRegressionEvidence:
                raise PostgresMemoryPublicationV3NotFoundError(
                    "exact regression evidence is not supplied"
                )
            cursor.execute(
                "SELECT descriptor FROM "
                "trace_backed_memory_v3_memory_revision."
                "v3_regression_evidence "
                "WHERE evidence_id = %s FOR SHARE",
                (evidence_id,),
            )
            if cursor.fetchall() != [{
                "descriptor": dumps_structured_regression_evidence(evidence)
            }]:
                raise PostgresMemoryPublicationV3NotFoundError(
                    "exact regression evidence is not stored"
                )

    @staticmethod
    def _approval_values(
        approval: MemoryRevisionApproval,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        verifier_id: str,
    ) -> tuple[object, ...]:
        return (
            approval.approval_id,
            approval.revision_id,
            approval.tenant_id,
            approval.repository_id,
            approval.repository_id or "",
            approval.memory_id,
            approval.revision_number,
            dumps_memory_revision_approval(approval),
            dumps_authorization_policy(policy),
            _request_descriptor(request),
            dumps_authorization_decision(decision),
            verifier_id,
        )

    @staticmethod
    def _activation_values(
        activation: MemoryRevisionActivation,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        verifier_id: str,
    ) -> tuple[object, ...]:
        return (
            activation.activation_id,
            activation.approval_id,
            activation.revision_id,
            activation.tenant_id,
            activation.repository_id,
            activation.repository_id or "",
            activation.memory_id,
            activation.revision_number,
            activation.previous_activation_id,
            dumps_memory_revision_activation(activation),
            dumps_authorization_policy(policy),
            _request_descriptor(request),
            dumps_authorization_decision(decision),
            verifier_id,
        )

    @staticmethod
    def _put_exact(
        cursor: object,
        *,
        table: str,
        id_column: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        conflict_message: str,
    ) -> bool:
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE {id_column} = %s FOR SHARE",
            (values[0],),
        )
        existing_rows = cursor.fetchall()
        if existing_rows:
            if (
                len(existing_rows) != 1
                or tuple(
                    existing_rows[0].get(column) for column in columns
                )
                != values
            ):
                raise PostgresMemoryPublicationV3ConflictError(
                    conflict_message
                )
            return False
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('%s' for _ in values)}) "
            f"ON CONFLICT DO NOTHING RETURNING {id_column}",
            values,
        )
        inserted_rows = cursor.fetchall()
        if inserted_rows:
            if inserted_rows != [{id_column: values[0]}]:
                raise PostgresMemoryPublicationV3PersistenceError(
                    "inserted publication identity has invalid shape"
                )
            return True
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE {id_column} = %s FOR SHARE",
            (values[0],),
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or tuple(rows[0].get(column) for column in columns) != values
        ):
            raise PostgresMemoryPublicationV3ConflictError(
                conflict_message
            )
        return False

    @staticmethod
    def _load_approval_row(
        cursor: object,
        *,
        approval_id: str | None = None,
        revision_id: str | None = None,
    ) -> tuple[
        MemoryRevisionApproval,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ]:
        if (approval_id is None) == (revision_id is None):
            raise ValueError("select approval by exactly one identity")
        column = "approval_id" if approval_id is not None else "revision_id"
        identity = approval_id if approval_id is not None else revision_id
        cursor.execute(
            "SELECT descriptor, authorization_policy_descriptor, "
            "authorization_request_descriptor, "
            "authorization_decision_descriptor, attestation_verified_by "
            "FROM trace_backed_memory_v3_memory_publication."
            f"v3_memory_revision_approvals WHERE {column} = %s FOR SHARE",
            (identity,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresMemoryPublicationV3NotFoundError(
                "memory revision approval was not found"
            )
        if len(rows) != 1 or any(
            type(rows[0].get(name)) is not str
            for name in (
                "descriptor",
                "authorization_policy_descriptor",
                "authorization_request_descriptor",
                "authorization_decision_descriptor",
                "attestation_verified_by",
            )
        ):
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored memory revision approval has invalid shape"
            )
        row = rows[0]
        try:
            return (
                loads_memory_revision_approval(row["descriptor"]),
                loads_authorization_policy(
                    row["authorization_policy_descriptor"]
                ),
                _loads_request(row["authorization_request_descriptor"]),
                loads_authorization_decision(
                    row["authorization_decision_descriptor"]
                ),
                _identifier(
                    row["attestation_verified_by"],
                    "attestation_verified_by",
                ),
            )
        except PostgresMemoryPublicationV3Error:
            raise
        except ValueError as error:
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored memory revision approval is invalid"
            ) from error

    @staticmethod
    def _load_activation_row(
        cursor: object,
        *,
        activation_id: str | None = None,
        revision_id: str | None = None,
    ) -> tuple[
        MemoryRevisionActivation,
        AuthorizationPolicyBundle,
        AuthorizationRequest,
        AuthorizationDecision,
        str,
    ]:
        if (activation_id is None) == (revision_id is None):
            raise ValueError("select activation by exactly one identity")
        column = (
            "activation_id" if activation_id is not None else "revision_id"
        )
        identity = activation_id if activation_id is not None else revision_id
        cursor.execute(
            "SELECT descriptor, authorization_policy_descriptor, "
            "authorization_request_descriptor, "
            "authorization_decision_descriptor, attestation_verified_by "
            "FROM trace_backed_memory_v3_memory_publication."
            f"v3_memory_revision_activations WHERE {column} = %s FOR SHARE",
            (identity,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresMemoryPublicationV3NotFoundError(
                "memory revision activation was not found"
            )
        if len(rows) != 1 or any(
            type(rows[0].get(name)) is not str
            for name in (
                "descriptor",
                "authorization_policy_descriptor",
                "authorization_request_descriptor",
                "authorization_decision_descriptor",
                "attestation_verified_by",
            )
        ):
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored memory revision activation has invalid shape"
            )
        row = rows[0]
        try:
            return (
                loads_memory_revision_activation(row["descriptor"]),
                loads_authorization_policy(
                    row["authorization_policy_descriptor"]
                ),
                _loads_request(row["authorization_request_descriptor"]),
                loads_authorization_decision(
                    row["authorization_decision_descriptor"]
                ),
                _identifier(
                    row["attestation_verified_by"],
                    "attestation_verified_by",
                ),
            )
        except PostgresMemoryPublicationV3Error:
            raise
        except ValueError as error:
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored memory revision activation is invalid"
            ) from error

    @staticmethod
    def _select_head(
        cursor: object,
        *,
        tenant_id: str,
        repository_id: str | None,
        memory_id: str,
        for_update: bool,
    ) -> PostgresMemoryPublicationV3Head | None:
        cursor.execute(
            "SELECT tenant_id, repository_id, memory_id, "
            "current_revision_number, current_revision_id, "
            "current_activation_id "
            "FROM trace_backed_memory_v3_memory_publication."
            "v3_memory_revision_activation_heads "
            "WHERE tenant_id = %s AND repository_id_key = %s "
            "AND memory_id = %s"
            f"{' FOR UPDATE' if for_update else ' FOR SHARE'}",
            (tenant_id, repository_id or "", memory_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored activation head is not unique"
            )
        row = rows[0]
        if row.get("current_revision_number") == 0:
            if (
                row.get("current_revision_id") is not None
                or row.get("current_activation_id") is not None
            ):
                raise PostgresMemoryPublicationV3PersistenceError(
                    "empty activation head is inconsistent"
                )
            return None
        if (
            type(row.get("tenant_id")) is not str
            or (
                row.get("repository_id") is not None
                and type(row.get("repository_id")) is not str
            )
            or type(row.get("memory_id")) is not str
            or type(row.get("current_revision_number")) is not int
            or type(row.get("current_revision_id")) is not str
            or type(row.get("current_activation_id")) is not str
        ):
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored activation head has invalid shape"
            )
        head = PostgresMemoryPublicationV3Head(
            tenant_id=row["tenant_id"],
            repository_id=row["repository_id"],
            memory_id=row["memory_id"],
            current_revision_number=row["current_revision_number"],
            current_revision_id=row["current_revision_id"],
            current_activation_id=row["current_activation_id"],
        )
        activation, *_ = (
            PostgresMemoryPublicationV3Repository._load_activation_row(
                cursor,
                activation_id=head.current_activation_id,
            )
        )
        if (
            activation.revision_id != head.current_revision_id
            or activation.revision_number != head.current_revision_number
            or activation.tenant_id != head.tenant_id
            or activation.repository_id != head.repository_id
            or activation.memory_id != head.memory_id
        ):
            raise PostgresMemoryPublicationV3PersistenceError(
                "stored activation head does not match activation"
            )
        return head

    @staticmethod
    def _raise_storage_error(error: Exception, operation: str) -> None:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresMemoryPublicationV3SchemaError(
                "PostgreSQL memory publication schema is missing or incomplete"
            ) from error
        if sqlstate in _CONFLICT_SQLSTATES:
            raise PostgresMemoryPublicationV3ConflictError(
                f"{operation} conflicts with durable publication state"
            ) from error
        raise PostgresMemoryPublicationV3PersistenceError(
            f"failed to {operation}"
        ) from error

    @_synchronized
    def append_approval(
        self,
        *,
        revision: MemoryRevision,
        previous_revision: MemoryRevision | None,
        content: bytes,
        fix_evidence_by_id: Mapping[str, FixEvidence],
        regression_evidence_by_id: Mapping[
            str,
            StructuredRegressionEvidence,
        ],
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        approved_by: str,
        approved_via_client_id: str,
        approved_at: str,
        approval_attestation_sha256: str,
    ) -> PostgresMemoryPublicationV3ApprovalResult:
        self._require_open()
        verifier_id = self._attestation_verifier_id
        approval = approve_memory_revision(
            revision=revision,
            previous_revision=previous_revision,
            content=content,
            fix_evidence_by_id=fix_evidence_by_id,
            regression_evidence_by_id=regression_evidence_by_id,
            policy=policy,
            request=request,
            decision=decision,
            approved_by=approved_by,
            approved_via_client_id=approved_via_client_id,
            approved_at=approved_at,
            approval_attestation_sha256=approval_attestation_sha256,
        )
        self._verify_attestation(
            "approval",
            approval.approved_by,
            approval.approved_via_client_id,
            approval.approval_attestation_sha256,
        )
        columns = (
            "approval_id",
            "revision_id",
            "tenant_id",
            "repository_id",
            "repository_id_key",
            "memory_id",
            "revision_number",
            "descriptor",
            "authorization_policy_descriptor",
            "authorization_request_descriptor",
            "authorization_decision_descriptor",
            "attestation_verified_by",
        )
        values = self._approval_values(
            approval,
            policy,
            request,
            decision,
            verifier_id,
        )
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=True) as cursor:
                    self._require_proposal_bundle(
                        cursor,
                        revision,
                        previous_revision,
                        fix_evidence_by_id,
                        regression_evidence_by_id,
                    )
                    inserted = self._put_exact(
                        cursor,
                        table=(
                            "trace_backed_memory_v3_memory_publication."
                            "v3_memory_revision_approvals"
                        ),
                        id_column="approval_id",
                        columns=columns,
                        values=values,
                        conflict_message="approval identity conflict",
                    )
                    retained = self._load_approval_row(
                        cursor,
                        approval_id=approval.approval_id,
                    )
                    if not _approval_provenance_matches(
                        retained,
                        approval,
                        policy,
                        request,
                        decision,
                        verifier_id,
                    ):
                        raise PostgresMemoryPublicationV3PersistenceError(
                            "approval read-back mismatch"
                        )
        except (PostgresMemoryPublicationV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_storage_error(
                error,
                "append memory revision approval",
            )
        return PostgresMemoryPublicationV3ApprovalResult(
            approval=approval,
            inserted=inserted,
            attestation_verified_by=verifier_id,
        )

    @_synchronized
    def append_activation(
        self,
        *,
        revision: MemoryRevision,
        previous_revision: MemoryRevision | None,
        content: bytes,
        fix_evidence_by_id: Mapping[str, FixEvidence],
        regression_evidence_by_id: Mapping[
            str,
            StructuredRegressionEvidence,
        ],
        approval: MemoryRevisionApproval,
        approval_policy: AuthorizationPolicyBundle,
        approval_request: AuthorizationRequest,
        approval_decision: AuthorizationDecision,
        policy: AuthorizationPolicyBundle,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
        activated_by: str,
        activated_via_client_id: str,
        activated_at: str,
        activation_attestation_sha256: str,
    ) -> PostgresMemoryPublicationV3ActivationResult:
        self._require_open()
        verifier_id = self._attestation_verifier_id
        self._verify_attestation(
            "activation",
            activated_by,
            activated_via_client_id,
            activation_attestation_sha256,
        )
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=True) as cursor:
                    self._require_proposal_bundle(
                        cursor,
                        revision,
                        previous_revision,
                        fix_evidence_by_id,
                        regression_evidence_by_id,
                    )
                    stored_approval = self._load_approval_row(
                        cursor,
                        approval_id=approval.approval_id,
                    )
                    if not _approval_provenance_matches(
                        stored_approval,
                        approval,
                        approval_policy,
                        approval_request,
                        approval_decision,
                        stored_approval[4],
                    ):
                        raise PostgresMemoryPublicationV3ConflictError(
                            "activation approval provenance mismatch"
                        )
                    cursor.execute(
                        "SELECT activation_id FROM "
                        "trace_backed_memory_v3_memory_publication."
                        "v3_memory_revision_activations "
                        "WHERE revision_id = %s FOR SHARE",
                        (revision.revision_id,),
                    )
                    existing_rows = cursor.fetchall()
                    if existing_rows:
                        if (
                            len(existing_rows) != 1
                            or type(
                                existing_rows[0].get("activation_id")
                            )
                            is not str
                        ):
                            raise PostgresMemoryPublicationV3PersistenceError(
                                "stored activation identity has invalid shape"
                            )
                        existing, *_ = self._load_activation_row(
                            cursor,
                            activation_id=existing_rows[0]["activation_id"],
                        )
                        if existing.previous_activation_id is None:
                            previous_activation = None
                        else:
                            previous_activation, *_ = (
                                self._load_activation_row(
                                    cursor,
                                    activation_id=(
                                        existing.previous_activation_id
                                    ),
                                )
                            )
                    else:
                        head = self._select_head(
                            cursor,
                            tenant_id=approval.tenant_id,
                            repository_id=approval.repository_id,
                            memory_id=approval.memory_id,
                            for_update=True,
                        )
                        if head is None:
                            if revision.revision_number != 1:
                                raise PostgresMemoryPublicationV3ConflictError(
                                    "activation has no durable predecessor"
                                )
                            cursor.execute(
                                "INSERT INTO "
                                "trace_backed_memory_v3_memory_publication."
                                "v3_memory_revision_activation_heads "
                                "(tenant_id, repository_id, "
                                "repository_id_key, memory_id, "
                                "current_revision_number, "
                                "current_revision_id, current_activation_id) "
                                "VALUES (%s, %s, %s, %s, 0, NULL, NULL) "
                                "ON CONFLICT (tenant_id, repository_id_key, "
                                "memory_id) DO NOTHING",
                                (
                                    approval.tenant_id,
                                    approval.repository_id,
                                    approval.repository_id or "",
                                    approval.memory_id,
                                ),
                            )
                            locked = self._select_head(
                                cursor,
                                tenant_id=approval.tenant_id,
                                repository_id=approval.repository_id,
                                memory_id=approval.memory_id,
                                for_update=True,
                            )
                            if locked is None:
                                previous_activation = None
                            elif (
                                locked.current_revision_number
                                == revision.revision_number
                                and locked.current_revision_id
                                == revision.revision_id
                            ):
                                retained_activation, *_ = (
                                    self._load_activation_row(
                                        cursor,
                                        activation_id=(
                                            locked.current_activation_id
                                        ),
                                    )
                                )
                                previous_activation_id = (
                                    retained_activation.previous_activation_id
                                )
                                if previous_activation_id is None:
                                    previous_activation = None
                                else:
                                    previous_activation, *_ = (
                                        self._load_activation_row(
                                            cursor,
                                            activation_id=(
                                                previous_activation_id
                                            ),
                                        )
                                    )
                            else:
                                raise (
                                    PostgresMemoryPublicationV3ConflictError(
                                        "first activation durable head "
                                        "is occupied"
                                    )
                                )
                        else:
                            if (
                                head.current_revision_number
                                == revision.revision_number
                                and head.current_revision_id
                                == revision.revision_id
                            ):
                                retained_activation, *_ = (
                                    self._load_activation_row(
                                        cursor,
                                        activation_id=(
                                            head.current_activation_id
                                        ),
                                    )
                                )
                                previous_activation_id = (
                                    retained_activation.previous_activation_id
                                )
                                if previous_activation_id is None:
                                    previous_activation = None
                                else:
                                    previous_activation, *_ = (
                                        self._load_activation_row(
                                            cursor,
                                            activation_id=(
                                                previous_activation_id
                                            ),
                                        )
                                    )
                            elif (
                                head.current_revision_number
                                != revision.revision_number - 1
                                or head.current_revision_id
                                != revision.previous_revision_id
                            ):
                                raise PostgresMemoryPublicationV3ConflictError(
                                    "activation durable head is stale"
                                )
                            else:
                                previous_activation, *_ = (
                                    self._load_activation_row(
                                        cursor,
                                        activation_id=(
                                            head.current_activation_id
                                        ),
                                    )
                                )
                    activation = activate_memory_revision(
                        revision=revision,
                        approval=approval,
                        previous_revision=previous_revision,
                        content=content,
                        fix_evidence_by_id=fix_evidence_by_id,
                        regression_evidence_by_id=(
                            regression_evidence_by_id
                        ),
                        approval_policy=approval_policy,
                        approval_request=approval_request,
                        approval_decision=approval_decision,
                        previous_activation=previous_activation,
                        policy=policy,
                        request=request,
                        decision=decision,
                        activated_by=activated_by,
                        activated_via_client_id=activated_via_client_id,
                        activated_at=activated_at,
                        activation_attestation_sha256=(
                            activation_attestation_sha256
                        ),
                    )
                    columns = (
                        "activation_id",
                        "approval_id",
                        "revision_id",
                        "tenant_id",
                        "repository_id",
                        "repository_id_key",
                        "memory_id",
                        "revision_number",
                        "previous_activation_id",
                        "descriptor",
                        "authorization_policy_descriptor",
                        "authorization_request_descriptor",
                        "authorization_decision_descriptor",
                        "attestation_verified_by",
                    )
                    values = self._activation_values(
                        activation,
                        policy,
                        request,
                        decision,
                        verifier_id,
                    )
                    inserted = self._put_exact(
                        cursor,
                        table=(
                            "trace_backed_memory_v3_memory_publication."
                            "v3_memory_revision_activations"
                        ),
                        id_column="activation_id",
                        columns=columns,
                        values=values,
                        conflict_message="activation identity conflict",
                    )
                    if inserted:
                        cursor.execute(
                            "UPDATE "
                            "trace_backed_memory_v3_memory_publication."
                            "v3_memory_revision_activation_heads "
                            "SET current_revision_number = %s, "
                            "current_revision_id = %s, "
                            "current_activation_id = %s "
                            "WHERE tenant_id = %s "
                            "AND repository_id_key = %s "
                            "AND memory_id = %s "
                            "AND current_revision_number = %s "
                            "AND current_activation_id "
                            "IS NOT DISTINCT FROM %s",
                            (
                                activation.revision_number,
                                activation.revision_id,
                                activation.activation_id,
                                activation.tenant_id,
                                activation.repository_id or "",
                                activation.memory_id,
                                activation.revision_number - 1,
                                activation.previous_activation_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise PostgresMemoryPublicationV3ConflictError(
                                "activation lost durable head compare-and-swap"
                            )
                    retained = self._load_activation_row(
                        cursor,
                        activation_id=activation.activation_id,
                    )
                    if not _activation_provenance_matches(
                        retained,
                        activation,
                        policy,
                        request,
                        decision,
                        verifier_id,
                    ):
                        raise PostgresMemoryPublicationV3PersistenceError(
                            "activation read-back mismatch"
                        )
                    if inserted:
                        head = self._select_head(
                            cursor,
                            tenant_id=activation.tenant_id,
                            repository_id=activation.repository_id,
                            memory_id=activation.memory_id,
                            for_update=True,
                        )
                        if (
                            head is None
                            or head.current_activation_id
                            != activation.activation_id
                            or head.current_revision_id
                            != activation.revision_id
                            or head.current_revision_number
                            != activation.revision_number
                        ):
                            raise PostgresMemoryPublicationV3ConflictError(
                                "activation is not the durable current head"
                            )
        except (PostgresMemoryPublicationV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_storage_error(
                error,
                "append memory revision activation",
            )
        return PostgresMemoryPublicationV3ActivationResult(
            activation=activation,
            inserted=inserted,
            attestation_verified_by=verifier_id,
        )

    @_synchronized
    def load_approval(
        self,
        approval_id: str,
    ) -> PostgresMemoryPublicationV3ApprovalResult:
        self._require_open()
        _identifier(approval_id, "approval_id")
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    approval, _policy, _request, _decision, verifier = (
                        self._load_approval_row(
                            cursor,
                            approval_id=approval_id,
                        )
                    )
        except (PostgresMemoryPublicationV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_storage_error(error, "load memory revision approval")
        return PostgresMemoryPublicationV3ApprovalResult(
            approval=approval,
            inserted=False,
            attestation_verified_by=verifier,
        )

    @_synchronized
    def load_activation(
        self,
        activation_id: str,
    ) -> PostgresMemoryPublicationV3ActivationResult:
        self._require_open()
        _identifier(activation_id, "activation_id")
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    activation, _policy, _request, _decision, verifier = (
                        self._load_activation_row(
                            cursor,
                            activation_id=activation_id,
                        )
                    )
        except (PostgresMemoryPublicationV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_storage_error(
                error,
                "load memory revision activation",
            )
        return PostgresMemoryPublicationV3ActivationResult(
            activation=activation,
            inserted=False,
            attestation_verified_by=verifier,
        )

    @_synchronized
    def load_head(
        self,
        *,
        tenant_id: str,
        repository_id: str | None,
        memory_id: str,
    ) -> PostgresMemoryPublicationV3Head:
        self._require_open()
        _identifier(tenant_id, "tenant_id")
        if repository_id is not None:
            _identifier(repository_id, "repository_id")
        _identifier(memory_id, "memory_id")
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    head = self._select_head(
                        cursor,
                        tenant_id=tenant_id,
                        repository_id=repository_id,
                        memory_id=memory_id,
                        for_update=False,
                    )
        except (PostgresMemoryPublicationV3Error, ValueError):
            raise
        except Exception as error:
            self._raise_storage_error(
                error,
                "load memory revision activation head",
            )
        if head is None:
            raise PostgresMemoryPublicationV3NotFoundError(
                "memory revision activation head was not found"
            )
        return head

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresMemoryPublicationV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
