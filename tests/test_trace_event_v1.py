from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.event_v1 import (
    EVENT_MAX_VERSION,
    CanonicalEvent,
    EventArtifactRef,
    EventSource,
    EventTrustedContext,
    build_canonical_event,
)
from trace_backed_memory.ledger_port_v1 import (
    EVENT_LEDGER_MAX_APPEND_BATCH,
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.trace_event_v1 import (
    TRACE_EVENT_MAX_SEQUENCE,
    TRACE_EVENT_RECORDED,
    TraceEventRecordRef,
    TraceEventV1Error,
    append_trace_event_batch,
    build_trace_event,
    build_trace_event_batch,
    parse_trace_event,
    trace_event_id,
    verify_trace_event_batch,
    verify_trace_event_parent,
)
from tests.postgres_support import PostgresCluster


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_INSTALL = ROOT / "schemas" / "postgres-v3-event-ledger.sql"


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id="authorization_decision_001",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _source(sequence: int) -> EventSource:
    return EventSource(
        source_system="codex_app_server",
        source_record_id=f"hook_record_{sequence:03d}",
        evidence_quality="exact",
        observed_at="2026-08-03T00:00:01Z",
    )


def _artifact(
    character: str = "a",
    *,
    classification: str = "internal",
) -> EventArtifactRef:
    content_sha256 = "sha256:" + character * 64
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + character * 64,
        content_sha256=content_sha256,
        media_type="application/json",
        size_bytes=128,
        classification=classification,  # type: ignore[arg-type]
        retention_policy_id="retention_engineering_memory",
        encryption_key_id=(
            None
            if classification in {"public", "internal"}
            else "encryption_key_trace_001"
        ),
        availability="available",
    )


def _record(
    sequence: int,
    *,
    trace_id: str = "trace_ordered_001",
    run_id: str = "run_ordered_001",
    artifact_refs: tuple[EventArtifactRef, ...] = (),
    trace_event_type: str = "SessionStart",
    permission_result: str = "not_applicable",
    tool_correlation_id: str | None = None,
    parent_trace_id: str | None = None,
    subagent_id: str | None = None,
    causation_event_id: str | None = None,
    classification: str = "internal",
) -> TraceEventRecordRef:
    return TraceEventRecordRef(
        trace_id=trace_id,
        run_id=run_id,
        sequence=sequence,
        trace_event_type=trace_event_type,
        occurred_at="2026-08-03T00:00:01Z",
        authorization_event_id="authorization_decision_001",
        source=_source(sequence),
        artifact_refs=artifact_refs,
        classification=classification,  # type: ignore[arg-type]
        retention_policy_id="retention_engineering_memory",
        tool_correlation_id=tool_correlation_id,
        permission_result=permission_result,  # type: ignore[arg-type]
        parent_trace_id=parent_trace_id,
        subagent_id=subagent_id,
        causation_event_id=causation_event_id,
    )


def _batch() -> tuple[CanonicalEvent, ...]:
    records = (
        _record(1),
        _record(
            2,
            artifact_refs=(_artifact(),),
            trace_event_type="PreToolUse",
            permission_result="allowed",
            tool_correlation_id="tool_call_001",
        ),
        _record(
            3,
            trace_event_type="SubagentStart",
            parent_trace_id="trace_parent_001",
            subagent_id="subagent_001",
        ),
    )
    return build_trace_event_batch(
        records,
        parent_event=None,
        first_global_position=1,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T00:00:10Z",
    )


def _continued_batch(
    parent_event: CanonicalEvent,
) -> tuple[CanonicalEvent, ...]:
    records = (
        _record(
            4,
            trace_event_type="PostToolUse",
            tool_correlation_id="tool_call_001",
        ),
        _record(5, trace_event_type="SessionEnd"),
    )
    return build_trace_event_batch(
        records,
        parent_event=parent_event,
        first_global_position=4,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T00:00:20Z",
    )


def _clone_event(
    event: CanonicalEvent,
    *,
    payload: dict[str, object] | None = None,
    request_sha256: str | None = None,
) -> CanonicalEvent:
    trusted = EventTrustedContext(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
        principal_id=event.principal_id,
        agent_client_id=event.agent_client_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        authorization_decision_id=event.authorization_decision_id,
    )
    return build_canonical_event(
        event_id=event.event_id,
        event_type=event.event_type,
        event_version=event.event_version,
        event_kind=event.event_kind,
        origin=event.origin,
        source=event.source,
        stream_id=event.stream_id,
        stream_type=event.stream_type,
        stream_version=event.stream_version,
        global_position=event.global_position,
        trusted_context=trusted,
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=(
            event.request_sha256 if request_sha256 is None else request_sha256
        ),
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=dict(event.payload) if payload is None else payload,
    )


