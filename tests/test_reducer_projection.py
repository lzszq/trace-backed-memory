from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import json
import subprocess
import sys

import pytest

from trace_backed_memory.event_registry_v1 import (
    DEFAULT_EVENT_TYPE_REGISTRY,
    EventPayloadRegistration,
    EventPayloadUpcaster,
    EventTypeRegistry,
)
from trace_backed_memory.event_v1 import CanonicalEvent, loads_canonical_event
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
    build_ledger_page,
)
from trace_backed_memory.projection import (
    ProjectionRuntime,
    ProjectionRuntimeError,
)
from trace_backed_memory.projection_checkpoint import (
    InMemoryProjectionCheckpointStore,
    ProjectionCheckpointConflictError,
)
from trace_backed_memory.reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerDeterminismError,
    ReducerEvent,
    build_event_inventory_reducer,
    execute_reducer_step,
    initial_reducer_state,
)
from trace_backed_memory.reducer_registry import (
    ReducerRegistry,
    ReducerRegistryError,
)


_ROOT = Path(__file__).resolve().parents[1]
_TIMESTAMP = "2026-07-31T12:00:02.000000Z"


def _event() -> CanonicalEvent:
    return loads_canonical_event(
        (_ROOT / "examples" / "event_v1.example.json").read_text(
            encoding="utf-8"
        )
    )


def _access(event: CanonicalEvent) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            event.organization_id,
            event.tenant_id,
            event.repository_id,
            event.environment_id,
        ),
        principal_id="principal_projection_operator",
        agent_client_id="agent_client_projection_operator",
        actor_type="service",
        actor_id="service_projection_operator",
        authorization_decision_id="authorization_projection_operator",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


class _Ledger:
    def __init__(self, event: CanonicalEvent) -> None:
        self.event = event
        self.access_context = _access(event)

    def read_global(self, after_position: int = 0, limit: int = 100):
        events = (self.event,) if after_position < self.event.global_position else ()
        return build_ledger_page(
            read_kind="global",
            events=events,
            high_watermark_global_position=self.event.global_position,
            next_stream_version=None,
            next_global_position=(
                self.event.global_position if events else after_position
            ),
            has_more=False,
        )


def _registry(*reducers: FunctionalReducer) -> ReducerRegistry:
    registry = ReducerRegistry()
    for reducer in reducers:
        registry.register(reducer)
    return registry.seal()


def _runtime(
    reducer: FunctionalReducer,
    store: InMemoryProjectionCheckpointStore,
) -> ProjectionRuntime:
    event = _event()
    return ProjectionRuntime(
        _Ledger(event),
        _registry(reducer),
        store,
        event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
    )


def _rebuild(
    runtime: ProjectionRuntime,
    *,
    version: int = 1,
    resume: bool = False,
    generation: int = 1,
):
    event = _event()
    return runtime.rebuild(
        "canonical-event-inventory",
        version,
        partition_sha256=_access(event).partition.partition_sha256,
        owner="projection_operator",
        rebuild_generation=generation,
        page_size=10,
        checkpoint_interval=1,
        resume=resume,
        created_at=_TIMESTAMP,
    )


