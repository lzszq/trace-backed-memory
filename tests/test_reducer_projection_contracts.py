from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from trace_backed_memory.event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from trace_backed_memory.ledger_port_v1 import (
    LedgerTenantPartition,
    build_ledger_page,
)
from trace_backed_memory.projection import ProjectionRuntime, ProjectionRuntimeError
from trace_backed_memory.projection_checkpoint import (
    InMemoryProjectionCheckpointStore,
    ProjectionActivation,
    ProjectionBlocked,
    ProjectionCheckpoint,
    ProjectionCheckpointConflictError,
    ProjectionCheckpointError,
    ProjectionCheckpointNotFoundError,
    parse_projection_activation,
    parse_projection_checkpoint,
)
from trace_backed_memory.reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerDeterminismError,
    ReducerEvent,
    ReducerExecutionError,
    ReducerV1Error,
    build_event_inventory_reducer,
    canonical_projection_state,
    initial_reducer_state,
    execute_reducer_step,
    parse_reducer_descriptor,
)
from trace_backed_memory.reducer_registry import (
    ReducerRegistry,
    ReducerRegistryError,
)
from tests.test_reducer_projection import (
    _Ledger,
    _access,
    _event,
    _inventory_v2,
    _registry,
)


_TIMESTAMP = "2026-08-01T00:10:00.000000Z"


def _rebuild(
    runtime: ProjectionRuntime,
    reducer_id: str = "canonical-event-inventory",
    reducer_version: int = 1,
    *,
    generation: int = 1,
    resume: bool = False,
):
    event = _event()
    return runtime.rebuild(
        reducer_id,
        reducer_version,
        partition_sha256=_access(event).partition.partition_sha256,
        owner="projection_operator",
        rebuild_generation=generation,
        checkpoint_interval=1,
        resume=resume,
        created_at=_TIMESTAMP,
    )


def _runtime(
    reducer: FunctionalReducer,
    store: InMemoryProjectionCheckpointStore,
    *,
    event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
) -> ProjectionRuntime:
    return ProjectionRuntime(
        _Ledger(_event()),
        _registry(reducer),
        store,
        event_registry=event_registry,
    )


def _clone_inventory(
    *,
    reducer_id: str = "canonical-event-inventory",
    code: str | None = None,
    configuration: str | None = None,
    output_projection: str = "canonical_event_inventory_v1",
) -> FunctionalReducer:
    base = build_event_inventory_reducer()
    descriptor = replace(
        base.descriptor,
        reducer_id=reducer_id,
        output_projection=output_projection,
        code_sha256=base.descriptor.code_sha256 if code is None else code,
        configuration_sha256=(
            base.descriptor.configuration_sha256
            if configuration is None
            else configuration
        ),
    )
    return FunctionalReducer(
        descriptor,
        base.initial_state_factory,
        base.transition,
    )


