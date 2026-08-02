from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    build_canonical_event,
)
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import (
    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE,
    SQLiteEventLedgerV1,
)
from trace_backed_memory.trace_event_v1 import (
    TRACE_DIFF_OBSERVED,
    TRACE_EVENT_MAX_BATCH,
    TRACE_EVENT_TYPES,
    TRACE_FINAL_RESPONSE_RECORDED,
    TRACE_PERMISSION_RECORDED,
    TRACE_PRE_COMPACT,
    TRACE_SESSION_ENDED,
    TRACE_SESSION_STARTED,
    TRACE_STOPPED,
    TRACE_SUBAGENT_STARTED,
    TRACE_SUBAGENT_STOPPED,
    TRACE_TOOL_COMPLETED,
    TRACE_TOOL_STARTED,
    TRACE_USER_PROMPT_SUBMITTED,
    TraceEventDraft,
    TraceEventLineage,
    TraceEventV1Error,
    TracePermissionResult,
    TraceToolCorrelation,
    append_trace_event_batch,
    build_trace_event_batch,
    build_trace_event_registry,
    dumps_trace_event_payload_dispatch_schema,
    trace_event_stream_id,
    verify_trace_event,
    verify_trace_event_lineage,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
_UNSET = object()


def _access(*, repository_id: str = "repository_001") -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id=repository_id,
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="service_trace_event_adapter",
        authorization_decision_id="authorization_trace_event_append",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _root_lineage() -> TraceEventLineage:
    return TraceEventLineage(
        role="root",
        subagent_id=None,
        parent_trace_id=None,
        parent_event_id=None,
    )


def _artifact(*, suffix: str = "a") -> EventArtifactRef:
    digest = "sha256:" + suffix * 64
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + suffix * 64,
        content_sha256=digest,
        media_type="application/json",
        size_bytes=128,
        classification="confidential",
        retention_policy_id="retention_trace_artifact",
        encryption_key_id="trace_key_001",
        availability="available",
    )


def _tool(phase: str) -> TraceToolCorrelation:
    return TraceToolCorrelation(
        tool_call_id="tool_call_001",
        tool_name="shell_command",
        phase=phase,  # type: ignore[arg-type]
        invocation_sha256=DIGEST_A,
    )


def _permission(*, status: str = "allowed") -> TracePermissionResult:
    return TracePermissionResult(
        decision_id="permission_decision_001",
        permission="tool:execute",
        status=status,  # type: ignore[arg-type]
        reason_code="allowed" if status == "allowed" else "policy_denied",
        decided_at="2026-08-02T00:00:03Z",
        request_sha256=DIGEST_A,
        policy_sha256=DIGEST_B,
    )


def _draft(
    event_type: str,
    sequence: int,
    *,
    trace_id: str = "trace_001",
    run_id: str = "run_001",
    occurred_at: str | None = None,
    lineage: TraceEventLineage | None = None,
    related_subagent_id: str | None = None,
    artifact_refs: tuple[EventArtifactRef, ...] = (),
) -> TraceEventDraft:
    phase = {
        TRACE_TOOL_STARTED: "request",
        TRACE_PERMISSION_RECORDED: "permission",
        TRACE_TOOL_COMPLETED: "result",
    }.get(event_type)
    default_timestamp = (
        f"2026-08-02T00:{sequence // 60:02d}:{sequence % 60:02d}Z"
    )
    return TraceEventDraft(
        event_type=event_type,
        trace_id=trace_id,
        run_id=run_id,
        sequence=sequence,
        occurred_at=occurred_at or default_timestamp,
        artifact_refs=artifact_refs,
        tool=None if phase is None else _tool(phase),
        permission_result=(
            _permission() if event_type == TRACE_PERMISSION_RECORDED else None
        ),
        lineage=lineage or _root_lineage(),
        related_subagent_id=related_subagent_id,
        classification="confidential" if artifact_refs else "internal",
    )


