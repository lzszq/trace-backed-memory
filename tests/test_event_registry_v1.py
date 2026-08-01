from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
import trace_backed_memory as tbm
import trace_backed_memory.event_registry_v1 as registry_module

from trace_backed_memory.event_registry_v1 import (
    DEFAULT_EVENT_TYPE_REGISTRY,
    EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS,
    EVENT_REGISTRY_MAX_TYPES,
    EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE,
    EVENT_REGISTRY_PROTOCOL_VERSION,
    EventPayloadRegistration,
    EventPayloadUpcaster,
    EventRegistryV1Error,
    EventTypeRegistry,
    TypedEventView,
    UnknownEventTypeError,
    dumps_event_payload_dispatch_schema,
    dumps_event_registry_catalog,
    validate_registered_event_payload,
)
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
)


ROOT = Path(__file__).resolve().parents[1]


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _event(
    *,
    event_type: str = "tbm.memory.proposed",
    event_version: int = 1,
    payload_schema: str = "tbm.memory.proposed.v1",
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
    return build_canonical_event(
        event_id=f"evt_registry_{event_version}_{event_type.replace('.', '_')}",
        event_type=event_type,
        event_version=event_version,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id="registry_stream_001",
        stream_type="registry_test",
        stream_version=1,
        global_position=1,
        trusted_context=EventTrustedContext(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_local",
            principal_id="principal_001",
            agent_client_id="agent_client_001",
            actor_type="principal",
            actor_id="principal_001",
            authorization_decision_id="authorization_decision_001",
        ),
        request_id="request_001",
        idempotency_key_sha256=_digest("a"),
        request_sha256=_digest("b"),
        correlation_id="correlation_001",
        causation_id=None,
        occurred_at="2026-08-01T00:00:00Z",
        recorded_at="2026-08-01T00:00:01Z",
        producer="trace_backed_memory",
        producer_version="0.1.0",
        payload_schema=payload_schema,
        previous_stream_event_sha256=None,
        classification="internal",
        retention_policy_id="retention_engineering_memory",
        artifact_refs=(),
        payload=(
            {
                "memory_revision_id": "memory_revision_001",
                "proposal_kind": "lesson",
                "scope_id": "repository_001",
            }
            if payload is None
            else payload
        ),
    )


def _schema(version: int) -> dict[str, object]:
    properties: dict[str, object] = {
        "name": {"type": "string", "minLength": 1, "maxLength": 32}
    }
    required = ["name"]
    if version >= 2:
        properties["count"] = {"type": "integer", "minimum": 0}
        required.append("count")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _value_registration(child_schema: object) -> EventPayloadRegistration:
    return EventPayloadRegistration(
        event_type="tbm.test.value",
        event_version=1,
        event_kind="domain",
        payload_schema="tbm.test.value.v1",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": child_schema},
        },
    )


def _versioned_registry(*, with_upcaster: bool = True) -> EventTypeRegistry:
    registry = EventTypeRegistry()
    for version in (1, 2):
        registry.register(
            EventPayloadRegistration(
                event_type="tbm.test.changed",
                event_version=version,
                event_kind="domain",
                payload_schema=f"tbm.test.changed.v{version}",
                schema=_schema(version),
            )
        )
    if with_upcaster:
        registry.register_upcaster(
            EventPayloadUpcaster(
                event_type="tbm.test.changed",
                from_version=1,
                to_version=2,
                upcaster_id="test_changed_v1_to_v2",
                producer_version="0.1.0",
                transform=lambda payload: {
                    **dict(payload),
                    "count": 0,
                },
            )
        )
    return registry.seal()


def test_default_registry_catalog_and_generated_schema_are_exact_resources() -> None:
    catalog = json.loads(dumps_event_registry_catalog(DEFAULT_EVENT_TYPE_REGISTRY))
    example = json.loads(
        (ROOT / "examples" / "event_type_registry_v1.example.json").read_text(
            encoding="utf-8"
        )
    )
    generated_schema = dumps_event_payload_dispatch_schema(DEFAULT_EVENT_TYPE_REGISTRY)

    assert catalog == example
    assert catalog["registry_version"] == EVENT_REGISTRY_PROTOCOL_VERSION
    assert generated_schema == (
        ROOT / "schemas" / "event_payload_registry_v1.schema.json"
    ).read_text(encoding="utf-8")