def test_reducer_checkpoint_and_activation_parsers_reject_digest_drift() -> None:
    reducer = build_event_inventory_reducer()
    assert parse_reducer_descriptor(reducer.descriptor.to_dict()) == reducer.descriptor

    invalid_descriptor = reducer.descriptor.to_dict()
    invalid_descriptor["descriptor_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ReducerV1Error) as descriptor_error:
        parse_reducer_descriptor(invalid_descriptor)
    assert descriptor_error.value.code == "TBM_REDUCER_DESCRIPTOR_DIGEST_MISMATCH"

    store = InMemoryProjectionCheckpointStore()
    runtime = _runtime(reducer, store)
    checkpoint = _rebuild(runtime).checkpoint
    assert parse_projection_checkpoint(checkpoint.to_dict()) == checkpoint

    invalid_checkpoint = checkpoint.to_dict()
    invalid_checkpoint["state_sha256"] = "sha256:" + "1" * 64
    with pytest.raises(ProjectionCheckpointError) as checkpoint_error:
        parse_projection_checkpoint(invalid_checkpoint)
    assert checkpoint_error.value.code == "TBM_PROJECTION_CHECKPOINT_DIGEST_MISMATCH"

    activation = runtime.activate(
        checkpoint.build_id,
        owner="projection_operator",
        approved=True,
        expected_head_version=0,
        expected_current_build_id=None,
        created_at=_TIMESTAMP,
    )
    assert parse_projection_activation(activation.to_dict()) == activation

    invalid_activation = activation.to_dict()
    invalid_activation["activation_sha256"] = "sha256:" + "2" * 64
    with pytest.raises(ProjectionCheckpointError) as activation_error:
        parse_projection_activation(invalid_activation)
    assert (
        activation_error.value.code
        == "TBM_PROJECTION_ACTIVATION_DIGEST_MISMATCH"
    )


@pytest.mark.parametrize(
    "value, expected_code",
    [
        (None, "TBM_REDUCER_DESCRIPTOR_INVALID"),
        ({}, "TBM_REDUCER_DESCRIPTOR_INVALID"),
        (None, "TBM_PROJECTION_CHECKPOINT_INVALID"),
        ({}, "TBM_PROJECTION_CHECKPOINT_INVALID"),
        (None, "TBM_PROJECTION_ACTIVATION_INVALID"),
        ({}, "TBM_PROJECTION_ACTIVATION_INVALID"),
    ],
)
def test_contract_parsers_reject_noncanonical_shapes(
    value: object,
    expected_code: str,
) -> None:
    if expected_code.startswith("TBM_REDUCER"):
        parser = parse_reducer_descriptor
    elif "CHECKPOINT" in expected_code:
        parser = parse_projection_checkpoint
    else:
        parser = parse_projection_activation
    with pytest.raises((ReducerV1Error, ProjectionCheckpointError)) as raised:
        parser(value)
    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "overrides",
    [
        {"deterministic": False},
        {"input_event_types": []},
        {"input_event_types": ()},
        {"input_event_types": ("tbm.memory.proposed",), "envelope_only": True},
        {"input_event_types": ("*",), "envelope_only": False},
        {"input_event_types": ("tbm.memory.verified", "tbm.memory.proposed")},
        {"input_event_types": ("tbm.memory.proposed", "tbm.memory.proposed")},
        {"input_event_types": ("tbm.memory.proposed",), "envelope_only": None},
        {
            "input_event_types": ("*",),
            "envelope_only": True,
            "target_event_versions": {"tbm.memory.proposed": 1},
        },
        {
            "input_event_types": ("tbm.memory.proposed",),
            "target_event_versions": {"tbm.memory.verified": 1},
        },
        {
            "input_event_types": ("tbm.memory.proposed",),
            "target_event_versions": [],
        },
    ],
)
def test_reducer_descriptor_rejects_ambiguous_inputs(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "reducer_id": "test-reducer",
        "reducer_version": 1,
        "input_event_types": ("tbm.a",),
        "output_projection": "test-projection",
        "output_schema_version": 1,
        "code_sha256": "sha256:" + "3" * 64,
        "configuration_sha256": "sha256:" + "4" * 64,
    }
    values.update(overrides)
    with pytest.raises(ReducerV1Error) as raised:
        ReducerDescriptor(**values)  # type: ignore[arg-type]
    assert raised.value.code == "TBM_REDUCER_DESCRIPTOR_INVALID"


@pytest.mark.parametrize(
    "state, expected_code",
    [
        (None, "TBM_REDUCER_STATE_INVALID"),
        ({"value": 2**63}, "TBM_REDUCER_STATE_INVALID"),
        ({"value": 0.5}, "TBM_REDUCER_STATE_INVALID"),
        ({"": True}, "TBM_REDUCER_STATE_INVALID"),
        ({1: "numeric", "value": "mixed"}, "TBM_REDUCER_STATE_INVALID"),
        ({"value": object()}, "TBM_REDUCER_STATE_INVALID"),
        ({"value": "x" * 1_048_576}, "TBM_REDUCER_STATE_LIMIT_EXCEEDED"),
    ],
)
def test_projection_state_rejects_nondeterministic_or_unbounded_values(
    state: object,
    expected_code: str,
) -> None:
    with pytest.raises(ReducerV1Error) as raised:
        canonical_projection_state(state)
    assert raised.value.code == expected_code