def _all_drafts() -> tuple[TraceEventDraft, ...]:
    ordered = (
        TRACE_SESSION_STARTED,
        TRACE_USER_PROMPT_SUBMITTED,
        TRACE_TOOL_STARTED,
        TRACE_PERMISSION_RECORDED,
        TRACE_TOOL_COMPLETED,
        TRACE_SUBAGENT_STARTED,
        TRACE_SUBAGENT_STOPPED,
        TRACE_PRE_COMPACT,
        TRACE_STOPPED,
        TRACE_SESSION_ENDED,
        TRACE_DIFF_OBSERVED,
        TRACE_FINAL_RESPONSE_RECORDED,
    )
    return tuple(
        _draft(
            event_type,
            sequence,
            related_subagent_id=(
                "subagent_001"
                if event_type in {TRACE_SUBAGENT_STARTED, TRACE_SUBAGENT_STOPPED}
                else None
            ),
            artifact_refs=(
                (_artifact(),)
                if event_type == TRACE_FINAL_RESPONSE_RECORDED
                else ()
            ),
        )
        for sequence, event_type in enumerate(ordered, start=1)
    )


def _build(
    drafts: tuple[TraceEventDraft, ...],
    *,
    access: LedgerAccessContext | None = None,
    previous_event: CanonicalEvent | None = None,
    expected_stream_version: int = 0,
    next_global_position: int = 1,
    recorded_at: str = "2026-08-02T00:01:00Z",
):
    return build_trace_event_batch(
        drafts,
        access=access or _access(),
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        ":memory:", isolation_level=None, check_same_thread=False
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE).decode(
            "utf-8"
        )
    )
    return connection


def _rebuild_with_payload(
    event: CanonicalEvent,
    payload: dict[str, object],
    *,
    causation_id: str | None | object = _UNSET,
) -> CanonicalEvent:
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
        trusted_context=_access().event_trusted_context(),
        request_id=event.request_id,
        idempotency_key_sha256=event.idempotency_key_sha256,
        request_sha256=event.request_sha256,
        correlation_id=event.correlation_id,
        causation_id=(
            event.causation_id
            if causation_id is _UNSET
            else causation_id  # type: ignore[arg-type]
        ),
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        producer=event.producer,
        producer_version=event.producer_version,
        payload_schema=event.payload_schema,
        previous_stream_event_sha256=event.previous_stream_event_sha256,
        classification=event.classification,
        retention_policy_id=event.retention_policy_id,
        artifact_refs=event.artifact_refs,
        payload=payload,
    )


def test_trace_event_registry_schema_example_and_root_exports_are_exact():
    registry = build_trace_event_registry()
    assert registry.sealed
    assert tuple(
        item["event_type"] for item in registry.catalog()["event_types"]
    ) == TRACE_EVENT_TYPES
    schema_text = dumps_trace_event_payload_dispatch_schema()
    schema = json.loads(schema_text)
    Draft202012Validator.check_schema(schema)
    assert schema_text == dumps_trace_event_payload_dispatch_schema()
    assert (
        ROOT / "schemas" / "trace_event_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8") == schema_text
    assert json.loads(
        (
            ROOT
            / "examples"
            / "trace_event_type_registry_v1.example.json"
        ).read_text(encoding="utf-8")
    ) == registry.catalog()
    assert tbm.TraceEventDraft is TraceEventDraft
    assert tbm.TRACE_EVENT_TYPES == TRACE_EVENT_TYPES