def test_runtime_registry_limits_match_catalog_schema_limits() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "event_type_registry_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS == (
        EVENT_REGISTRY_MAX_TYPES * EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE**2
    )
    assert schema["properties"]["event_types"]["maxItems"] == (
        EVENT_REGISTRY_MAX_TYPES * EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE
    )
    assert schema["properties"]["compatibility"]["maxItems"] == (
        EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS
    )


def test_event_registry_contract_is_intentionally_exported() -> None:
    assert tbm.EVENT_REGISTRY_PROTOCOL_VERSION == "tbm.event-registry.v1"
    assert tbm.DEFAULT_EVENT_TYPE_REGISTRY is DEFAULT_EVENT_TYPE_REGISTRY
    assert {
        "EventTypeRegistry",
        "EventPayloadRegistration",
        "EventPayloadUpcaster",
        "UnknownEventTypeError",
    } <= set(tbm.__all__)


def test_known_event_is_typed_without_mutating_canonical_event() -> None:
    event = _event()
    before = event.to_dict()

    resolution = DEFAULT_EVENT_TYPE_REGISTRY.inspect(event)
    typed = DEFAULT_EVENT_TYPE_REGISTRY.consume(event)

    assert resolution.status == "known"
    assert resolution.consumable is True
    assert typed.source_event is event
    assert typed.source_version == typed.target_version == 1
    assert typed.applied_upcaster_ids == ()
    assert isinstance(typed.payload, MappingProxyType)
    assert event.to_dict() == before


@pytest.mark.parametrize(
    ("event_type", "event_version", "status"),
    [
        ("tbm.future.unknown", 1, "unknown_type"),
        ("tbm.memory.proposed", 99, "unknown_version"),
    ],
)
def test_unknown_event_is_preserved_but_cannot_be_silently_consumed(
    event_type: str, event_version: int, status: str
) -> None:
    event = _event(
        event_type=event_type,
        event_version=event_version,
        payload_schema=f"{event_type}.v{event_version}",
        payload={"future": "preserved"},
    )

    resolution = DEFAULT_EVENT_TYPE_REGISTRY.inspect(event)

    assert resolution.event is event
    assert resolution.status == status
    assert resolution.registration is None
    assert resolution.consumable is False
    with pytest.raises(UnknownEventTypeError) as error:
        DEFAULT_EVENT_TYPE_REGISTRY.consume(event)
    assert error.value.event is event
    assert error.value.status == status
    assert error.value.code == "TBM_EVENT_REGISTRY_UNKNOWN_EVENT"


def test_duplicate_type_version_and_schema_names_fail_closed() -> None:
    registration = EventPayloadRegistration(
        event_type="tbm.test.created",
        event_version=1,
        event_kind="domain",
        payload_schema="tbm.test.created.v1",
        schema=_schema(1),
    )
    registry = EventTypeRegistry()
    registry.register(registration)
    with pytest.raises(EventRegistryV1Error) as duplicate:
        registry.register(registration)
    assert duplicate.value.code == "TBM_EVENT_REGISTRY_DUPLICATE_TYPE_VERSION"

    with pytest.raises(EventRegistryV1Error) as duplicate_schema:
        registry.register(
            EventPayloadRegistration(
                event_type="tbm.test.renamed",
                event_version=1,
                event_kind="domain",
                payload_schema="tbm.test.created.v1",
                schema=_schema(1),
            )
        )
    assert duplicate_schema.value.code == "TBM_EVENT_REGISTRY_DUPLICATE_SCHEMA"