def test_initial_state_failures_are_sanitized_and_nondeterminism_is_detected() -> None:
    base = build_event_inventory_reducer()

    def fail() -> Mapping[str, object]:
        raise RuntimeError("sensitive internal failure")

    with pytest.raises(ReducerExecutionError) as failed:
        initial_reducer_state(
            FunctionalReducer(base.descriptor, fail, base.transition)
        )
    assert failed.value.code == "TBM_REDUCER_INITIAL_STATE_FAILED"

    calls = 0

    def changing() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    with pytest.raises(ReducerDeterminismError) as nondeterministic:
        initial_reducer_state(
            FunctionalReducer(base.descriptor, changing, base.transition)
        )
    assert (
        nondeterministic.value.code
        == "TBM_REDUCER_NONDETERMINISTIC_INITIAL_STATE"
    )

    with pytest.raises(ReducerV1Error) as invalid_state:
        initial_reducer_state(
            FunctionalReducer(base.descriptor, lambda: None, base.transition)  # type: ignore[arg-type]
        )
    assert invalid_state.value.code == "TBM_REDUCER_STATE_INVALID"


def test_reducer_event_and_execution_enforce_typed_payload_boundaries() -> None:
    event = _event()
    envelope = build_event_inventory_reducer()
    typed = DEFAULT_EVENT_TYPE_REGISTRY.consume(event)

    with pytest.raises(ReducerV1Error) as source:
        ReducerEvent(object(), None)  # type: ignore[arg-type]
    with pytest.raises(ReducerV1Error) as typed_shape:
        ReducerEvent(event, object())  # type: ignore[arg-type]
    with pytest.raises(ReducerV1Error) as wrong_event:
        execute_reducer_step(envelope, {}, object())  # type: ignore[arg-type]
    with pytest.raises(ReducerV1Error) as envelope_typed:
        execute_reducer_step(envelope, {}, ReducerEvent(event, typed))
    assert source.value.code == "TBM_REDUCER_EVENT_INVALID"
    assert typed_shape.value.code == "TBM_REDUCER_EVENT_INVALID"
    assert wrong_event.value.code == "TBM_REDUCER_EVENT_INVALID"
    assert envelope_typed.value.code == "TBM_REDUCER_EVENT_INVALID"

    descriptor = ReducerDescriptor(
        reducer_id="typed-boundary",
        reducer_version=1,
        input_event_types=(event.event_type,),
        output_projection="typed-boundary",
        output_schema_version=1,
        code_sha256="sha256:" + "b" * 64,
        configuration_sha256="sha256:" + "c" * 64,
    )
    reducer = FunctionalReducer(
        descriptor,
        lambda: {},
        lambda state, _event: state,
    )
    with pytest.raises(ReducerV1Error) as missing_typed:
        execute_reducer_step(reducer, {}, ReducerEvent(event, None))
    assert missing_typed.value.code == "TBM_REDUCER_TYPED_EVENT_REQUIRED"

    unrelated = replace(
        descriptor,
        input_event_types=("tbm.memory.verified",),
        target_event_versions={},
    )
    unchanged = execute_reducer_step(
        FunctionalReducer(unrelated, lambda: {}, lambda state, _event: state),
        {"retained": True},
        ReducerEvent(event, None),
    )
    assert unchanged.changed is False


def test_reducer_transition_failures_remain_sanitized() -> None:
    base = build_event_inventory_reducer()
    invalid = FunctionalReducer(
        base.descriptor,
        base.initial_state_factory,
        lambda _state, _event: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ReducerV1Error) as invalid_state:
        execute_reducer_step(invalid, {}, ReducerEvent(_event(), None))
    assert invalid_state.value.code == "TBM_REDUCER_STATE_INVALID"

    with pytest.raises(ReducerExecutionError) as malformed_inventory:
        execute_reducer_step(
            base,
            {
                "event_count": 0,
                "event_type_counts": None,
                "last_event_sha256": None,
                "last_global_position": 0,
            },
            ReducerEvent(_event(), None),
        )
    assert malformed_inventory.value.code == "TBM_REDUCER_TRANSITION_FAILED"


def test_projection_state_enforces_depth_limit() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(40):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(ReducerV1Error) as raised:
        canonical_projection_state(nested)
    assert raised.value.code == "TBM_REDUCER_STATE_LIMIT_EXCEEDED"