def test_trace_event_batch_round_trips_ordered_engineering_evidence() -> None:
    events = _batch()

    assert tuple(event.event_type for event in events) == (TRACE_EVENT_RECORDED,) * 3
    assert tuple(event.stream_version for event in events) == (1, 2, 3)
    assert len({event.idempotency_key_sha256 for event in events}) == 1
    assert len({event.request_sha256 for event in events}) == 1
    assert tuple(parse_trace_event(event).trace_event_type for event in events) == (
        "SessionStart",
        "PreToolUse",
        "SubagentStart",
    )
    tool = parse_trace_event(events[1])
    assert tool.tool_correlation_id == "tool_call_001"
    assert tool.permission_result == "allowed"
    assert tool.artifact_refs == (_artifact(),)
    subagent = parse_trace_event(events[2])
    assert subagent.parent_trace_id == "trace_parent_001"
    assert subagent.subagent_id == "subagent_001"
    verify_trace_event_batch(events, parent_event=None)

    typed = tuple(DEFAULT_EVENT_TYPE_REGISTRY.consume(event) for event in events)
    assert all(item.source_event.event_kind == "observation" for item in typed)


def test_trace_event_batch_continues_atomically_and_replays_exactly_in_sqlite() -> None:
    first_batch = _batch()
    second_batch = _continued_batch(first_batch[-1])
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        first = append_trace_event_batch(
            ledger,
            first_batch,
            parent_event=None,
        )
        first_replay = append_trace_event_batch(
            ledger,
            first_batch,
            parent_event=None,
        )
        second = append_trace_event_batch(
            ledger,
            second_batch,
            parent_event=first_batch[-1],
        )
        second_replay = append_trace_event_batch(
            ledger,
            second_batch,
            parent_event=first_batch[-1],
        )

        assert first.inserted is True
        assert first_replay.inserted is False
        assert first_replay.receipt == first.receipt
        assert second.inserted is True
        assert second_replay.inserted is False
        assert second_replay.receipt == second.receipt
        assert second.receipt.previous_stream_version == 3
        assert second.receipt.current_stream_version == 5
        assert second.receipt.first_global_position == 4
        assert second.receipt.last_global_position == 5
        retained = ledger.read_stream(first_batch[0].stream_id, limit=100).events
        assert retained == first_batch + second_batch
        verify_trace_event_batch(first_batch, parent_event=None)
        verify_trace_event_batch(second_batch, parent_event=first_batch[-1])
        assert ledger.verify_stream(first_batch[0].stream_id).valid


def test_trace_event_batch_matches_postgres_event_ledger(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    installed = postgres_cluster.run_script(POSTGRES_INSTALL)
    assert installed.returncode == 0, installed.stderr
    first_batch = _batch()
    second_batch = _continued_batch(first_batch[-1])
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as sqlite_ledger:
        sqlite_commits = (
            append_trace_event_batch(
                sqlite_ledger,
                first_batch,
                parent_event=None,
            ),
            append_trace_event_batch(
                sqlite_ledger,
                second_batch,
                parent_event=first_batch[-1],
            ),
        )
        sqlite_stream = sqlite_ledger.read_stream(
            first_batch[0].stream_id,
            limit=100,
        )
        sqlite_global = sqlite_ledger.read_global(limit=100)
    with PostgresEventLedgerV1.connect(
        _access(),
        **postgres_cluster.connection_kwargs(),
    ) as ledger:
        postgres_commits = (
            append_trace_event_batch(
                ledger,
                first_batch,
                parent_event=None,
            ),
            append_trace_event_batch(
                ledger,
                second_batch,
                parent_event=first_batch[-1],
            ),
        )
        replay = append_trace_event_batch(
            ledger,
            second_batch,
            parent_event=first_batch[-1],
        )

        assert postgres_commits == sqlite_commits
        assert replay.inserted is False
        assert replay.receipt == postgres_commits[-1].receipt
        assert ledger.read_stream(first_batch[0].stream_id, limit=100) == sqlite_stream
        assert ledger.read_global(limit=100) == sqlite_global


def test_trace_event_sensitive_content_remains_out_of_payload_metadata() -> None:
    artifact = _artifact("f", classification="restricted")
    event = build_trace_event(
        _record(
            1,
            artifact_refs=(artifact,),
            trace_event_type="PostToolUse",
            classification="restricted",
        ),
        parent_event=None,
        global_position=1,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T00:00:10Z",
    )

    payload_json = json.dumps(dict(event.payload), sort_keys=True)
    assert artifact.artifact_id in payload_json
    assert artifact.content_sha256 not in payload_json
    assert artifact.encryption_key_id not in payload_json
    assert event.artifact_refs == (artifact,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parent_trace_id", "trace_ordered_001"),
        ("occurred_at", "2026-08-03T00:00:01+00:00"),
        ("permission_result", "granted"),
        ("permission_result", ["allowed"]),
        ("classification", ["internal"]),
        ("causation_event_id", "evt_"),
        ("tool_correlation_id", "tool call 001"),
        ("subagent_id", "subagent 001"),
        ("source", object()),
        ("artifact_refs", (_artifact(), _artifact())),
        ("sequence", 0),
    ],
)
def test_trace_event_reference_rejects_invalid_identity_time_and_metadata(
    field: str,
    value: object,
) -> None:
    with pytest.raises(TraceEventV1Error):
        replace(_record(1), **{field: value})