def test_upcaster_chain_is_explicit_revalidated_and_reported() -> None:
    registry = _versioned_registry()
    event = _event(
        event_type="tbm.test.changed",
        payload_schema="tbm.test.changed.v1",
        payload={"name": "before"},
    )

    typed = registry.consume(event, target_version=2)
    matrix = [row.to_dict() for row in registry.compatibility_matrix()]

    assert typed.target_version == 2
    assert dict(typed.payload) == {"name": "before", "count": 0}
    assert typed.applied_upcaster_ids == ("test_changed_v1_to_v2",)
    assert {
        "event_type": "tbm.test.changed",
        "source_version": 1,
        "target_version": 2,
        "compatibility": "upcast",
    } in matrix
    assert {
        "event_type": "tbm.test.changed",
        "source_version": 2,
        "target_version": 1,
        "compatibility": "unsupported",
    } in matrix


def test_missing_duplicate_or_failing_upcaster_is_never_silent() -> None:
    no_edge = _versioned_registry(with_upcaster=False)
    event = _event(
        event_type="tbm.test.changed",
        payload_schema="tbm.test.changed.v1",
        payload={"name": "before"},
    )
    with pytest.raises(EventRegistryV1Error) as missing:
        no_edge.consume(event, target_version=2)
    assert missing.value.code == "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED"

    registry = EventTypeRegistry()
    for version in (1, 2):
        registry.register(
            EventPayloadRegistration(
                event_type="tbm.test.changed",
                event_version=version,
                event_kind="domain",
                payload_schema=f"tbm.test.changed.v{version}",
                schema=_schema(version),
            )
        )
    edge = EventPayloadUpcaster(
        event_type="tbm.test.changed",
        from_version=1,
        to_version=2,
        upcaster_id="failing_edge",
        producer_version="0.1.0",
        transform=lambda _payload: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    registry.register_upcaster(edge)
    with pytest.raises(EventRegistryV1Error) as duplicate:
        registry.register_upcaster(edge)
    assert duplicate.value.code == "TBM_EVENT_REGISTRY_DUPLICATE_UPCASTER"
    registry.seal()
    with pytest.raises(EventRegistryV1Error) as failed:
        registry.consume(event, target_version=2)
    assert failed.value.code == "TBM_EVENT_REGISTRY_UPCAST_FAILED"
    assert "secret" not in str(failed.value)


def test_upcaster_ids_are_unique_across_registered_edges() -> None:
    registry = EventTypeRegistry()
    for version in (1, 2, 3):
        registry.register(
            EventPayloadRegistration(
                event_type="tbm.test.changed",
                event_version=version,
                event_kind="domain",
                payload_schema=f"tbm.test.changed.v{version}",
                schema=_schema(version),
            )
        )
    registry.register_upcaster(
        EventPayloadUpcaster(
            event_type="tbm.test.changed",
            from_version=1,
            to_version=2,
            upcaster_id="shared_upcaster_id",
            producer_version="0.1.0",
            transform=lambda payload: payload,
        )
    )
    with pytest.raises(EventRegistryV1Error) as duplicate:
        registry.register_upcaster(
            EventPayloadUpcaster(
                event_type="tbm.test.changed",
                from_version=2,
                to_version=3,
                upcaster_id="shared_upcaster_id",
                producer_version="0.1.0",
                transform=lambda payload: payload,
            )
        )
    assert duplicate.value.code == "TBM_EVENT_REGISTRY_DUPLICATE_UPCASTER"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"memory_revision_id": "revision", "proposal_kind": "lesson"},
        {
            "memory_revision_id": "revision",
            "proposal_kind": "unknown",
            "scope_id": "repository",
        },
        {
            "memory_revision_id": "revision",
            "proposal_kind": "lesson",
            "scope_id": "repository",
            "unexpected": True,
        },
    ],
)
def test_registered_payload_validation_is_strict(payload: dict[str, object]) -> None:
    event = _event(payload=payload)
    with pytest.raises(EventRegistryV1Error) as error:
        DEFAULT_EVENT_TYPE_REGISTRY.consume(event)
    assert error.value.code == "TBM_EVENT_REGISTRY_PAYLOAD_INVALID"