def test_reducer_registry_lifecycle_rejects_ambiguous_ownership() -> None:
    base = build_event_inventory_reducer()
    registry = ReducerRegistry()
    with pytest.raises(ReducerRegistryError) as unsealed:
        registry.resolve(base.descriptor.reducer_id)
    with pytest.raises(ReducerRegistryError) as empty:
        registry.seal()
    assert unsealed.value.code == "TBM_REDUCER_REGISTRY_UNSEALED"
    assert empty.value.code == "TBM_REDUCER_REGISTRY_EMPTY"

    registry.register(base)
    with pytest.raises(ReducerRegistryError) as duplicate:
        registry.register(base)
    conflicting = _clone_inventory(reducer_id="other-reducer")
    with pytest.raises(ReducerRegistryError) as projection_conflict:
        registry.register(conflicting)
    assert duplicate.value.code == "TBM_REDUCER_REGISTRY_DUPLICATE"
    assert (
        projection_conflict.value.code
        == "TBM_REDUCER_REGISTRY_PROJECTION_CONFLICT"
    )

    assert registry.seal().seal() is registry
    assert registry.resolve(base.descriptor.reducer_id) is base
    with pytest.raises(ReducerRegistryError) as invalid_lookup:
        registry.resolve(base.descriptor.reducer_id, 0)
    with pytest.raises(ReducerRegistryError) as sealed:
        registry.register(_clone_inventory(output_projection="sealed-output"))
    assert invalid_lookup.value.code == "TBM_REDUCER_REGISTRY_LOOKUP_INVALID"
    assert sealed.value.code == "TBM_REDUCER_REGISTRY_SEALED"


@pytest.mark.parametrize(
    "kind, expected_code",
    [
        ("code", "TBM_REDUCER_CODE_HASH_MISMATCH"),
        ("configuration", "TBM_REDUCER_CONFIGURATION_HASH_MISMATCH"),
        ("registry", "TBM_PROJECTION_CHECKPOINT_INCOMPATIBLE"),
    ],
)
def test_resume_rejects_changed_reducer_or_registry_identity(
    kind: str,
    expected_code: str,
) -> None:
    store = InMemoryProjectionCheckpointStore()
    _rebuild(_runtime(build_event_inventory_reducer(), store))
    if kind == "code":
        reducers = [_clone_inventory(code="sha256:" + "5" * 64)]
    elif kind == "configuration":
        reducers = [_clone_inventory(configuration="sha256:" + "6" * 64)]
    else:
        reducers = [
            build_event_inventory_reducer(),
            _clone_inventory(
                reducer_id="extra-inventory",
                output_projection="extra-inventory",
            ),
        ]
    runtime = ProjectionRuntime(
        _Ledger(_event()),
        _registry(*reducers),
        store,
        event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
    )

    with pytest.raises(ProjectionRuntimeError) as raised:
        _rebuild(runtime, generation=2, resume=True)
    assert raised.value.code == expected_code


def test_resume_rejects_a_checkpoint_from_another_reducer_version() -> None:
    event = _event()
    base = build_event_inventory_reducer()
    original_registry = _registry(base)
    retained_store = InMemoryProjectionCheckpointStore()
    retained = _rebuild(
        ProjectionRuntime(_Ledger(event), original_registry, retained_store)
    ).checkpoint

    class MismatchedCheckpointStore(InMemoryProjectionCheckpointStore):
        def load_latest_checkpoint(
            self,
            projection_name: str,
            reducer_version: int,
            partition_sha256: str,
        ) -> ProjectionCheckpoint | None:
            assert projection_name == retained.projection_name
            assert reducer_version == 2
            assert partition_sha256 == retained.partition_sha256
            return retained

    version_two_descriptor = replace(
        base.descriptor,
        reducer_version=2,
    )
    version_two = FunctionalReducer(
        version_two_descriptor,
        base.initial_state_factory,
        base.transition,
    )
    runtime = ProjectionRuntime(
        _Ledger(event),
        _registry(version_two),
        MismatchedCheckpointStore(),
    )

    with pytest.raises(ProjectionRuntimeError) as raised:
        _rebuild(runtime, reducer_version=2, generation=2, resume=True)
    assert raised.value.code == "TBM_REDUCER_VERSION_MISMATCH"


def test_typed_rebuild_blocks_when_declared_upcaster_is_unavailable() -> None:
    event = _event()
    descriptor = ReducerDescriptor(
        reducer_id="typed-memory-proposal-v2",
        reducer_version=1,
        input_event_types=(event.event_type,),
        output_projection="typed-memory-proposal-v2",
        output_schema_version=1,
        code_sha256="sha256:" + "7" * 64,
        configuration_sha256="sha256:" + "8" * 64,
        target_event_versions={event.event_type: 2},
    )
    reducer = FunctionalReducer(
        descriptor,
        lambda: {},
        lambda state, _event: state,
    )
    result = _rebuild(
        _runtime(reducer, InMemoryProjectionCheckpointStore()),
        descriptor.reducer_id,
    )

    assert result.status == "blocked"
    assert result.blocked is not None
    assert result.blocked.error_code == "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED"
    assert result.blocked.required_migration == "event-upcaster"