def _inventory_v2() -> FunctionalReducer:
    base = build_event_inventory_reducer()
    descriptor = ReducerDescriptor(
        reducer_id=base.descriptor.reducer_id,
        reducer_version=2,
        input_event_types=("*",),
        output_projection=base.descriptor.output_projection,
        output_schema_version=2,
        code_sha256="sha256:" + "2" * 64,
        configuration_sha256="sha256:" + "3" * 64,
        envelope_only=True,
    )

    def initial() -> Mapping[str, object]:
        return {
            "event_count": 0,
            "event_type_counts": {},
            "last_event_sha256": None,
            "last_global_position": 0,
            "schema_marker": 2,
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        counts = dict(state["event_type_counts"])
        event_type = reducer_event.source_event.event_type
        counts[event_type] = int(counts.get(event_type, 0)) + 1
        return {
            "event_count": int(state["event_count"]) + 1,
            "event_type_counts": counts,
            "last_event_sha256": reducer_event.source_event.event_sha256,
            "last_global_position": reducer_event.source_event.global_position,
            "schema_marker": 2,
        }

    return FunctionalReducer(descriptor, initial, transition)


def test_inventory_reducer_has_deterministic_canonical_output() -> None:
    reducer = build_event_inventory_reducer()
    initial = initial_reducer_state(reducer)
    first = execute_reducer_step(
        reducer,
        initial.state,
        ReducerEvent(_event(), None),
    )
    second = execute_reducer_step(
        reducer,
        initial.state,
        ReducerEvent(_event(), None),
    )

    assert first == second
    assert first.state == {
        "event_count": 1,
        "event_type_counts": {"tbm.memory.proposed": 1},
        "last_event_sha256": _event().event_sha256,
        "last_global_position": 1,
    }


def test_reducer_detects_nondeterministic_transition() -> None:
    calls = 0
    base = build_event_inventory_reducer()

    def transition(
        _state: Mapping[str, object],
        _event: ReducerEvent,
    ) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    reducer = FunctionalReducer(
        base.descriptor,
        lambda: {"calls": 0},
        transition,
    )

    with pytest.raises(ReducerDeterminismError) as raised:
        execute_reducer_step(
            reducer,
            {"calls": 0},
            ReducerEvent(_event(), None),
        )

    assert raised.value.code == "TBM_REDUCER_NONDETERMINISTIC_TRANSITION"


def test_full_rebuild_and_checkpoint_resume_are_exact() -> None:
    store = InMemoryProjectionCheckpointStore()
    runtime = _runtime(build_event_inventory_reducer(), store)

    rebuilt = _rebuild(runtime)
    resumed = _rebuild(runtime, resume=True, generation=2)

    assert rebuilt.status == "completed"
    assert rebuilt.processed_events == 1
    assert resumed.status == "completed"
    assert resumed.processed_events == 0
    assert resumed.resumed_from_build_id == rebuilt.checkpoint.build_id
    assert resumed.checkpoint.build_id == rebuilt.checkpoint.build_id


def test_shadow_compare_activate_swap_and_rollback() -> None:
    event = _event()
    store = InMemoryProjectionCheckpointStore()
    registry = _registry(build_event_inventory_reducer(), _inventory_v2())
    runtime = ProjectionRuntime(
        _Ledger(event),
        registry,
        store,
        event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
    )
    partition = _access(event).partition.partition_sha256
    v1 = runtime.rebuild(
        "canonical-event-inventory",
        1,
        partition_sha256=partition,
        owner="projection_operator",
        rebuild_generation=1,
        created_at=_TIMESTAMP,
    )
    v2 = runtime.rebuild(
        "canonical-event-inventory",
        2,
        partition_sha256=partition,
        owner="projection_operator",
        rebuild_generation=2,
        created_at=_TIMESTAMP,
    )
    first = runtime.activate(
        v1.checkpoint.build_id,
        owner="projection_operator",
        approved=True,
        expected_head_version=0,
        expected_current_build_id=None,
        created_at=_TIMESTAMP,
    )
    comparison = runtime.compare(v1.checkpoint.build_id, v2.checkpoint.build_id)
    second = runtime.activate(
        v2.checkpoint.build_id,
        owner="projection_operator",
        approved=True,
        expected_head_version=first.head_version,
        expected_current_build_id=v1.checkpoint.build_id,
        comparison=comparison,
        created_at=_TIMESTAMP,
    )
    rollback = runtime.rollback(
        v1.checkpoint.projection_name,
        partition,
        owner="projection_operator",
        expected_head_version=second.head_version,
        expected_current_build_id=v2.checkpoint.build_id,
        created_at=_TIMESTAMP,
    )

    assert comparison.equivalent is False
    assert comparison.differences
    assert second.target_build_id == v2.checkpoint.build_id
    assert rollback.operation == "rollback"
    assert rollback.target_build_id == v1.checkpoint.build_id
    assert store.current_activation(v1.checkpoint.projection_name, partition) == rollback


def test_poison_event_blocks_without_skipping_source_event() -> None:
    base = build_event_inventory_reducer()

    def fail(
        _state: Mapping[str, object],
        _event: ReducerEvent,
    ) -> Mapping[str, object]:
        raise RuntimeError("unbounded internal detail")

    reducer = FunctionalReducer(base.descriptor, base.initial_state_factory, fail)
    result = _rebuild(_runtime(reducer, InMemoryProjectionCheckpointStore()))

    assert result.status == "blocked"
    assert result.processed_events == 0
    assert result.blocked is not None
    assert result.blocked.event_id == _event().event_id
    assert result.blocked.error_code == "TBM_REDUCER_TRANSITION_FAILED"
    assert result.checkpoint.global_position == 0


def test_projection_requires_complete_classification_view() -> None:
    event = _event()
    ledger = _Ledger(event)
    ledger.access_context = LedgerAccessContext(
        partition=ledger.access_context.partition,
        principal_id="principal_projection_operator",
        agent_client_id="agent_client_projection_operator",
        actor_type="service",
        actor_id="service_projection_operator",
        authorization_decision_id="authorization_projection_operator",
        classification_filter=LedgerClassificationFilter(("internal",)),
    )
    runtime = ProjectionRuntime(
        ledger,
        _registry(build_event_inventory_reducer()),
        InMemoryProjectionCheckpointStore(),
    )

    with pytest.raises(ProjectionRuntimeError) as raised:
        _rebuild(runtime)

    assert raised.value.code == "TBM_PROJECTION_CLASSIFICATION_VIEW_INCOMPLETE"


def test_typed_projection_applies_declared_upcaster_before_reduction() -> None:
    event = _event()
    event_type = event.event_type
    properties: dict[str, object] = {
        "memory_revision_id": {"type": "string", "minLength": 1},
        "proposal_kind": {"type": "string", "minLength": 1},
        "scope_id": {"type": "string", "minLength": 1},
    }
    registry = EventTypeRegistry()
    registry.register(
        EventPayloadRegistration(
            event_type=event_type,
            event_version=1,
            event_kind="domain",
            payload_schema="tbm.memory.proposed.v1",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": list(properties),
                "properties": properties,
            },
        )
    )
    registry.register(
        EventPayloadRegistration(
            event_type=event_type,
            event_version=2,
            event_kind="domain",
            payload_schema="tbm.memory.proposed.v2",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": [*properties, "upcasted"],
                "properties": {
                    **properties,
                    "upcasted": {"type": "boolean"},
                },
            },
        )
    )
    registry.register_upcaster(
        EventPayloadUpcaster(
            event_type=event_type,
            from_version=1,
            to_version=2,
            upcaster_id="memory_proposed_v1_to_v2",
            producer_version="0.1.0",
            transform=lambda payload: {**dict(payload), "upcasted": True},
        )
    )
    registry.seal()
    descriptor = ReducerDescriptor(
        reducer_id="typed-memory-proposal",
        reducer_version=1,
        input_event_types=(event_type,),
        output_projection="typed-memory-proposal",
        output_schema_version=1,
        code_sha256="sha256:" + "4" * 64,
        configuration_sha256="sha256:" + "5" * 64,
        target_event_versions={event_type: 2},
    )

    def transition(
        _state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        assert reducer_event.typed_event is not None
        return {
            "target_version": reducer_event.typed_event.target_version,
            "upcasted": reducer_event.typed_event.payload["upcasted"],
            "upcasters": list(reducer_event.typed_event.applied_upcaster_ids),
        }

    reducer = FunctionalReducer(descriptor, lambda: {}, transition)
    runtime = ProjectionRuntime(
        _Ledger(event),
        _registry(reducer),
        InMemoryProjectionCheckpointStore(),
        event_registry=registry,
    )
    rebuilt = runtime.rebuild(
        descriptor.reducer_id,
        descriptor.reducer_version,
        partition_sha256=_access(event).partition.partition_sha256,
        owner="projection_operator",
        rebuild_generation=1,
        created_at=_TIMESTAMP,
    )

    assert rebuilt.status == "completed"
    assert rebuilt.checkpoint.state == {
        "target_version": 2,
        "upcasted": True,
        "upcasters": ("memory_proposed_v1_to_v2",),
    }


def test_reducer_registry_rejects_unknown_version_and_hash_mismatch() -> None:
    reducer = build_event_inventory_reducer()
    registry = _registry(reducer)

    with pytest.raises(ReducerRegistryError) as missing:
        registry.resolve(reducer.descriptor.reducer_id, 999)
    with pytest.raises(ReducerRegistryError) as code:
        registry.resolve(
            reducer.descriptor.reducer_id,
            reducer.descriptor.reducer_version,
            expected_code_sha256="sha256:" + "0" * 64,
        )
    with pytest.raises(ReducerRegistryError) as configuration:
        registry.resolve(
            reducer.descriptor.reducer_id,
            reducer.descriptor.reducer_version,
            expected_configuration_sha256="sha256:" + "0" * 64,
        )

    assert missing.value.code == "TBM_REDUCER_NOT_FOUND"
    assert code.value.code == "TBM_REDUCER_CODE_HASH_MISMATCH"
    assert (
        configuration.value.code
        == "TBM_REDUCER_CONFIGURATION_HASH_MISMATCH"
    )


def test_checkpoint_store_rejects_different_state_at_same_position() -> None:
    reducer = build_event_inventory_reducer()
    store = InMemoryProjectionCheckpointStore()
    runtime = _runtime(reducer, store)
    rebuilt = _rebuild(runtime)
    checkpoint = rebuilt.checkpoint
    conflicting = type(checkpoint)(
        reducer_descriptor=checkpoint.reducer_descriptor,
        partition_sha256=checkpoint.partition_sha256,
        global_position=checkpoint.global_position,
        event_high_watermark=checkpoint.event_high_watermark,
        state={"different": True},
        reducer_registry_sha256=checkpoint.reducer_registry_sha256,
        event_registry_sha256=checkpoint.event_registry_sha256,
        created_at=checkpoint.created_at,
        owner=checkpoint.owner,
        rebuild_generation=checkpoint.rebuild_generation,
    )

    with pytest.raises(ProjectionCheckpointConflictError) as raised:
        store.save_checkpoint(conflicting)

    assert raised.value.code == "TBM_PROJECTION_CHECKPOINT_CONFLICT"


def test_projection_determinism_golden_verifier() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/verify_projection_determinism.py"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "fixture_version": "tbm.reducer-determinism-fixture.v1",
        "projection_sha256": (
            "sha256:9a257f398b55db473403a66d17cafc01983baa50aeb68ca70d69783c0444e9d4"
        ),
        "status": "ok",
    }