def test_payload_schema_name_and_event_kind_must_match_registration() -> None:
    event = _event(payload_schema="tbm.memory.alternate.v1")
    with pytest.raises(EventRegistryV1Error, match="payload_schema"):
        DEFAULT_EVENT_TYPE_REGISTRY.consume(event)

    event = _event()
    registration = DEFAULT_EVENT_TYPE_REGISTRY.inspect(event).registration
    assert registration is not None
    observation_registry = EventTypeRegistry()
    observation_registry.register(
        EventPayloadRegistration(
            event_type=registration.event_type,
            event_version=registration.event_version,
            event_kind="observation",
            payload_schema=registration.payload_schema,
            schema=registration.schema,
        )
    )
    observation_registry.seal()
    with pytest.raises(EventRegistryV1Error) as kind_error:
        observation_registry.consume(event)
    assert kind_error.value.code == "TBM_EVENT_REGISTRY_PAYLOAD_INVALID"


def test_registry_lifecycle_requires_nonempty_sealed_immutable_catalog() -> None:
    registry = EventTypeRegistry()
    with pytest.raises(EventRegistryV1Error) as unsealed:
        registry.inspect(_event())
    assert unsealed.value.code == "TBM_EVENT_REGISTRY_NOT_SEALED"
    with pytest.raises(EventRegistryV1Error) as empty:
        registry.seal()
    assert empty.value.code == "TBM_EVENT_REGISTRY_EMPTY"

    registry.register(
        EventPayloadRegistration(
            event_type="tbm.test.created",
            event_version=1,
            event_kind="domain",
            payload_schema="tbm.test.created.v1",
            schema=_schema(1),
        )
    )
    assert registry.seal().sealed is True
    assert registry.seal() is registry
    with pytest.raises(EventRegistryV1Error) as sealed:
        registry.register(
            EventPayloadRegistration(
                event_type="tbm.test.other",
                event_version=1,
                event_kind="domain",
                payload_schema="tbm.test.other.v1",
                schema=_schema(1),
            )
        )
    assert sealed.value.code == "TBM_EVENT_REGISTRY_SEALED"


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "additionalProperties": True,
            "required": [],
            "properties": {},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {},
            "format": "unsupported",
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string", "pattern": "["}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string", "minLength": -1}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string", "minLength": None}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": None}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {
                "value": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1.5,
                }
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {
                "value": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": "true",
                }
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {
                "value": {
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                }
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["password"],
            "properties": {"password": {"type": "string"}},
        },
    ],
)
def test_schema_registration_rejects_ambiguous_or_unsupported_schema(
    schema: dict[str, object],
) -> None:
    with pytest.raises(EventRegistryV1Error) as error:
        EventPayloadRegistration(
            event_type="tbm.test.invalid",
            event_version=1,
            event_kind="domain",
            payload_schema="tbm.test.invalid.v1",
            schema=schema,
        )
    assert error.value.code == "TBM_EVENT_REGISTRY_SCHEMA_INVALID"


def test_direct_registration_validator_and_public_dumps_check_types() -> None:
    registration = DEFAULT_EVENT_TYPE_REGISTRY.inspect(_event()).registration
    assert registration is not None
    validate_registered_event_payload(
        registration,
        {
            "memory_revision_id": "revision",
            "proposal_kind": "policy",
            "scope_id": "repository",
        },
    )
    with pytest.raises(EventRegistryV1Error):
        validate_registered_event_payload(registration, {"proposal_kind": "policy"})
    with pytest.raises(EventRegistryV1Error):
        dumps_event_registry_catalog("registry")  # type: ignore[arg-type]
    with pytest.raises(EventRegistryV1Error):
        dumps_event_payload_dispatch_schema("registry")  # type: ignore[arg-type]


def test_typed_view_constructor_rejects_inconsistent_source_metadata() -> None:
    event = _event()
    with pytest.raises(EventRegistryV1Error) as error:
        TypedEventView(
            source_event=event,
            event_type="tbm.memory.other",
            source_version=1,
            target_version=1,
            payload_schema="tbm.memory.other.v1",
            payload=event.payload,
            applied_upcaster_ids=(),
        )
    assert error.value.code == "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID"


