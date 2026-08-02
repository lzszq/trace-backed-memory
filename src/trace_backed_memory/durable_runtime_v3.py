from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import secrets
import sqlite3
from threading import RLock
from typing import NoReturn, Protocol

from ._timestamps import utc_timestamp
from .completion_outbox_v3 import CompletionOutboxEvent
from .completion_outbox_worker_v3 import (
    CompletionOutboxConsumerReceipt,
    CompletionOutboxDeliveryWorker,
    CompletionOutboxWorkerResult,
)
from .durable_agent_v3 import AuthenticatedDurableAgentMemory
from .durable_composition_v3 import (
    DurableAuthorityGraph,
    DurableServiceBundle,
)
from .durable_agent_wire_v1 import (
    DurableAgentProtocolDispatcher,
    DurableAgentWireConfiguration,
    RepositoryIdResolver,
)
from .durable_execution_v3 import (
    DurableExecutionService,
    OutcomeEvaluatorAuthenticator,
)
from .durable_finalization_v3 import DurableFinalizationService
from .durable_retrieval_preparation_v3 import (
    DurablePreparedGateEvidenceVerifier,
    DurableRetrievalPreparationService,
)
from .durable_semantic_gate_v3 import (
    AuthenticatedSemanticGateSessionService,
)
from .ledger_replay_export_v1 import ContextualLedgerReplayExportReaderV1
from .gate_service_v3 import AuthenticatedGateSessionService
from .gate_worker_v3 import (
    GateSessionRecoveryResult,
    GateSessionRecoveryWorker,
)
from .entity_registry_v3 import EntityRegistrySnapshot
from .postgres_authorization_v3 import PostgresAuthorizationV3Repository
from .postgres_completion_outbox_v3 import (
    PostgresCompletionOutboxV3Repository,
)
from .postgres_event_ledger_v1 import PostgresEventLedgerV1
from .postgres_gate_evidence_v3 import PostgresGateEvidenceV3Repository
from .postgres_replay_v3 import PostgresReplayV3Repository
from .postgres_semantic_gate_artifact_v3 import (
    PostgresSemanticGateArtifactV3Repository,
)
from .retrieval_policy_v3 import RetrievalPolicyBundle
from .retrieval_preparation_v3 import (
    ActivatedRevisionRetrievalSource,
    AuthenticatedRetrievalPreparationService,
    CandidateDiscovery,
)
from .semantic_gate_service_v3 import (
    AuthenticatedSemanticGateService,
    SemanticGateServiceConfiguration,
    TrustedSemanticProvider,
)
from .service_v3 import AuthenticatedRetrievalService
from .sqlite_authorization_v3 import SQLiteAuthorizationV3Repository
from .sqlite_completion_outbox_v3 import (
    SQLiteCompletionOutboxV3Repository,
)
from .sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from .sqlite_gate_evidence_v3 import SQLiteGateEvidenceV3Repository
from .sqlite_replay_v3 import SQLiteReplayV3Repository
from .sqlite_semantic_gate_artifact_v3 import (
    SQLiteSemanticGateArtifactV3Repository,
)
from .sqlite_bundle_v3 import (
    SQLITE_V3_BUNDLE_RESOURCE,
    SQLiteV3BundleError,
    install_sqlite_v3_bundle,
    verify_sqlite_v3_bundle,
)


DURABLE_RUNTIME_CONTRACT_VERSION = "tbm.durable-runtime.v3"
DURABLE_SQLITE_RUNTIME_SCHEMA_RESOURCES = (
    SQLITE_V3_BUNDLE_RESOURCE,
)
_POSTGRES_SCHEMA_CATALOG = (
    ("trace_backed_memory_v3_authorization", "tbm.authorization.v3"),
    ("trace_backed_memory_v3_gate_session", "tbm.gate-session.v3"),
    ("trace_backed_memory_v3_gate_evidence", "tbm.gate-evidence.v3"),
    (
        "trace_backed_memory_v3_semantic_gate",
        "tbm.semantic-gate-attempt.v3",
    ),
    (
        "trace_backed_memory_v3_semantic_gate_artifacts",
        "tbm.semantic-gate-artifact.v3",
    ),
    ("trace_backed_memory_v3_replay", "tbm.replay.v3"),
    ("trace_backed_memory_v3_outcome", "tbm.run-outcome.v3"),
    (
        "trace_backed_memory_v3_completion_outbox",
        "tbm.completion-outbox.v3",
    ),
    (
        "trace_backed_memory_v3_event_ledger",
        "tbm.event-ledger-port.v1",
    ),
)