def test_typed_rebuild_requires_a_sealed_event_registry() -> None:
    event = _event()
    descriptor = ReducerDescriptor(
        reducer_id="typed-memory-proposal",
        reducer_version=1,
        input_event_types=(event.event_type,),
        output_projection="typed-memory-proposal",
        output_schema_version=1,
        code_sha256="sha256:" + "9" * 64,
        configuration_sha256="sha256:" + "a" * 64,
    )
    reducer = FunctionalReducer(
        descriptor,
        lambda: {},
        lambda state, _event: state,
    )
    with pytest.raises(ProjectionRuntimeError) as raised:
        _rebuild(
            _runtime(
                reducer,
                InMemoryProjectionCheckpointStore(),
                event_registry=None,
            ),
            descriptor.reducer_id,
        )
    assert raised.value.code == "TBM_PROJECTION_EVENT_REGISTRY_REQUIRED"


@pytest.mark.parametrize(
    "access_kind, expected_code",
    [
        ("missing", "TBM_PROJECTION_LEDGER_VIEW_UNVERIFIED"),
        ("invalid", "TBM_PROJECTION_LEDGER_VIEW_UNVERIFIED"),
        ("partition", "TBM_PROJECTION_PARTITION_MISMATCH"),
    ],
)
def test_projection_rebuild_verifies_ledger_access_identity(
    access_kind: str,
    expected_code: str,
) -> None:
    event = _event()
    ledger = _Ledger(event)
    if access_kind == "missing":
        del ledger.access_context
    elif access_kind == "invalid":
        ledger.access_context = object()
    else:
        access = ledger.access_context
        ledger.access_context = replace(
            access,
            partition=LedgerTenantPartition(
                access.partition.organization_id,
                "tenant_other",
                access.partition.repository_id,
                access.partition.environment_id,
            ),
        )
    runtime = ProjectionRuntime(
        ledger,
        _registry(build_event_inventory_reducer()),
        InMemoryProjectionCheckpointStore(),
    )
    with pytest.raises(ProjectionRuntimeError) as raised:
        _rebuild(runtime)
    assert raised.value.code == expected_code


def test_unverified_ledger_can_only_be_used_with_explicit_opt_out() -> None:
    ledger = _Ledger(_event())
    del ledger.access_context
    runtime = ProjectionRuntime(
        ledger,
        _registry(build_event_inventory_reducer()),
        InMemoryProjectionCheckpointStore(),
        require_complete_classification_view=False,
    )
    assert _rebuild(runtime).status == "completed"


def _shadow_runtime():
    event = _event()
    store = InMemoryProjectionCheckpointStore()
    other = _clone_inventory(
        reducer_id="other-inventory",
        output_projection="other-inventory",
    )
    runtime = ProjectionRuntime(
        _Ledger(event),
        _registry(
            build_event_inventory_reducer(),
            _inventory_v2(),
            other,
        ),
        store,
        event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
    )
    v1 = _rebuild(runtime, generation=1)
    v2 = _rebuild(runtime, reducer_version=2, generation=2)
    unrelated = _rebuild(
        runtime,
        reducer_id="other-inventory",
        generation=3,
    )
    return runtime, store, v1, v2, unrelated


def test_projection_activation_requires_approval_and_bound_comparison() -> None:
    runtime, _store, v1, v2, _unrelated = _shadow_runtime()
    with pytest.raises(ProjectionRuntimeError) as approval:
        runtime.activate(
            v1.checkpoint.build_id,
            owner="projection_operator",
            approved=False,
            expected_head_version=0,
            expected_current_build_id=None,
        )
    assert approval.value.code == "TBM_PROJECTION_APPROVAL_REQUIRED"

    first = runtime.activate(
        v1.checkpoint.build_id,
        owner="projection_operator",
        approved=True,
        expected_head_version=0,
        expected_current_build_id=None,
        created_at=_TIMESTAMP,
    )
    with pytest.raises(ProjectionRuntimeError) as missing:
        runtime.activate(
            v2.checkpoint.build_id,
            owner="projection_operator",
            approved=True,
            expected_head_version=first.head_version,
            expected_current_build_id=v1.checkpoint.build_id,
        )
    wrong_comparison = runtime.compare(
        v2.checkpoint.build_id,
        v1.checkpoint.build_id,
    )
    with pytest.raises(ProjectionRuntimeError) as mismatch:
        runtime.activate(
            v2.checkpoint.build_id,
            owner="projection_operator",
            approved=True,
            expected_head_version=first.head_version,
            expected_current_build_id=v1.checkpoint.build_id,
            comparison=wrong_comparison,
        )
    assert missing.value.code == "TBM_PROJECTION_COMPARISON_REQUIRED"
    assert mismatch.value.code == "TBM_PROJECTION_COMPARISON_MISMATCH"