@pytest.mark.parametrize(
    (
        "event_type",
        "event_version",
        "event_kind",
        "payload_schema",
        "schema",
        "expected_code",
    ),
    [
        (
            "tbm.test.invalid",
            1,
            "command",
            "tbm.test.invalid.v1",
            _schema(1),
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
        ),
        (
            "tbm.test.invalid",
            1,
            "domain",
            "invalid",
            _schema(1),
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
        ),
        (
            "TBM.Bad",
            1,
            "domain",
            "tbm.test.invalid.v1",
            _schema(1),
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
        ),
        (
            "tbm.test.invalid",
            0,
            "domain",
            "tbm.test.invalid.v1",
            _schema(1),
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
        ),
        (
            "tbm.test.invalid",
            True,
            "domain",
            "tbm.test.invalid.v1",
            _schema(1),
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
        ),
        (
            "tbm.test.invalid",
            1,
            "domain",
            "tbm.test.invalid.v1",
            [],
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
        ),
    ],
)
def test_registration_metadata_rejects_noncanonical_values(
    event_type: object,
    event_version: object,
    event_kind: object,
    payload_schema: object,
    schema: object,
    expected_code: str,
) -> None:
    with pytest.raises(EventRegistryV1Error) as error:
        EventPayloadRegistration(
            event_type=event_type,  # type: ignore[arg-type]
            event_version=event_version,  # type: ignore[arg-type]
            event_kind=event_kind,  # type: ignore[arg-type]
            payload_schema=payload_schema,  # type: ignore[arg-type]
            schema=schema,  # type: ignore[arg-type]
        )
    assert error.value.code == expected_code


def test_upcaster_contract_rejects_invalid_edges_and_exposes_descriptor() -> None:
    valid = EventPayloadUpcaster(
        event_type="tbm.test.changed",
        from_version=1,
        to_version=2,
        upcaster_id="test_changed_v1_to_v2",
        producer_version="0.1.0",
        transform=lambda payload: payload,
    )
    assert valid.to_dict() == {
        "event_type": "tbm.test.changed",
        "from_version": 1,
        "to_version": 2,
        "upcaster_id": "test_changed_v1_to_v2",
        "producer_version": "0.1.0",
    }

    with pytest.raises(EventRegistryV1Error) as nonadjacent:
        EventPayloadUpcaster(
            event_type="tbm.test.changed",
            from_version=1,
            to_version=3,
            upcaster_id="nonadjacent",
            producer_version="0.1.0",
            transform=lambda payload: payload,
        )
    assert nonadjacent.value.code == "TBM_EVENT_REGISTRY_UPCASTER_INVALID"

    with pytest.raises(EventRegistryV1Error) as noncallable:
        EventPayloadUpcaster(
            event_type="tbm.test.changed",
            from_version=1,
            to_version=2,
            upcaster_id="noncallable",
            producer_version="0.1.0",
            transform=None,  # type: ignore[arg-type]
        )
    assert noncallable.value.code == "TBM_EVENT_REGISTRY_UPCASTER_INVALID"