class DurableRuntimeV3Error(RuntimeError):
    """Stable failure while constructing or operating a durable runtime."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DurableRuntimeDependencies:
    """Server-owned services and policies that are not persisted by this graph."""

    registry_provider: Callable[[], EntityRegistrySnapshot]
    policy_provider: Callable[[], RetrievalPolicyBundle]
    discovery: CandidateDiscovery
    revision_source: ActivatedRevisionRetrievalSource
    semantic_provider: TrustedSemanticProvider
    semantic_configuration: SemanticGateServiceConfiguration
    evaluator_authenticator: OutcomeEvaluatorAuthenticator
    repository_id_resolver: RepositoryIdResolver
    clock: Callable[[], str] = utc_timestamp
    authorization_request_id_factory: Callable[[], str] = (
        lambda: f"authorization_request_{secrets.token_hex(16)}"
    )
    session_id_factory: Callable[[], str] = (
        lambda: f"gate_session_{secrets.token_hex(16)}"
    )
    retrieval_evaluator_id: str = "system_gate"
    retrieval_evaluator_version: str = "v1"
    completion_consumer: (
        Callable[[CompletionOutboxEvent], CompletionOutboxConsumerReceipt]
        | None
    ) = None

    def __post_init__(self) -> None:
        for callback in (
            self.registry_provider,
            self.policy_provider,
            self.evaluator_authenticator,
            self.repository_id_resolver,
            self.clock,
            self.authorization_request_id_factory,
            self.session_id_factory,
        ):
            if not callable(callback):
                raise TypeError("durable runtime dependency is not callable")
        if not callable(getattr(self.discovery, "discover", None)):
            raise TypeError("discovery must provide discover()")
        if not all(
            callable(getattr(self.revision_source, name, None))
            for name in ("load_authorized", "verify_current")
        ):
            raise TypeError("revision_source is invalid")
        if type(self.semantic_provider) is not TrustedSemanticProvider:
            raise TypeError(
                "semantic_provider must be TrustedSemanticProvider"
            )
        if (
            type(self.semantic_configuration)
            is not SemanticGateServiceConfiguration
        ):
            raise TypeError(
                "semantic_configuration must be "
                "SemanticGateServiceConfiguration"
            )
        if self.completion_consumer is not None and not callable(
            self.completion_consumer
        ):
            raise TypeError("completion_consumer must be callable")
        for value in (
            self.retrieval_evaluator_id,
            self.retrieval_evaluator_version,
        ):
            if (
                type(value) is not str
                or not value
                or value.strip() != value
                or len(value) > 128
            ):
                raise ValueError(
                    "retrieval evaluator identifiers must be bounded"
                )


class _RuntimeLock(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class _RuntimeOperationGuard:
    """Serialize one runtime operation and reject use after close."""

    def __init__(
        self,
        lock: _RuntimeLock,
        require_open: Callable[[], None],
    ) -> None:
        self._lock = lock
        self._require_open = require_open

    def __enter__(self) -> None:
        self._lock.acquire()
        try:
            self._require_open()
        except Exception:
            self._lock.release()
            raise

    def __exit__(self, *args: object) -> None:
        self._lock.release()


class DurableSQLiteRuntime:
    """One coherent SQLite authority graph for the durable Agent facade.

    All operations and worker ticks share one re-entrant process lock because
    the repositories use one SQLite connection for cross-authority atomicity.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        dependencies: DurableRuntimeDependencies,
        *,
        owns_connection: bool = False,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be sqlite3.Connection")
        if type(dependencies) is not DurableRuntimeDependencies:
            raise TypeError("dependencies must be DurableRuntimeDependencies")
        if type(owns_connection) is not bool:
            raise TypeError("owns_connection must be a boolean")
        self._connection = connection
        self._owns_connection = owns_connection
        self._dependencies = dependencies
        self._operation_lock = RLock()
        self._closed = False
        self._operation_guard = _RuntimeOperationGuard(
            self._operation_lock,
            self._require_open,
        )
        try:
            if not connection.in_transaction:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA recursive_triggers = ON")
            if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                _runtime_failed(
                    "TBM_DURABLE_RUNTIME_SQLITE_FOREIGN_KEYS",
                    "durable SQLite runtime requires foreign keys",
                )
            if connection.execute("PRAGMA recursive_triggers").fetchone() != (
                1,
            ):
                _runtime_failed(
                    "TBM_DURABLE_RUNTIME_SQLITE_RECURSIVE_TRIGGERS",
                    "durable SQLite runtime requires recursive triggers",
                )
            verify_sqlite_v3_bundle(connection)
            self.authorization_repository = SQLiteAuthorizationV3Repository(
                connection
            )
            self.evidence_repository = SQLiteGateEvidenceV3Repository(
                connection
            )
            self.evidence_repository.enable_event_first()
            self.semantic_repository = (
                SQLiteSemanticGateArtifactV3Repository(connection)
            )
            self.semantic_repository.enable_event_first()
            self.replay_repository = SQLiteReplayV3Repository(connection)
            self.replay_repository.enable_event_first()
            self.replay_export_reader = ContextualLedgerReplayExportReaderV1(
                lambda access: SQLiteEventLedgerV1(connection, access),
                self.replay_repository,
            )
            self.outbox_repository = SQLiteCompletionOutboxV3Repository(
                connection,
                clock=dependencies.clock,
            )
            self.sessions = self.outbox_repository.gate_sessions
            self.sessions.enable_event_first()

            self.authorization_service = AuthenticatedRetrievalService(
                registry_provider=dependencies.registry_provider,
                decision_writer=self.authorization_repository,
                clock=dependencies.clock,
                request_id_factory=(
                    dependencies.authorization_request_id_factory
                ),
            )
            retrieval = AuthenticatedRetrievalPreparationService(
                authorization_service=self.authorization_service,
                policy_provider=dependencies.policy_provider,
                discovery=dependencies.discovery,
                revision_source=dependencies.revision_source,
                clock=dependencies.clock,
                evaluator_id=dependencies.retrieval_evaluator_id,
                evaluator_version=dependencies.retrieval_evaluator_version,
            )
            gate = AuthenticatedGateSessionService(
                authorization_service=self.authorization_service,
                session_writer=self.sessions,
                session_id_factory=dependencies.session_id_factory,
                evidence_verifier=DurablePreparedGateEvidenceVerifier(
                    self.evidence_repository
                ),
            )
            preparation = DurableRetrievalPreparationService(
                gate_session_service=gate,
                retrieval_service=retrieval,
                evidence_authority=self.evidence_repository,
            )
            semantic = AuthenticatedSemanticGateService(
                provider=dependencies.semantic_provider,
                configuration=dependencies.semantic_configuration,
                evidence_reader=self.evidence_repository,
                authority=self.semantic_repository,
                clock=dependencies.clock,
            )
            semantic_session = AuthenticatedSemanticGateSessionService(
                semantic_gate_service=semantic,
                session_writer=self.sessions,
            )
            finalization = DurableFinalizationService(
                authorization_service=self.authorization_service,
                session_writer=self.sessions,
                evidence_reader=self.evidence_repository,
                semantic_authority=self.semantic_repository,
                revision_source=dependencies.revision_source,
                policy_loader=dependencies.policy_provider,
                replay_authority=self.replay_repository,
                clock=dependencies.clock,
            )
            execution = DurableExecutionService(
                authorization_service=self.authorization_service,
                session_writer=self.sessions,
                finalization_reader=finalization,
                completion_authority=self.outbox_repository,
                evaluator_authenticator=dependencies.evaluator_authenticator,
                clock=dependencies.clock,
            )
            self.authority_graph = DurableAuthorityGraph(
                authorization_service=self.authorization_service,
                session_authority=self.sessions,
                evidence_authority=self.evidence_repository,
                semantic_authority=self.semantic_repository,
                revision_source=dependencies.revision_source,
                replay_authority=self.replay_repository,
                completion_authority=self.outbox_repository,
                replay_export_reader=self.replay_export_reader,
            )
            self.service_bundle = DurableServiceBundle(
                authority_graph=self.authority_graph,
                preparation_service=preparation,
                semantic_service=semantic_session,
                finalization_service=finalization,
                execution_service=execution,
            )
            self.agent = AuthenticatedDurableAgentMemory(
                service_bundle=self.service_bundle
            )
            self.dispatcher = DurableAgentProtocolDispatcher(
                DurableAgentWireConfiguration(
                    "sqlite",
                    expose_injection_content=expose_injection_content,
                    expose_replay_content=expose_replay_content,
                ),
                self.agent,
                repository_id_resolver=(
                    dependencies.repository_id_resolver
                ),
                evaluator_resolver=dependencies.evaluator_authenticator,
                operation_lock=self._operation_guard,
            )
            self.gate_recovery_worker = GateSessionRecoveryWorker(
                self.sessions
            )
            self.outbox_worker = (
                None
                if dependencies.completion_consumer is None
                else CompletionOutboxDeliveryWorker(
                    self.outbox_repository,
                    dependencies.completion_consumer,
                )
            )
        except DurableRuntimeV3Error:
            self._close_partial()
            raise
        except SQLiteV3BundleError as error:
            self._close_partial()
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_SQLITE_SCHEMA_INVALID",
                "durable SQLite schema bundle is invalid",
            ) from error
        except Exception as error:
            self._close_partial()
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_SQLITE_CONSTRUCTION_FAILED",
                "durable SQLite authority graph could not be constructed",
            ) from error

    @classmethod
    def connect(
        cls,
        database: str | bytes | Path = ":memory:",
        *,
        dependencies: DurableRuntimeDependencies,
        initialize: bool = False,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
        **kwargs: object,
    ) -> DurableSQLiteRuntime:
        if type(initialize) is not bool:
            raise TypeError("initialize must be a boolean")
        kwargs.setdefault("check_same_thread", False)
        try:
            connection = sqlite3.connect(database, **kwargs)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA recursive_triggers = ON")
            if initialize:
                install_sqlite_v3_bundle(connection)
        except (
            OSError,
            UnicodeError,
            sqlite3.Error,
            SQLiteV3BundleError,
            TypeError,
            ValueError,
        ) as error:
            if "connection" in locals():
                connection.close()
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_SQLITE_CONNECT_FAILED",
                "durable SQLite storage could not be opened",
            ) from error
        try:
            return cls(
                connection,
                dependencies,
                owns_connection=True,
                expose_injection_content=expose_injection_content,
                expose_replay_content=expose_replay_content,
            )
        except Exception:
            connection.close()
            raise

    def recover_due(
        self,
        *,
        limit: int = 100,
    ) -> tuple[GateSessionRecoveryResult, ...]:
        with self._operation_guard:
            return self.gate_recovery_worker.run_once(limit=limit)

    def deliver_outbox(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        limit: int = 100,
        retry_delay_seconds: int = 60,
        max_attempts: int = 5,
    ) -> tuple[CompletionOutboxWorkerResult, ...]:
        with self._operation_guard:
            worker = self.outbox_worker
            if worker is None:
                raise DurableRuntimeV3Error(
                    "TBM_DURABLE_RUNTIME_OUTBOX_CONSUMER_MISSING",
                    "durable completion outbox consumer is not configured",
                )
            return worker.run_once(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                limit=limit,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            )

    def _require_open(self) -> None:
        if self._closed:
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_CLOSED",
                "durable SQLite runtime is closed",
            )

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._close_partial()

    def _close_partial(self) -> None:
        for name in (
            "semantic_repository",
            "replay_repository",
            "evidence_repository",
            "authorization_repository",
            "outbox_repository",
        ):
            repository = getattr(self, name, None)
            close = getattr(repository, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if self._owns_connection:
            try:
                self._connection.close()
            except Exception:
                pass

    def __enter__(self) -> DurableSQLiteRuntime:
        with self._operation_guard:
            pass
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class DurablePostgresRuntime:
    """One coherent PostgreSQL authority graph for the durable Agent facade.

    Schema installation remains an explicit operator migration. Construction
    verifies every isolated v3 catalog before exposing the dispatcher.
    """

    def __init__(
        self,
        connection: object,
        dependencies: DurableRuntimeDependencies,
        *,
        owns_connection: bool = False,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
    ) -> None:
        if connection is None or bool(getattr(connection, "closed", False)):
            raise TypeError("an open PostgreSQL connection is required")
        if type(dependencies) is not DurableRuntimeDependencies:
            raise TypeError("dependencies must be DurableRuntimeDependencies")
        if type(owns_connection) is not bool:
            raise TypeError("owns_connection must be a boolean")
        self._connection = connection
        self._owns_connection = owns_connection
        self._dependencies = dependencies
        self._operation_lock = RLock()
        self._closed = False
        self._operation_guard = _RuntimeOperationGuard(
            self._operation_lock,
            self._require_open,
        )
        try:
            _verify_postgres_schema_catalog(connection)
            self.authorization_repository = (
                PostgresAuthorizationV3Repository(connection)
            )
            self.evidence_repository = PostgresGateEvidenceV3Repository(
                connection
            )
            self.evidence_repository.enable_event_first()
            self.semantic_repository = (
                PostgresSemanticGateArtifactV3Repository(connection)
            )
            self.semantic_repository.enable_event_first()
            self.replay_repository = PostgresReplayV3Repository(connection)
            self.replay_repository.enable_event_first()
            self.replay_export_reader = ContextualLedgerReplayExportReaderV1(
                lambda access: PostgresEventLedgerV1(connection, access),
                self.replay_repository,
            )
            self.outbox_repository = (
                PostgresCompletionOutboxV3Repository(connection)
            )
            self.sessions = self.outbox_repository.gate_sessions
            self.sessions.enable_event_first()

            self.authorization_service = AuthenticatedRetrievalService(
                registry_provider=dependencies.registry_provider,
                decision_writer=self.authorization_repository,
                clock=dependencies.clock,
                request_id_factory=(
                    dependencies.authorization_request_id_factory
                ),
            )
            retrieval = AuthenticatedRetrievalPreparationService(
                authorization_service=self.authorization_service,
                policy_provider=dependencies.policy_provider,
                discovery=dependencies.discovery,
                revision_source=dependencies.revision_source,
                clock=dependencies.clock,
                evaluator_id=dependencies.retrieval_evaluator_id,
                evaluator_version=dependencies.retrieval_evaluator_version,
            )
            gate = AuthenticatedGateSessionService(
                authorization_service=self.authorization_service,
                session_writer=self.sessions,
                session_id_factory=dependencies.session_id_factory,
                evidence_verifier=DurablePreparedGateEvidenceVerifier(
                    self.evidence_repository
                ),
            )
            preparation = DurableRetrievalPreparationService(
                gate_session_service=gate,
                retrieval_service=retrieval,
                evidence_authority=self.evidence_repository,
            )
            semantic = AuthenticatedSemanticGateService(
                provider=dependencies.semantic_provider,
                configuration=dependencies.semantic_configuration,
                evidence_reader=self.evidence_repository,
                authority=self.semantic_repository,
                clock=dependencies.clock,
            )
            semantic_session = AuthenticatedSemanticGateSessionService(
                semantic_gate_service=semantic,
                session_writer=self.sessions,
            )
            finalization = DurableFinalizationService(
                authorization_service=self.authorization_service,
                session_writer=self.sessions,
                evidence_reader=self.evidence_repository,
                semantic_authority=self.semantic_repository,
                revision_source=dependencies.revision_source,
                policy_loader=dependencies.policy_provider,
                replay_authority=self.replay_repository,
                clock=dependencies.clock,
            )
            execution = DurableExecutionService(
                authorization_service=self.authorization_service,
                session_writer=self.sessions,
                finalization_reader=finalization,
                completion_authority=self.outbox_repository,
                evaluator_authenticator=dependencies.evaluator_authenticator,
                clock=dependencies.clock,
            )
            self.authority_graph = DurableAuthorityGraph(
                authorization_service=self.authorization_service,
                session_authority=self.sessions,
                evidence_authority=self.evidence_repository,
                semantic_authority=self.semantic_repository,
                revision_source=dependencies.revision_source,
                replay_authority=self.replay_repository,
                completion_authority=self.outbox_repository,
                replay_export_reader=self.replay_export_reader,
            )
            self.service_bundle = DurableServiceBundle(
                authority_graph=self.authority_graph,
                preparation_service=preparation,
                semantic_service=semantic_session,
                finalization_service=finalization,
                execution_service=execution,
            )
            self.agent = AuthenticatedDurableAgentMemory(
                service_bundle=self.service_bundle
            )
            self.dispatcher = DurableAgentProtocolDispatcher(
                DurableAgentWireConfiguration(
                    "postgres",
                    expose_injection_content=expose_injection_content,
                    expose_replay_content=expose_replay_content,
                ),
                self.agent,
                repository_id_resolver=(
                    dependencies.repository_id_resolver
                ),
                evaluator_resolver=dependencies.evaluator_authenticator,
                operation_lock=self._operation_guard,
            )
            self.gate_recovery_worker = GateSessionRecoveryWorker(
                self.sessions
            )
            self.outbox_worker = (
                None
                if dependencies.completion_consumer is None
                else CompletionOutboxDeliveryWorker(
                    self.outbox_repository,
                    dependencies.completion_consumer,
                )
            )
        except DurableRuntimeV3Error:
            self._close_partial()
            raise
        except Exception as error:
            self._close_partial()
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_POSTGRES_CONSTRUCTION_FAILED",
                "durable PostgreSQL authority graph could not be constructed",
            ) from error

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        *,
        dependencies: DurableRuntimeDependencies,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
        **kwargs: object,
    ) -> DurablePostgresRuntime:
        try:
            import psycopg

            connection = psycopg.connect(conninfo, **kwargs)
        except (ImportError, OSError, TypeError, ValueError) as error:
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_POSTGRES_CONNECT_FAILED",
                "durable PostgreSQL storage could not be opened",
            ) from error
        except Exception as error:
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_POSTGRES_CONNECT_FAILED",
                "durable PostgreSQL storage could not be opened",
            ) from error
        try:
            return cls(
                connection,
                dependencies,
                owns_connection=True,
                expose_injection_content=expose_injection_content,
                expose_replay_content=expose_replay_content,
            )
        except Exception:
            connection.close()
            raise

    def recover_due(
        self,
        *,
        limit: int = 100,
    ) -> tuple[GateSessionRecoveryResult, ...]:
        with self._operation_guard:
            return self.gate_recovery_worker.run_once(limit=limit)

    def deliver_outbox(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        limit: int = 100,
        retry_delay_seconds: int = 60,
        max_attempts: int = 5,
    ) -> tuple[CompletionOutboxWorkerResult, ...]:
        with self._operation_guard:
            worker = self.outbox_worker
            if worker is None:
                raise DurableRuntimeV3Error(
                    "TBM_DURABLE_RUNTIME_OUTBOX_CONSUMER_MISSING",
                    "durable completion outbox consumer is not configured",
                )
            return worker.run_once(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                limit=limit,
                retry_delay_seconds=retry_delay_seconds,
                max_attempts=max_attempts,
            )

    def _require_open(self) -> None:
        if self._closed or bool(getattr(self._connection, "closed", False)):
            raise DurableRuntimeV3Error(
                "TBM_DURABLE_RUNTIME_CLOSED",
                "durable PostgreSQL runtime is closed",
            )

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._close_partial()

    def _close_partial(self) -> None:
        for name in (
            "semantic_repository",
            "replay_repository",
            "evidence_repository",
            "authorization_repository",
            "outbox_repository",
        ):
            repository = getattr(self, name, None)
            close = getattr(repository, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if self._owns_connection:
            close_connection = getattr(self._connection, "close", None)
            if callable(close_connection):
                try:
                    close_connection()
                except Exception:
                    pass

    def __enter__(self) -> DurablePostgresRuntime:
        with self._operation_guard:
            pass
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@dataclass(frozen=True)
class DurableRuntimeFactory:
    """The only supported constructor for transport-owned durable stacks."""

    dependencies: DurableRuntimeDependencies

    def __post_init__(self) -> None:
        if type(self.dependencies) is not DurableRuntimeDependencies:
            raise TypeError(
                "dependencies must be DurableRuntimeDependencies"
            )

    def open_sqlite(
        self,
        database: str | bytes | Path = ":memory:",
        *,
        initialize: bool = False,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
        **kwargs: object,
    ) -> DurableSQLiteRuntime:
        return DurableSQLiteRuntime.connect(
            database,
            dependencies=self.dependencies,
            initialize=initialize,
            expose_injection_content=expose_injection_content,
            expose_replay_content=expose_replay_content,
            **kwargs,
        )

    def bind_sqlite(
        self,
        connection: sqlite3.Connection,
        *,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
    ) -> DurableSQLiteRuntime:
        return DurableSQLiteRuntime(
            connection,
            self.dependencies,
            expose_injection_content=expose_injection_content,
            expose_replay_content=expose_replay_content,
        )

    def open_postgres(
        self,
        conninfo: str = "",
        *,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
        **kwargs: object,
    ) -> DurablePostgresRuntime:
        return DurablePostgresRuntime.connect(
            conninfo,
            dependencies=self.dependencies,
            expose_injection_content=expose_injection_content,
            expose_replay_content=expose_replay_content,
            **kwargs,
        )

    def bind_postgres(
        self,
        connection: object,
        *,
        expose_injection_content: bool = False,
        expose_replay_content: bool = False,
    ) -> DurablePostgresRuntime:
        return DurablePostgresRuntime(
            connection,
            self.dependencies,
            expose_injection_content=expose_injection_content,
            expose_replay_content=expose_replay_content,
        )


def _runtime_failed(code: str, message: str) -> NoReturn:
    raise DurableRuntimeV3Error(code, message)


def _verify_postgres_schema_catalog(connection: object) -> None:
    info = getattr(connection, "info", None)
    initial_status = getattr(info, "transaction_status", None)
    was_idle = (
        initial_status == 0
        or getattr(initial_status, "name", None) == "IDLE"
    )
    try:
        with connection.cursor() as cursor:
            for schema, contract in _POSTGRES_SCHEMA_CATALOG:
                cursor.execute(
                    "SELECT schema_version, contract_version "
                    f"FROM {schema}.schema_metadata"
                )
                rows = cursor.fetchall()
                if rows != [(1, contract)]:
                    _runtime_failed(
                        "TBM_DURABLE_RUNTIME_POSTGRES_SCHEMA_INVALID",
                        "durable PostgreSQL schema catalog is invalid",
                    )
    except DurableRuntimeV3Error:
        raise
    except Exception as error:
        raise DurableRuntimeV3Error(
            "TBM_DURABLE_RUNTIME_POSTGRES_SCHEMA_INVALID",
            "durable PostgreSQL schema catalog is invalid",
        ) from error
    finally:
        current_status = getattr(
            getattr(connection, "info", None),
            "transaction_status",
            None,
        )
        if was_idle and not (
            current_status == 0
            or getattr(current_status, "name", None) == "IDLE"
        ):
            try:
                connection.rollback()
            except Exception:
                pass


DurableSQLiteRuntimeDependencies = DurableRuntimeDependencies
DurableSQLiteRuntimeV3Error = DurableRuntimeV3Error


__all__ = [
    "DURABLE_RUNTIME_CONTRACT_VERSION",
    "DURABLE_SQLITE_RUNTIME_SCHEMA_RESOURCES",
    "DurablePostgresRuntime",
    "DurableRuntimeDependencies",
    "DurableRuntimeFactory",
    "DurableRuntimeV3Error",
    "DurableSQLiteRuntime",
    "DurableSQLiteRuntimeDependencies",
    "DurableSQLiteRuntimeV3Error",
]