def test_ordered_trace_events_bind_all_eight_protocol_requirements():
    drafts = _all_drafts()
    events, idempotency = _build(drafts)

    assert len(events) == len(drafts) == 12
    assert tuple(event.event_type for event in events) == tuple(
        draft.event_type for draft in drafts
    )
    assert tuple(event.stream_version for event in events) == tuple(
        range(1, 13)
    )
    assert tuple(event.global_position for event in events) == tuple(
        range(1, 13)
    )
    assert all(event.stream_id == trace_event_stream_id("trace_001") for event in events)
    assert events[0].previous_stream_event_sha256 is None
    assert all(
        event.previous_stream_event_sha256 == events[index - 1].event_sha256
        and event.causation_id == events[index - 1].event_id
        for index, event in enumerate(events[1:], start=1)
    )
    assert all(event.occurred_at == drafts[index].occurred_at for index, event in enumerate(events))
    assert events[2].payload["tool"]["tool_call_id"] == "tool_call_001"  # type: ignore[index]
    assert events[3].payload["permission_result"]["status"] == "allowed"  # type: ignore[index]
    assert events[4].payload["tool"]["invocation_sha256"] == DIGEST_A  # type: ignore[index]
    assert events[5].payload["related_subagent_id"] == "subagent_001"
    assert events[-1].payload["artifact_ids"] == (_artifact().artifact_id,)
    assert events[-1].artifact_refs == (_artifact(),)
    assert idempotency.idempotency_key_sha256.startswith("sha256:")
    for event in events:
        verify_trace_event(event)


@pytest.mark.parametrize(
    "status",
    ("allowed", "denied", "unknown"),
)
def test_permission_results_are_explicit_and_not_checked_is_null(status: str):
    permission = _permission(status=status)
    permission_event = replace(
        _draft(
            TRACE_PERMISSION_RECORDED,
            1,
            occurred_at="2026-08-02T00:00:04Z",
        ),
        permission_result=permission,
    )
    event = _build((permission_event,))[0][0]
    assert event.payload["permission_result"]["status"] == status  # type: ignore[index]
    assert event.payload["permission_result"]["decision_id"] == permission.decision_id  # type: ignore[index]

    not_checked = _build((_draft(TRACE_SESSION_STARTED, 1),))[0][0]
    assert not_checked.payload["permission_result"] is None


def test_permission_tool_and_timestamp_semantics_fail_closed():
    with pytest.raises(TraceEventV1Error, match="allowed permission status"):
        replace(_permission(), reason_code="policy_denied")
    with pytest.raises(TraceEventV1Error, match="only permission-recorded"):
        replace(
            _draft(TRACE_TOOL_STARTED, 1),
            permission_result=_permission(),
        )
    with pytest.raises(TraceEventV1Error, match="tool phase"):
        replace(_draft(TRACE_TOOL_COMPLETED, 1), tool=_tool("request"))
    with pytest.raises(TraceEventV1Error, match="canonical UTC"):
        _draft(
            TRACE_SESSION_STARTED,
            1,
            occurred_at="2026-08-02T08:00:01+08:00",
        )
    with pytest.raises(TraceEventV1Error, match="cannot follow"):
        replace(
            _draft(
                TRACE_PERMISSION_RECORDED,
                1,
                occurred_at="2026-08-02T00:00:04Z",
            ),
            occurred_at="2026-08-02T00:00:02Z",
        )


def test_artifact_descriptors_are_sorted_unique_and_exactly_bound():
    first = _artifact(suffix="a")
    second = _artifact(suffix="b")
    draft = _draft(
        TRACE_FINAL_RESPONSE_RECORDED,
        1,
        artifact_refs=(first, second),
    )
    event = _build((draft,))[0][0]
    verify_trace_event(event)

    with pytest.raises(TraceEventV1Error, match="sorted and unique"):
        replace(draft, artifact_refs=(second, first))
    with pytest.raises(TraceEventV1Error, match="sorted and unique"):
        replace(draft, artifact_refs=(first, first))

    tampered_payload = dict(event.payload)
    tampered_payload["artifact_ids"] = []
    tampered = _rebuild_with_payload(event, tampered_payload)
    with pytest.raises(TraceEventV1Error, match="artifact IDs"):
        verify_trace_event(tampered)