def test_registry_rejects_wrong_types_unregistered_edges_and_kind_changes() -> None:
    registry = EventTypeRegistry()
    with pytest.raises(EventRegistryV1Error) as bad_registration:
        registry.register(object())  # type: ignore[arg-type]
    assert bad_registration.value.code == "TBM_EVENT_REGISTRY_REGISTRATION_INVALID"

    domain = EventPayloadRegistration(
        "tbm.test.changed", 1, "domain", "tbm.test.changed.v1", _schema(1)
    )
    observation = EventPayloadRegistration(
        "tbm.test.changed",
        2,
        "observation",
        "tbm.test.changed.v2",
        _schema(2),
    )
    registry.register(domain)
    with pytest.raises(EventRegistryV1Error) as bad_upcaster:
        registry.register_upcaster(object())  # type: ignore[arg-type]
    assert bad_upcaster.value.code == "TBM_EVENT_REGISTRY_UPCASTER_INVALID"

    edge = EventPayloadUpcaster(
        "tbm.test.changed",
        1,
        2,
        "test_changed_v1_to_v2",
        "0.1.0",
        lambda payload: payload,
    )
    with pytest.raises(EventRegistryV1Error) as missing_endpoint:
        registry.register_upcaster(edge)
    assert missing_endpoint.value.code == "TBM_EVENT_REGISTRY_UPCASTER_INVALID"

    registry.register(observation)
    with pytest.raises(EventRegistryV1Error) as changed_kind:
        registry.register_upcaster(edge)
    assert changed_kind.value.code == "TBM_EVENT_REGISTRY_UPCASTER_INVALID"
    registry.seal()
    with pytest.raises(EventRegistryV1Error) as bad_event:
        registry.inspect(object())  # type: ignore[arg-type]
    assert bad_event.value.code == "TBM_EVENT_REGISTRY_EVENT_INVALID"


def test_registry_enforces_type_and_version_cardinality_limits() -> None:
    versions = EventTypeRegistry()
    for version in range(1, EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE + 1):
        versions.register(
            EventPayloadRegistration(
                "tbm.test.versioned",
                version,
                "domain",
                f"tbm.test.versioned.v{version}",
                _schema(version),
            )
        )
    with pytest.raises(EventRegistryV1Error) as version_limit:
        versions.register(
            EventPayloadRegistration(
                "tbm.test.versioned",
                EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE + 1,
                "domain",
                f"tbm.test.versioned.v{EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE + 1}",
                _schema(2),
            )
        )
    assert version_limit.value.code == "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED"

    types = EventTypeRegistry()
    for index in range(EVENT_REGISTRY_MAX_TYPES):
        types.register(
            EventPayloadRegistration(
                f"tbm.test.limit{index}",
                1,
                "domain",
                f"tbm.test.limit{index}.v1",
                _schema(1),
            )
        )
    with pytest.raises(EventRegistryV1Error) as type_limit:
        types.register(
            EventPayloadRegistration(
                "tbm.test.overflow",
                1,
                "domain",
                "tbm.test.overflow.v1",
                _schema(1),
            )
        )
    assert type_limit.value.code == "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_event": object()},
        {"source_version": 2},
        {"target_version": 0},
        {"payload_schema": "tbm.memory.other.v1"},
        {"applied_upcaster_ids": []},
        {"target_version": 2, "applied_upcaster_ids": ()},
        {"payload": []},
    ],
)
def test_typed_view_rejects_every_inconsistent_boundary(
    changes: dict[str, object],
) -> None:
    event = _event()
    values: dict[str, object] = {
        "source_event": event,
        "event_type": event.event_type,
        "source_version": 1,
        "target_version": 1,
        "payload_schema": event.payload_schema,
        "payload": event.payload,
        "applied_upcaster_ids": (),
    }
    values.update(changes)
    with pytest.raises(EventRegistryV1Error) as error:
        TypedEventView(**values)  # type: ignore[arg-type]
    assert error.value.code == "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID"


def test_consumption_rejects_downcast_unknown_target_and_typed_failures() -> None:
    registry = _versioned_registry()
    version_two = _event(
        event_type="tbm.test.changed",
        event_version=2,
        payload_schema="tbm.test.changed.v2",
        payload={"name": "before", "count": 0},
    )
    with pytest.raises(EventRegistryV1Error) as downcast:
        registry.consume(version_two, target_version=1)
    assert downcast.value.code == "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED"

    version_one = _event(
        event_type="tbm.test.changed",
        payload_schema="tbm.test.changed.v1",
        payload={"name": "before"},
    )
    with pytest.raises(EventRegistryV1Error) as unknown_target:
        registry.consume(version_one, target_version=3)
    assert unknown_target.value.code == "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED"

    failing = EventTypeRegistry()
    for version in (1, 2):
        failing.register(
            EventPayloadRegistration(
                "tbm.test.changed",
                version,
                "domain",
                f"tbm.test.changed.v{version}",
                _schema(version),
            )
        )
    failing.register_upcaster(
        EventPayloadUpcaster(
            "tbm.test.changed",
            1,
            2,
            "typed_failure",
            "0.1.0",
            lambda _payload: (_ for _ in ()).throw(
                EventRegistryV1Error(
                    "TBM_EVENT_REGISTRY_PAYLOAD_INVALID",
                    "typed failure",
                )
            ),
        )
    )
    failing.seal()
    with pytest.raises(EventRegistryV1Error) as typed_failure:
        failing.consume(version_one, target_version=2)
    assert typed_failure.value.code == "TBM_EVENT_REGISTRY_PAYLOAD_INVALID"