def test_projection_rollback_rejects_stale_or_unapproved_targets() -> None:
    runtime, _store, v1, v2, unrelated = _shadow_runtime()
    first = runtime.activate(
        v1.checkpoint.build_id,
        owner="projection_operator",
        approved=True,
        expected_head_version=0,
        expected_current_build_id=None,
        created_at=_TIMESTAMP,
    )
    partition = v1.checkpoint.partition_sha256

    with pytest.raises(ProjectionRuntimeError) as stale:
        runtime.rollback(
            v1.checkpoint.projection_name,
            partition,
            owner="projection_operator",
            expected_head_version=0,
            expected_current_build_id=v1.checkpoint.build_id,
        )
    with pytest.raises(ProjectionRuntimeError) as predecessor:
        runtime.rollback(
            v1.checkpoint.projection_name,
            partition,
            owner="projection_operator",
            expected_head_version=first.head_version,
            expected_current_build_id=v1.checkpoint.build_id,
        )
    with pytest.raises(ProjectionRuntimeError) as scope:
        runtime.rollback(
            v1.checkpoint.projection_name,
            partition,
            owner="projection_operator",
            expected_head_version=first.head_version,
            expected_current_build_id=v1.checkpoint.build_id,
            target_build_id=unrelated.checkpoint.build_id,
        )
    with pytest.raises(ProjectionRuntimeError) as never_active:
        runtime.rollback(
            v1.checkpoint.projection_name,
            partition,
            owner="projection_operator",
            expected_head_version=first.head_version,
            expected_current_build_id=v1.checkpoint.build_id,
            target_build_id=v2.checkpoint.build_id,
        )

    assert stale.value.code == "TBM_PROJECTION_HEAD_CONFLICT"
    assert predecessor.value.code == "TBM_PROJECTION_ROLLBACK_UNAVAILABLE"
    assert scope.value.code == "TBM_PROJECTION_ROLLBACK_SCOPE_MISMATCH"
    assert never_active.value.code == "TBM_PROJECTION_ROLLBACK_UNAVAILABLE"


def test_projection_compare_rejects_scope_mismatch_and_self_compare_is_exact() -> None:
    runtime, _store, v1, _v2, unrelated = _shadow_runtime()
    with pytest.raises(ProjectionRuntimeError) as raised:
        runtime.compare(
            v1.checkpoint.build_id,
            unrelated.checkpoint.build_id,
        )
    assert raised.value.code == "TBM_PROJECTION_COMPARISON_SCOPE_MISMATCH"

    comparison = runtime.compare(
        v1.checkpoint.build_id,
        v1.checkpoint.build_id,
    )
    assert comparison.equivalent is True
    assert comparison.differences == ()
    assert comparison.truncated is False


def test_in_memory_projection_store_has_bounded_exact_semantics() -> None:
    store = InMemoryProjectionCheckpointStore()
    runtime = _runtime(build_event_inventory_reducer(), store)
    checkpoint = _rebuild(runtime).checkpoint
    assert store.save_checkpoint(checkpoint) is checkpoint
    assert store.list_checkpoints() == (checkpoint,)
    assert store.list_checkpoints(
        checkpoint.projection_name,
        checkpoint.partition_sha256,
    ) == (checkpoint,)
    assert store.load_latest_checkpoint(
        "missing-projection",
        1,
        checkpoint.partition_sha256,
    ) is None
    assert store.current_activation(
        checkpoint.projection_name,
        checkpoint.partition_sha256,
    ) is None
    assert store.activation_history(
        checkpoint.projection_name,
        checkpoint.partition_sha256,
    ) == ()

    with pytest.raises(ProjectionCheckpointNotFoundError) as missing:
        store.load_checkpoint("sha256:" + "f" * 64)
    assert missing.value.code == "TBM_PROJECTION_NOT_FOUND"

    orphan = ProjectionActivation(
        projection_name=checkpoint.projection_name,
        partition_sha256=checkpoint.partition_sha256,
        head_version=1,
        target_build_id="sha256:" + "e" * 64,
        previous_build_id=None,
        operation="activate",
        owner="projection_operator",
        created_at=_TIMESTAMP,
    )
    with pytest.raises(ProjectionCheckpointNotFoundError) as orphaned:
        store.append_activation(
            orphan,
            expected_head_version=0,
            expected_current_build_id=None,
        )
    assert orphaned.value.code == "TBM_PROJECTION_NOT_FOUND"