def test_trace_event_rejects_wrong_authorization_parent_and_batch_bounds() -> None:
    access = _access()
    first = build_trace_event(
        _record(1),
        parent_event=None,
        global_position=1,
        trusted_context=access.event_trusted_context(),
        recorded_at="2026-08-03T00:00:10Z",
    )
    with pytest.raises(TraceEventV1Error):
        build_trace_event(
            _record(2, run_id="run_other_001"),
            parent_event=first,
            global_position=2,
            trusted_context=access.event_trusted_context(),
            recorded_at="2026-08-03T00:00:10Z",
        )
    wrong_trusted = replace(
        access.event_trusted_context(),
        authorization_decision_id="authorization_decision_other",
    )
    with pytest.raises(TraceEventV1Error):
        build_trace_event(
            _record(1),
            parent_event=None,
            global_position=1,
            trusted_context=wrong_trusted,
            recorded_at="2026-08-03T00:00:10Z",
        )
    over_limit = tuple(
        _record(sequence) for sequence in range(1, EVENT_LEDGER_MAX_APPEND_BATCH + 2)
    )
    with pytest.raises(TraceEventV1Error):
        build_trace_event_batch(
            over_limit,
            parent_event=None,
            first_global_position=1,
            trusted_context=access.event_trusted_context(),
            recorded_at="2026-08-03T00:02:00Z",
        )


def test_trace_event_batch_verifier_rejects_tampered_command_and_descriptor() -> None:
    events = _batch()
    tampered_command = (
        _clone_event(events[0], request_sha256="sha256:" + "f" * 64),
        *events[1:],
    )
    with pytest.raises(TraceEventV1Error):
        verify_trace_event_batch(tampered_command, parent_event=None)

    payload = dict(events[0].payload)
    payload["batch_size"] = 2
    tampered_descriptor = (_clone_event(events[0], payload=payload), *events[1:])
    with pytest.raises(TraceEventV1Error):
        parse_trace_event(tampered_descriptor[0])

    singleton = build_trace_event(
        _record(1),
        parent_event=None,
        global_position=1,
        trusted_context=_access().event_trusted_context(),
        recorded_at="2026-08-03T00:00:10Z",
    )
    tampered_singleton = _clone_event(
        singleton,
        request_sha256="sha256:" + "e" * 64,
    )
    with pytest.raises(TraceEventV1Error):
        parse_trace_event(tampered_singleton)

    overflow_payload = dict(singleton.payload)
    overflow_payload["batch_first_sequence"] = TRACE_EVENT_MAX_SEQUENCE
    overflow_payload["batch_size"] = 2
    with pytest.raises(TraceEventV1Error):
        parse_trace_event(_clone_event(singleton, payload=overflow_payload))


@pytest.mark.parametrize("first_global_position", [0, EVENT_MAX_VERSION])
def test_trace_event_batch_rejects_invalid_global_position_before_build(
    first_global_position: int,
) -> None:
    with pytest.raises(TraceEventV1Error):
        build_trace_event_batch(
            (_record(1), _record(2)),
            parent_event=None,
            first_global_position=first_global_position,
            trusted_context=_access().event_trusted_context(),
            recorded_at="2026-08-03T00:00:10Z",
        )


def test_trace_event_identity_is_partition_scoped() -> None:
    trusted = _access().event_trusted_context()
    other_partition = replace(trusted, tenant_id="tenant_002")

    assert trace_event_id("trace_ordered_001", 1, trusted) != trace_event_id(
        "trace_ordered_001",
        1,
        other_partition,
    )


def test_typed_trace_append_rejects_invalid_batch_and_context_before_persistence() -> (
    None
):
    events = _batch()
    with pytest.raises(TraceEventV1Error):
        append_trace_event_batch(
            object(),  # type: ignore[arg-type]
            events,
            parent_event=None,
        )

    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(TraceEventV1Error):
            append_trace_event_batch(
                ledger,
                events[:-1],
                parent_event=None,
            )
        assert ledger.read_global().events == ()

    wrong_access = replace(
        _access(),
        partition=replace(_access().partition, tenant_id="tenant_002"),
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        wrong_access,
        initialize=True,
    ) as ledger:
        with pytest.raises(TraceEventV1Error):
            append_trace_event_batch(
                ledger,
                events,
                parent_event=None,
            )
        assert ledger.read_global().events == ()


def test_trace_event_parent_and_public_exports_are_intentional() -> None:
    events = _batch()
    verify_trace_event_parent(events[0], None)
    verify_trace_event_parent(events[1], events[0])
    with pytest.raises(TraceEventV1Error):
        verify_trace_event_parent(events[1], None)

    assert tbm.TraceEventRecordRef is TraceEventRecordRef
    assert tbm.TRACE_EVENT_RECORDED == "tbm.trace.event_recorded"
    assert {
        "TraceEventRecordRef",
        "append_trace_event_batch",
        "build_trace_event_batch",
        "verify_trace_event_batch",
    } <= set(tbm.__all__)