@pytest.mark.parametrize(
    "child_schema",
    [
        {"type": "string", "items": {"type": "string"}},
        {"type": "integer", "minLength": 1},
        {"type": "string", "minimum": 0},
        {"type": "string", "pattern": None},
        {"type": "string", "pattern": "x" * 513},
        {"enum": "x"},
        {"enum": []},
        {"enum": [1, 1]},
        {"type": "object", "additionalProperties": False},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["missing"],
            "properties": {},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {"nested": 1},
        },
        {"type": "array"},
        {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 1,
        },
        {
            "type": "string",
            "minLength": 2,
            "maxLength": 1,
        },
        {"type": "integer", "minimum": "zero"},
        {"type": "number", "minimum": 2, "maximum": 1},
        {"oneOf": "invalid"},
        {"oneOf": []},
        {"oneOf": [1]},
    ],
)
def test_schema_definition_rejects_contextual_and_structural_ambiguity(
    child_schema: object,
) -> None:
    with pytest.raises(EventRegistryV1Error) as error:
        _value_registration(child_schema)
    assert error.value.code == "TBM_EVENT_REGISTRY_SCHEMA_INVALID"


@pytest.mark.parametrize(
    ("child_schema", "value"),
    [
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, True),
        ({"oneOf": [{"type": "number"}, {"type": "integer"}]}, 1),
        ({"const": "expected"}, "other"),
        ({"enum": ["expected"]}, "other"),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "required": [],
                "properties": {},
            },
            "not-an-object",
        ),
        ({"type": "array", "items": {"type": "integer"}}, "not-an-array"),
        (
            {"type": "array", "items": {"type": "integer"}, "minItems": 2},
            [1],
        ),
        (
            {"type": "array", "items": {"type": "integer"}, "maxItems": 1},
            [1, 2],
        ),
        (
            {
                "type": "array",
                "items": {"type": "integer"},
                "uniqueItems": True,
            },
            [1, 1],
        ),
        ({"type": "string"}, 1),
        ({"type": "string", "minLength": 2}, "x"),
        ({"type": "string", "maxLength": 1}, "xx"),
        ({"type": "string", "pattern": "x+"}, "y"),
        ({"type": "integer"}, "1"),
        ({"type": "number"}, "1"),
        ({"type": "boolean"}, 1),
        ({"type": "null"}, 0),
        ({"type": "integer", "minimum": 1}, 0),
        ({"type": "integer", "maximum": 1}, 2),
    ],
)
def test_payload_validation_rejects_each_supported_schema_boundary(
    child_schema: object,
    value: object,
) -> None:
    registration = _value_registration(child_schema)
    with pytest.raises(EventRegistryV1Error) as error:
        validate_registered_event_payload(registration, {"value": value})
    assert error.value.code == "TBM_EVENT_REGISTRY_PAYLOAD_INVALID"


def test_payload_validation_accepts_valid_union_and_iterates_array_items() -> None:
    union = _value_registration({"oneOf": [{"type": "string"}, {"type": "integer"}]})
    validate_registered_event_payload(union, {"value": "selected"})

    array = _value_registration(
        {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
        }
    )
    validate_registered_event_payload(array, {"value": [0, 1]})