def test_subagent_lineage_requires_exact_parent_event_and_scope():
    parent_draft = _draft(
        TRACE_SUBAGENT_STARTED,
        1,
        trace_id="trace_parent",
        run_id="run_parent",
        related_subagent_id="subagent_001",
    )
    parent = _build((parent_draft,))[0][0]
    child_lineage = TraceEventLineage(
        role="subagent",
        subagent_id="subagent_001",
        parent_trace_id="trace_parent",
        parent_event_id=parent.event_id,
    )
    child_draft = _draft(
        TRACE_SESSION_STARTED,
        1,
        trace_id="trace_child",
        run_id="run_child",
        occurred_at="2026-08-02T00:00:02Z",
        lineage=child_lineage,
    )
    child = _build(
        (child_draft,),
        next_global_position=2,
    )[0][0]
    assert child.causation_id == parent.event_id
    verify_trace_event_lineage(child, parent)

    wrong_parent_trace = replace(
        child_draft,
        lineage=replace(child_lineage, parent_trace_id="trace_other"),
    )
    wrong_child = _build(
        (wrong_parent_trace,),
        next_global_position=2,
    )[0][0]
    with pytest.raises(TraceEventV1Error, match="exact parent event"):
        verify_trace_event_lineage(wrong_child, parent)

    wrong_causation = _rebuild_with_payload(
        child,
        dict(child.payload),
        causation_id="evt_wrong_parent",
    )
    with pytest.raises(TraceEventV1Error, match="exact parent event"):
        verify_trace_event_lineage(wrong_causation, parent)

    other_scope_child = _build(
        (child_draft,),
        access=_access(repository_id="repository_other"),
        next_global_position=2,
    )[0][0]
    with pytest.raises(TraceEventV1Error, match="exact parent event"):
        verify_trace_event_lineage(other_scope_child, parent)


def test_sequence_batch_bounds_parent_and_timestamp_rejections():
    with pytest.raises(TraceEventV1Error, match="bounded non-empty"):
        _build(())
    too_many = tuple(
        _draft(TRACE_SESSION_STARTED, index)
        for index in range(1, TRACE_EVENT_MAX_BATCH + 2)
    )
    with pytest.raises(TraceEventV1Error, match="bounded non-empty"):
        _build(too_many)
    with pytest.raises(TraceEventV1Error, match="contiguous"):
        _build((_draft(TRACE_SESSION_STARTED, 2),))
    with pytest.raises(TraceEventV1Error, match="one trace, run"):
        _build(
            (
                _draft(TRACE_SESSION_STARTED, 1),
                _draft(
                    TRACE_SESSION_ENDED,
                    2,
                    trace_id="trace_other",
                ),
            )
        )
    first = _build((_draft(TRACE_SESSION_STARTED, 1),))[0][0]
    with pytest.raises(TraceEventV1Error, match="cannot move backwards"):
        _build(
            (
                _draft(
                    TRACE_SESSION_ENDED,
                    2,
                    occurred_at="2026-08-02T00:00:00Z",
                ),
            ),
            previous_event=first,
            expected_stream_version=1,
            next_global_position=2,
        )
    other_head = _build(
        (
            _draft(
                TRACE_SESSION_STARTED,
                1,
                trace_id="trace_other",
            ),
        )
    )[0][0]
    with pytest.raises(TraceEventV1Error, match="stream head"):
        _build(
            (_draft(TRACE_SESSION_ENDED, 2),),
            previous_event=other_head,
            expected_stream_version=1,
            next_global_position=2,
        )


def test_sqlite_bounded_append_is_atomic_and_exactly_idempotent():
    access = _access()
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, access)
    drafts = (
        _draft(TRACE_SESSION_STARTED, 1),
        _draft(TRACE_SESSION_ENDED, 2),
    )

    connection.execute("BEGIN IMMEDIATE")
    rolled_back = append_trace_event_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:01:00Z",
    )
    assert rolled_back.current_stream_version == 2
    connection.rollback()
    assert ledger.read_stream(trace_event_stream_id("trace_001")).events == ()

    committed = append_trace_event_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:01:00Z",
    )
    replayed = append_trace_event_batch(
        ledger,
        drafts,
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:01:00Z",
    )
    assert replayed == committed
    assert replayed.outcome == "committed"
    assert ledger.read_stream(trace_event_stream_id("trace_001")).events == (
        committed.events
    )
