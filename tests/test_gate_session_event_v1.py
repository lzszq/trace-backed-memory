from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.gate_session_event_v1 as gate_session_events
from tests.test_artifact_service_v3 import _registry
from trace_backed_memory.event_registry_v1 import (
    EventPayloadRegistration,
    EventRegistryV1Error,
    dumps_event_registry_catalog,
)
from trace_backed_memory.gate_session_event_v1 import (
    EXECUTION_ABANDONED,
    EXECUTION_STARTED,
    GATE_SESSION_AWAITING_DECISION,
    GATE_SESSION_BASELINE_IMPORTED,
    GATE_SESSION_CANCELED,
    GATE_SESSION_CREATED,
    GATE_SESSION_EVENT_TYPES,
    GATE_SESSION_EXPIRED,
    GATE_SESSION_LEASE_RENEWED,
    GATE_SESSION_PREPARED,
    SEMANTIC_GATE_DECIDED,
    USAGE_DECISION_FINALIZED,
    GateSessionEventLedgerProjector,
    RegistryGateSessionLedgerAccessResolver,
    build_gate_session_event_registry,
    dumps_gate_session_event_payload_dispatch_schema,
    gate_session_projection_sha256,
    gate_session_stream_id,
    reduce_gate_session_events,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.sqlite_bundle_v3 import install_sqlite_v3_bundle
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.sqlite_gate_session_v3 import (
    SQLiteGateSessionRepository,
)


ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(
            2026,
            8,
            1,
            1,
            0,
            0,
            100_000,
            tzinfo=timezone.utc,
        )

    def __call__(self) -> str:
        value = self._next
        self._next += timedelta(milliseconds=125)
        return value.isoformat().replace("+00:00", "Z")

    def advance(self, *, seconds: int) -> None:
        self._next += timedelta(seconds=seconds)


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="service_durable_event_adapter",
        authorization_decision_id="authorization_durable_event_append",
        classification_filter=LedgerClassificationFilter(("internal",)),
    )


def _repository(
    connection: sqlite3.Connection,
) -> tuple[SQLiteGateSessionRepository, GateSessionEventLedgerProjector]:
    projector = GateSessionEventLedgerProjector(
        ledger_factory=lambda access: SQLiteEventLedgerV1(connection, access),
        access_resolver=lambda _session: _access(),
    )
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    repository.bind_revision_event_sink(projector)
    return repository, projector


def _create(repository: SQLiteGateSessionRepository):
    return repository.create_or_get(
        session_id="gate_session_event_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint="sha256:" + "1" * 64,
        idempotency_key="idempotency_event_001",
        expires_in_seconds=3_600,
    ).session


def _stream(
    connection: sqlite3.Connection,
    session_id: str,
):
    with SQLiteEventLedgerV1(connection, _access()) as ledger:
        return ledger.read_stream(
            gate_session_stream_id(session_id),
            from_version=1,
            limit=100,
        ).events


def test_gate_session_event_registry_is_strict_and_complete() -> None:
    registry = build_gate_session_event_registry()

    assert registry.sealed is True
    assert (
        tuple(sorted(item["event_type"] for item in registry.catalog()["event_types"]))
        == GATE_SESSION_EVENT_TYPES
    )
    registration = registry.catalog()["event_types"][0]
    assert registration["schema"]["additionalProperties"] is False

    invalid = dict(registration["schema"])
    invalid["additionalProperties"] = True
    with pytest.raises(EventRegistryV1Error):
        EventPayloadRegistration(
            event_type=registration["event_type"],
            event_version=1,
            event_kind="domain",
            payload_schema="tbm.gate_session.invalid.v1",
            schema=invalid,
        )


def test_gate_session_event_api_is_exported_from_package_root() -> None:
    for name in gate_session_events.__all__:
        assert getattr(tbm, name) is getattr(gate_session_events, name)


def test_gate_session_event_registry_artifacts_match_runtime_exactly() -> None:
    registry = build_gate_session_event_registry()
    catalog_name = "examples/gate_session_event_type_registry_v1.example.json"
    schema_name = "schemas/gate_session_event_payload_registry_v1.schema.json"
    catalog_bytes = (ROOT / catalog_name).read_bytes()
    schema_bytes = (ROOT / schema_name).read_bytes()

    assert json.loads(catalog_bytes) == registry.catalog()
    assert dumps_event_registry_catalog(registry) == dumps_event_registry_catalog(
        build_gate_session_event_registry()
    )
    assert schema_bytes.decode("utf-8") == (
        dumps_gate_session_event_payload_dispatch_schema()
    )
    assert tbm.read_packaged_resource(catalog_name) == catalog_bytes
    assert tbm.read_packaged_resource(schema_name) == schema_bytes