def test_payload_copy_rejects_secret_cycles_depth_size_keys_and_non_json() -> None:
    registration = _value_registration({"type": "string"})
    invalid_payloads: list[object] = [
        [],
        {"password": "secret"},
        {1: "non-string-key"},
        {"value": object()},
        {"value": [0] * 8192},
    ]

    mapping_cycle: dict[str, object] = {}
    mapping_cycle["value"] = mapping_cycle
    invalid_payloads.append(mapping_cycle)
    sequence_cycle: list[object] = []
    sequence_cycle.append(sequence_cycle)
    invalid_payloads.append({"value": sequence_cycle})

    too_deep: dict[str, object] = {}
    cursor = too_deep
    for _ in range(30):
        child: dict[str, object] = {}
        cursor["value"] = child
        cursor = child
    invalid_payloads.append(too_deep)

    for payload in invalid_payloads:
        with pytest.raises(EventRegistryV1Error) as error:
            validate_registered_event_payload(
                registration,
                payload,  # type: ignore[arg-type]
            )
        assert error.value.code == "TBM_EVENT_REGISTRY_PAYLOAD_INVALID"


def test_schema_byte_limit_is_enforced_after_structural_validation() -> None:
    large_enum = [f"{index:03d}-" + "x" * 600 for index in range(256)]
    with pytest.raises(EventRegistryV1Error) as error:
        _value_registration({"enum": large_enum})
    assert error.value.code == "TBM_EVENT_REGISTRY_SCHEMA_INVALID"


def test_direct_payload_validator_rejects_wrong_registration_type() -> None:
    with pytest.raises(EventRegistryV1Error) as error:
        validate_registered_event_payload(
            object(),  # type: ignore[arg-type]
            {},
        )
    assert error.value.code == "TBM_EVENT_REGISTRY_REGISTRATION_INVALID"


def test_registry_covers_root_metadata_identifier_and_missing_path_guards() -> None:
    with pytest.raises(EventRegistryV1Error) as non_object_root:
        EventPayloadRegistration(
            "tbm.test.root",
            1,
            "domain",
            "tbm.test.root.v1",
            {"type": "string"},
        )
    assert non_object_root.value.code == "TBM_EVENT_REGISTRY_SCHEMA_INVALID"

    with pytest.raises(EventRegistryV1Error) as metadata:
        _value_registration({"type": "string", "title": ""})
    assert metadata.value.code == "TBM_EVENT_REGISTRY_SCHEMA_INVALID"

    with pytest.raises(EventRegistryV1Error) as identifier:
        EventPayloadUpcaster(
            "tbm.test.changed",
            1,
            2,
            "invalid upcaster id",
            "0.1.0",
            lambda payload: payload,
        )
    assert identifier.value.code == "TBM_EVENT_REGISTRY_REGISTRATION_INVALID"

    no_edge = _versioned_registry(with_upcaster=False)
    assert any(
        row.source_version == 1
        and row.target_version == 2
        and row.compatibility == "unsupported"
        for row in no_edge.compatibility_matrix()
    )


def test_registry_enforces_upcaster_and_matrix_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = EventTypeRegistry()
    for version in (1, 2):
        registry.register(
            EventPayloadRegistration(
                "tbm.test.changed",
                version,
                "domain",
                f"tbm.test.changed.v{version}",
                _schema(version),
            )
        )
    edge = EventPayloadUpcaster(
        "tbm.test.changed",
        1,
        2,
        "limited_edge",
        "0.1.0",
        lambda payload: payload,
    )
    monkeypatch.setattr(registry_module, "EVENT_REGISTRY_MAX_UPCASTERS", 0)
    with pytest.raises(EventRegistryV1Error) as upcaster_limit:
        registry.register_upcaster(edge)
    assert upcaster_limit.value.code == "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED"

    registry.seal()
    monkeypatch.setattr(
        registry_module,
        "EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS",
        0,
    )
    with pytest.raises(EventRegistryV1Error) as matrix_limit:
        registry.compatibility_matrix()
    assert matrix_limit.value.code == "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED"