def test_projection_checkpoint_records_reject_invalid_lifecycle_fields() -> None:
    reducer = build_event_inventory_reducer()
    registry = _registry(reducer)
    values = {
        "reducer_descriptor": reducer.descriptor,
        "partition_sha256": _access(_event()).partition.partition_sha256,
        "global_position": 1,
        "event_high_watermark": 1,
        "state": {},
        "reducer_registry_sha256": registry.registry_sha256,
        "event_registry_sha256": None,
        "created_at": _TIMESTAMP,
        "owner": "projection_operator",
        "rebuild_generation": 1,
    }
    for overrides in (
        {"reducer_descriptor": object()},
        {"global_position": 2},
        {"rebuild_generation": 0},
    ):
        with pytest.raises(ProjectionCheckpointError):
            ProjectionCheckpoint(**{**values, **overrides})  # type: ignore[arg-type]

    with pytest.raises(ProjectionCheckpointError) as retryable:
        ProjectionBlocked(
            event_id="event_001",
            event_sha256="sha256:" + "1" * 64,
            payload_sha256="sha256:" + "2" * 64,
            reducer_id=reducer.descriptor.reducer_id,
            reducer_version=1,
            projection_name=reducer.descriptor.output_projection,
            last_good_position=0,
            error_code="TBM_TEST_BLOCKED",
            retryable=1,  # type: ignore[arg-type]
            required_migration=None,
        )
    assert retryable.value.code == "TBM_PROJECTION_BLOCKED_INVALID"

    with pytest.raises(ProjectionCheckpointError) as operation:
        ProjectionActivation(
            projection_name=reducer.descriptor.output_projection,
            partition_sha256=values["partition_sha256"],  # type: ignore[arg-type]
            head_version=1,
            target_build_id="sha256:" + "3" * 64,
            previous_build_id=None,
            operation="replace",  # type: ignore[arg-type]
            owner="projection_operator",
            created_at=_TIMESTAMP,
        )
    assert operation.value.code == "TBM_PROJECTION_ACTIVATION_INVALID"


def test_projection_store_activation_cas_rejects_stale_and_invalid_heads() -> None:
    store = InMemoryProjectionCheckpointStore()
    checkpoint = _rebuild(
        _runtime(build_event_inventory_reducer(), store)
    ).checkpoint
    activation = ProjectionActivation(
        projection_name=checkpoint.projection_name,
        partition_sha256=checkpoint.partition_sha256,
        head_version=1,
        target_build_id=checkpoint.build_id,
        previous_build_id=None,
        operation="activate",
        owner="projection_operator",
        created_at=_TIMESTAMP,
    )
    with pytest.raises(ProjectionCheckpointError) as invalid_type:
        store.save_checkpoint(object())  # type: ignore[arg-type]
    with pytest.raises(ProjectionCheckpointError) as activation_type:
        store.append_activation(  # type: ignore[arg-type]
            object(),
            expected_head_version=0,
            expected_current_build_id=None,
        )
    with pytest.raises(ProjectionCheckpointConflictError) as stale:
        store.append_activation(
            activation,
            expected_head_version=1,
            expected_current_build_id=None,
        )
    assert invalid_type.value.code == "TBM_PROJECTION_CHECKPOINT_INVALID"
    assert activation_type.value.code == "TBM_PROJECTION_ACTIVATION_INVALID"
    assert stale.value.code == "TBM_PROJECTION_HEAD_CONFLICT"

    store.append_activation(
        activation,
        expected_head_version=0,
        expected_current_build_id=None,
    )
    invalid_advance = ProjectionActivation(
        projection_name=checkpoint.projection_name,
        partition_sha256=checkpoint.partition_sha256,
        head_version=3,
        target_build_id=checkpoint.build_id,
        previous_build_id=checkpoint.build_id,
        operation="activate",
        owner="projection_operator",
        created_at=_TIMESTAMP,
    )
    with pytest.raises(ProjectionCheckpointError) as invalid_head:
        store.append_activation(
            invalid_advance,
            expected_head_version=1,
            expected_current_build_id=checkpoint.build_id,
        )
    assert invalid_head.value.code == "TBM_PROJECTION_ACTIVATION_INVALID"