def test_gate_session_events_rebuild_every_revision_exactly() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    repository, _projector = _repository(connection)

    created = _create(repository)
    prepared = repository.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=600,
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_evaluation_001",
    )
    awaiting = repository.transition(
        prepared.session_id,
        "awaiting_decision",
        expected_version=prepared.version,
    )
    renewed = repository.renew_lease(
        awaiting.session_id,
        expected_version=awaiting.version,
        lease_seconds=700,
    )
    decided = repository.transition(
        renewed.session_id,
        "decided",
        expected_version=renewed.version,
        semantic_gate_attempt_ids=("semantic_gate_attempt_001",),
        decision_id="decision_001",
    )
    finalized = repository.transition(
        decided.session_id,
        "finalized",
        expected_version=decided.version,
        final_memory_revision_ids=("memory_revision_001",),
        injection_artifact_id="injection_artifact_001",
        usage_decision_id="usage_decision_001",
    )
    executing = repository.transition(
        finalized.session_id,
        "executing",
        expected_version=finalized.version,
    )
    abandoned = repository.transition(
        executing.session_id,
        "abandoned",
        expected_version=executing.version,
        terminal_reason="execution_lease_abandoned",
    )

    events = _stream(connection, created.session_id)
    assert tuple(event.event_type for event in events) == (
        GATE_SESSION_CREATED,
        GATE_SESSION_PREPARED,
        GATE_SESSION_AWAITING_DECISION,
        GATE_SESSION_LEASE_RENEWED,
        SEMANTIC_GATE_DECIDED,
        USAGE_DECISION_FINALIZED,
        EXECUTION_STARTED,
        EXECUTION_ABANDONED,
    )
    assert tuple(event.stream_version for event in events) == tuple(range(1, 9))
    rebuilt = reduce_gate_session_events(events)
    for field in fields(abandoned):
        assert getattr(rebuilt, field.name) == getattr(abandoned, field.name)
    assert gate_session_projection_sha256(rebuilt) == gate_session_projection_sha256(
        abandoned
    )
    assert rebuilt == abandoned
    assert repository.get(created.session_id) == abandoned


@pytest.mark.parametrize(
    ("terminal_status", "event_type"),
    (
        ("canceled", GATE_SESSION_CANCELED),
        ("expired", GATE_SESSION_EXPIRED),
    ),
)
def test_canceled_and_expired_revisions_have_exact_terminal_events(
    terminal_status: str,
    event_type: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    clock = _Clock()
    projector = GateSessionEventLedgerProjector(
        ledger_factory=lambda access: SQLiteEventLedgerV1(connection, access),
        access_resolver=lambda _session: _access(),
    )
    repository = SQLiteGateSessionRepository(connection, clock=clock)
    repository.bind_revision_event_sink(projector)
    current = _create(repository)
    if terminal_status == "expired":
        current = repository.transition(
            current.session_id,
            "prepared",
            expected_version=current.version,
            lease_seconds=600,
            retrieval_snapshot_id="retrieval_snapshot_expired",
            system_gate_evaluation_id="system_gate_evaluation_expired",
        )
        clock.advance(seconds=3_601)

    terminal = repository.transition(
        current.session_id,
        terminal_status,
        expected_version=current.version,
        terminal_reason=f"session {terminal_status}",
    )

    events = _stream(connection, current.session_id)
    assert events[-1].event_type == event_type
    rebuilt = reduce_gate_session_events(events)
    for field in fields(terminal):
        assert getattr(rebuilt, field.name) == getattr(terminal, field.name)
    assert gate_session_projection_sha256(rebuilt) == gate_session_projection_sha256(
        terminal
    )


def test_existing_projection_is_imported_before_first_native_transition() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    created = _create(repository)
    projector = GateSessionEventLedgerProjector(
        ledger_factory=lambda access: SQLiteEventLedgerV1(connection, access),
        access_resolver=lambda _session: _access(),
    )
    repository.bind_revision_event_sink(projector)

    prepared = repository.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=600,
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_evaluation_001",
    )

    events = _stream(connection, created.session_id)
    assert tuple(event.event_type for event in events) == (
        GATE_SESSION_BASELINE_IMPORTED,
        GATE_SESSION_PREPARED,
    )
    assert events[0].origin == "imported"
    assert events[0].source is not None
    assert events[0].source.evidence_quality == "legacy_partial"
    assert reduce_gate_session_events(events) == prepared


def test_event_append_and_projection_write_roll_back_together() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    created = _create(repository)
    projector = GateSessionEventLedgerProjector(
        ledger_factory=lambda access: SQLiteEventLedgerV1(connection, access),
        access_resolver=lambda _session: _access(),
    )

    class _RejectAfterAppend:
        def append_and_reduce(self, current, next_session):
            projector.append_and_reduce(current, next_session)
            raise RuntimeError("reject after event append")

    repository.bind_revision_event_sink(_RejectAfterAppend())
    with pytest.raises(RuntimeError, match="reject after event append"):
        repository.transition(
            created.session_id,
            "prepared",
            expected_version=created.version,
            lease_seconds=600,
            retrieval_snapshot_id="retrieval_snapshot_001",
            system_gate_evaluation_id="system_gate_evaluation_001",
        )

    assert repository.get(created.session_id) == created
    assert _stream(connection, created.session_id) == ()


def test_registry_access_resolver_uses_exact_entity_partition() -> None:
    registry = _registry(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    install_sqlite_v3_bundle(connection)
    repository = SQLiteGateSessionRepository(connection, clock=_Clock())
    created = _create(repository)

    access = RegistryGateSessionLedgerAccessResolver(lambda: registry)(created)

    assert access.partition.organization_id == "organization_001"
    assert access.partition.tenant_id == created.tenant_id
    assert access.partition.repository_id == created.repository_id
    assert access.partition.environment_id == "environment_001"
    assert access.principal_id == created.principal_id
    assert access.agent_client_id == created.agent_client_id