def test_projection_resume_rejects_checkpoint_ahead_of_ledger() -> None:
    event = _event()
    reducer = build_event_inventory_reducer()
    registry = _registry(reducer)
    store = InMemoryProjectionCheckpointStore()
    checkpoint = ProjectionCheckpoint(
        reducer_descriptor=reducer.descriptor,
        partition_sha256=_access(event).partition.partition_sha256,
        global_position=2,
        event_high_watermark=2,
        state={"event_count": 0},
        reducer_registry_sha256=registry.registry_sha256,
        event_registry_sha256=None,
        created_at=_TIMESTAMP,
        owner="projection_operator",
        rebuild_generation=1,
    )
    store.save_checkpoint(checkpoint)
    runtime = ProjectionRuntime(_Ledger(event), registry, store)

    with pytest.raises(ProjectionRuntimeError) as raised:
        _rebuild(runtime, generation=2, resume=True)
    assert raised.value.code == "TBM_PROJECTION_CHECKPOINT_AHEAD"


def test_projection_rebuild_reads_bounded_followup_pages() -> None:
    event = _event()

    class PagedLedger:
        def __init__(self) -> None:
            self.access_context = _access(event)
            self.cursors: list[int] = []

        def read_global(self, after_position: int = 0, limit: int = 100):
            self.cursors.append(after_position)
            if after_position == 0:
                return build_ledger_page(
                    read_kind="global",
                    events=(event,),
                    high_watermark_global_position=2,
                    next_stream_version=None,
                    next_global_position=1,
                    has_more=True,
                )
            return build_ledger_page(
                read_kind="global",
                events=(),
                high_watermark_global_position=2,
                next_stream_version=None,
                next_global_position=after_position,
                has_more=False,
            )

    ledger = PagedLedger()
    runtime = ProjectionRuntime(
        ledger,
        _registry(build_event_inventory_reducer()),
        InMemoryProjectionCheckpointStore(),
    )
    result = _rebuild(runtime)
    assert result.status == "completed"
    assert result.processed_events == 1
    assert ledger.cursors == [0, 1]


def test_projection_comparison_reports_structural_differences_and_truncation() -> None:
    event = _event()
    reducer = build_event_inventory_reducer()
    registry = _registry(reducer)
    partition = _access(event).partition.partition_sha256
    store = InMemoryProjectionCheckpointStore()

    def checkpoint(position: int, state: Mapping[str, object]) -> ProjectionCheckpoint:
        return ProjectionCheckpoint(
            reducer_descriptor=reducer.descriptor,
            partition_sha256=partition,
            global_position=position,
            event_high_watermark=2,
            state=state,
            reducer_registry_sha256=registry.registry_sha256,
            event_registry_sha256=None,
            created_at=_TIMESTAMP,
            owner="projection_operator",
            rebuild_generation=position + 1,
        )

    active = checkpoint(
        0,
        {
            "changed": 1,
            "list": [1],
            "nested": {"value": 1},
            "removed": True,
            "typed": 1,
        },
    )
    shadow = checkpoint(
        1,
        {
            "added": True,
            "changed": 2,
            "list": [2],
            "nested": {"value": 2},
            "typed": "1",
        },
    )
    many = checkpoint(2, {f"key_{index:03d}": index for index in range(300)})
    for item in (active, shadow, many):
        store.save_checkpoint(item)
    runtime = ProjectionRuntime(_Ledger(event), registry, store)

    comparison = runtime.compare(active.build_id, shadow.build_id)
    assert {difference.kind for difference in comparison.differences} == {
        "added",
        "changed",
        "removed",
        "type_changed",
    }
    truncated = runtime.compare(active.build_id, many.build_id)
    assert truncated.truncated is True
    assert len(truncated.differences) == 256
