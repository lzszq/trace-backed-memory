from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.event_v1 import (
    EVENT_JSON_MAX_BYTES,
    EVENT_PAYLOAD_MAX_BYTES,
    EVENT_PROTOCOL_VERSION,
    CanonicalEvent,
    EventArtifactRef,
    EventSource,
    EventTrustedContext,
    EventV1ContractError,
    build_canonical_event,
    canonical_event_sha256,
    dumps_canonical_event,
    event_payload_sha256,
    loads_canonical_event,
    parse_canonical_event,
    verify_event_parent,
    verify_event_trusted_context,
)


ROOT = Path(__file__).resolve().parents[1]


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _trusted(**overrides: object) -> EventTrustedContext:
    values: dict[str, object] = {
        "organization_id": "organization_001",
        "tenant_id": "tenant_001",
        "repository_id": "repository_001",
        "environment_id": "environment_local",
        "principal_id": "principal_001",
        "agent_client_id": "agent_client_001",
        "actor_type": "principal",
        "actor_id": "principal_001",
        "authorization_decision_id": "authorization_decision_001",
    }
    values.update(overrides)
    return EventTrustedContext(**values)  # type: ignore[arg-type]


def _artifact(
    character: str = "a", **overrides: object
) -> EventArtifactRef:
    values: dict[str, object] = {
        "artifact_id": "artifact_sha256_" + character * 64,
        "content_sha256": _digest(character),
        "media_type": "application/json",
        "size_bytes": 128,
        "classification": "internal",
        "retention_policy_id": "retention_engineering_memory",
        "encryption_key_id": None,
        "availability": "available",
    }
    values.update(overrides)
    return EventArtifactRef(**values)  # type: ignore[arg-type]


def _event(**overrides: object) -> CanonicalEvent:
    values: dict[str, object] = {
        "event_id": "evt_memory_proposed_001",
        "event_type": "tbm.memory.proposed",
        "event_version": 1,
        "event_kind": "domain",
        "origin": "native",
        "source": None,
        "stream_id": "memory_revision_001",
        "stream_type": "memory_revision",
        "stream_version": 1,
        "global_position": 1,
        "trusted_context": _trusted(),
        "request_id": "request_001",
        "idempotency_key_sha256": _digest("b"),
        "request_sha256": _digest("c"),
        "correlation_id": "correlation_001",
        "causation_id": None,
        "occurred_at": "2026-07-31T12:00:00Z",
        "recorded_at": "2026-07-31T12:00:01Z",
        "producer": "trace_backed_memory",
        "producer_version": "0.1.0",
        "payload_schema": "tbm.memory.proposed.v1",
        "previous_stream_event_sha256": None,
        "classification": "internal",
        "retention_policy_id": "retention_engineering_memory",
        "artifact_refs": (_artifact(),),
        "payload": {
            "memory_revision_id": "memory_revision_001",
            "proposal_kind": "lesson",
        },
    }
    values.update(overrides)
    return build_canonical_event(**values)  # type: ignore[arg-type]


def _direct_event(**overrides: object) -> CanonicalEvent:
    event = _event()
    values = {
        name: getattr(event, name)
        for name in CanonicalEvent.__dataclass_fields__
    }
    values.update(overrides)
    return CanonicalEvent(**values)  # type: ignore[arg-type]


def test_example_round_trips_and_schema_matches_envelope() -> None:
    example = json.loads(
        (ROOT / "examples" / "event_v1.example.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "schemas" / "event_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )

    event = loads_canonical_event(json.dumps(example))

    assert event.to_dict() == example
    assert json.loads(dumps_canonical_event(event)) == example
    assert set(schema["required"]) == set(example)
    assert set(schema["properties"]) == set(example)
    assert schema["additionalProperties"] is False
    assert event.protocol_version == EVENT_PROTOCOL_VERSION


def test_public_package_exports_the_event_contract() -> None:
    assert tbm.CanonicalEvent is CanonicalEvent
    assert tbm.EVENT_PROTOCOL_VERSION == "tbm.event.v1"
    assert "build_canonical_event" in tbm.__all__
    assert "verify_event_trusted_context" in tbm.__all__


def test_builder_copies_payload_sorts_artifacts_and_domain_separates_hash() -> None:
    payload: dict[str, object] = {"nested": {"state": "draft"}}
    event = _event(
        artifact_refs=(_artifact("b"), _artifact("a")), payload=payload
    )
    payload["nested"] = {"state": "mutated"}

    assert event.payload["nested"] != payload["nested"]
    assert [item.artifact_id for item in event.artifact_refs] == [
        "artifact_sha256_" + "a" * 64,
        "artifact_sha256_" + "b" * 64,
    ]
    encoded = dumps_canonical_event(event)
    assert encoded == json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    unsigned = event.to_dict(include_event_sha256=False)
    raw_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert event.event_sha256 != raw_hash


def test_unknown_well_formed_event_type_remains_an_envelope_concern() -> None:
    event = _event(
        event_type="tbm.future.capability_observed",
        event_kind="observation",
        origin="imported",
        source=EventSource(
            source_system="future_connector",
            source_record_id="record_001",
            evidence_quality="observed",
            observed_at="2026-07-31T11:59:59Z",
        ),
        occurred_at=None,
        payload_schema="tbm.future.capability_observed.v9",
    )

    assert loads_canonical_event(dumps_canonical_event(event)) == event


def test_import_and_timestamp_provenance_do_not_fabricate_time() -> None:
    source = EventSource(
        source_system="legacy_snapshot_v2",
        source_record_id="lesson_001",
        evidence_quality="legacy_partial",
        observed_at="2026-07-31T11:59:59Z",
    )

    imported = _event(
        event_kind="observation",
        origin="imported",
        source=source,
        occurred_at=None,
    )
    assert imported.occurred_at is None

    with pytest.raises(EventV1ContractError, match="source observation"):
        _event(occurred_at=None)
    with pytest.raises(EventV1ContractError, match="require source evidence"):
        _event(origin="imported")
    with pytest.raises(EventV1ContractError, match="cannot claim import"):
        _event(source=source)
    future_source = EventSource(
        source_system="legacy_snapshot_v2",
        source_record_id="lesson_001",
        evidence_quality="observed",
        observed_at="2026-07-31T12:00:02Z",
    )
    with pytest.raises(EventV1ContractError, match="observed_at"):
        _event(
            event_kind="observation",
            origin="imported",
            source=future_source,
        )


def test_parent_and_trusted_context_verification_are_exact() -> None:
    first = _event()
    second = _event(
        event_id="evt_memory_approved_001",
        event_type="tbm.memory.approved",
        stream_version=2,
        global_position=7,
        previous_stream_event_sha256=first.event_sha256,
        causation_id=first.event_id,
    )

    verify_event_parent(first, None)
    verify_event_parent(second, first)
    verify_event_trusted_context(second, _trusted())

    wrong_parent = _event(stream_id="memory_revision_002")
    with pytest.raises(EventV1ContractError, match="previous_stream"):
        verify_event_parent(second, wrong_parent)
    with pytest.raises(EventV1ContractError, match="tenant_id"):
        verify_event_trusted_context(second, _trusted(tenant_id="tenant_002"))


def test_strict_parser_rejects_ambiguous_or_unbounded_json() -> None:
    encoded = dumps_canonical_event(_event())
    duplicate = encoded.replace(
        '"actor_id":"principal_001"',
        '"actor_id":"principal_001","actor_id":"principal_002"',
    )
    with pytest.raises(EventV1ContractError) as duplicate_error:
        loads_canonical_event(duplicate)
    assert duplicate_error.value.code == "TBM_EVENT_INVALID_JSON"

    unknown = json.loads(encoded)
    unknown["unexpected"] = True
    with pytest.raises(EventV1ContractError, match="fields"):
        parse_canonical_event(unknown)
    with pytest.raises(EventV1ContractError) as utf8_error:
        loads_canonical_event(b'\xff')
    assert utf8_error.value.code == "TBM_EVENT_INVALID_JSON"
    with pytest.raises(EventV1ContractError):
        loads_canonical_event(" " * (EVENT_JSON_MAX_BYTES + 1))
    with pytest.raises(EventV1ContractError):
        loads_canonical_event(
            encoded.replace('"stream_version":1', '"stream_version":NaN')
        )


@pytest.mark.parametrize(
    "secret_key",
    [
        "API-Key",
        "apiKey",
        "accessToken",
        "Authorization",
        "client_secret",
        "privateKey",
        "x-api-key",
    ],
)
def test_payload_secret_metadata_variants_fail_closed(secret_key: str) -> None:
    with pytest.raises(EventV1ContractError, match="secret metadata"):
        _event(payload={"metadata": {secret_key: "must-not-persist"}})


def test_payload_bounds_cycles_and_integrity_fail_closed() -> None:
    cycle: dict[str, object] = {}
    cycle["cycle"] = cycle
    with pytest.raises(EventV1ContractError, match="cycle"):
        _event(payload=cycle)
    with pytest.raises(EventV1ContractError, match="byte limit"):
        _event(payload={"value": "x" * EVENT_PAYLOAD_MAX_BYTES})

    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(25):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(EventV1ContractError, match="depth"):
        _event(payload=nested)

    raw = _event().to_dict()
    raw_payload = raw["payload"]
    assert isinstance(raw_payload, dict)
    raw_payload["proposal_kind"] = "policy"
    with pytest.raises(EventV1ContractError, match="payload_sha256"):
        parse_canonical_event(raw)

    raw = _event().to_dict()
    raw["producer_version"] = "0.2.0"
    with pytest.raises(EventV1ContractError, match="event_sha256"):
        parse_canonical_event(raw)


def test_artifact_descriptors_enforce_content_identity_and_classification() -> None:
    with pytest.raises(EventV1ContractError, match="derived"):
        _artifact(artifact_id="artifact_sha256_" + "f" * 64)
    with pytest.raises(EventV1ContractError, match="encryption_key_id"):
        _artifact(classification="confidential")
    with pytest.raises(EventV1ContractError, match="classification"):
        _event(
            classification="public",
            artifact_refs=(_artifact(classification="internal"),),
        )


def test_direct_contract_construction_rejects_bad_versions_and_self_causation() -> None:
    raw = _event().to_dict()
    raw["stream_version"] = True
    with pytest.raises(EventV1ContractError, match="integer"):
        parse_canonical_event(raw)

    with pytest.raises(EventV1ContractError, match="itself"):
        _event(causation_id="evt_memory_proposed_001")


def test_builder_reports_stable_contract_errors_for_malformed_inputs() -> None:
    with pytest.raises(EventV1ContractError) as timestamp_error:
        _event(recorded_at="not-a-timestamp")
    assert timestamp_error.value.code == "TBM_EVENT_INVALID"

    with pytest.raises(EventV1ContractError) as source_error:
        _event(source={"source_system": "untrusted"})
    assert source_error.value.code == "TBM_EVENT_INVALID"

    with pytest.raises(EventV1ContractError) as artifact_error:
        _event(artifact_refs=[_artifact()])
    assert artifact_error.value.code == "TBM_EVENT_INVALID"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protocol_version", "tbm.event.v2", "protocol_version"),
        ("event_id", "bad", "event identifier"),
        ("event_type", "MemoryProposed", "event_type"),
        ("event_version", 0, "positive integer"),
        ("event_kind", "unknown", "not supported"),
        ("origin", "unknown", "not supported"),
        ("actor_type", "model", "not supported"),
        ("organization_id", " leading", "bounded identifier"),
        ("producer_version", "", "bounded code"),
        ("payload_schema", "memory.proposed", "payload_schema"),
        ("request_sha256", "sha256:bad", "sha256 digest"),
        ("global_position", 0, "positive integer"),
        ("occurred_at", "2026-07-31T12:00:00+00:00", "canonical RFC3339"),
        ("classification", "secret", "not supported"),
    ],
)
def test_direct_envelope_validation_rejects_invalid_fields(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(EventV1ContractError, match=message):
        _direct_event(**{field: value})


def test_direct_envelope_validation_rejects_source_and_stream_inconsistency() -> None:
    with pytest.raises(EventV1ContractError, match="EventSource or null"):
        _direct_event(source={"source_system": "untrusted"})
    with pytest.raises(EventV1ContractError, match="observation"):
        _direct_event(event_kind="observation")
    with pytest.raises(EventV1ContractError, match="occurred_at"):
        _direct_event(occurred_at="2026-07-31T12:00:02Z")
    with pytest.raises(EventV1ContractError, match="first stream event"):
        _direct_event(previous_stream_event_sha256=_digest("f"))


def test_artifact_and_source_value_bounds_are_strict() -> None:
    with pytest.raises(EventV1ContractError, match="size_bytes"):
        _artifact(size_bytes=-1)
    protected = _artifact(
        classification="confidential", encryption_key_id="kms_key_001"
    )
    assert protected.encryption_key_id == "kms_key_001"
    with pytest.raises(EventV1ContractError, match="availability"):
        _artifact(availability="lost")
    with pytest.raises(EventV1ContractError, match="evidence_quality"):
        EventSource(
            source_system="legacy",
            source_record_id="record_001",
            evidence_quality="fabricated",  # type: ignore[arg-type]
            observed_at="2026-07-31T12:00:00Z",
        )


def test_hash_helpers_and_canonical_encoder_reject_noncanonical_input() -> None:
    event = _event()
    with pytest.raises(EventV1ContractError, match="unsigned"):
        canonical_event_sha256(event.to_dict())
    with pytest.raises(EventV1ContractError) as json_error:
        canonical_event_sha256({"unsupported": {"set"}})
    assert json_error.value.code == "TBM_EVENT_NON_CANONICAL_JSON"
    with pytest.raises(EventV1ContractError, match="payload must be"):
        event_payload_sha256(["not", "an", "object"])  # type: ignore[arg-type]


def test_parent_verifier_rejects_every_discontinuity() -> None:
    first = _event()
    second = _event(
        event_id="evt_memory_approved_001",
        event_type="tbm.memory.approved",
        stream_version=2,
        global_position=2,
        previous_stream_event_sha256=first.event_sha256,
    )
    with pytest.raises(EventV1ContractError, match="exactly CanonicalEvent"):
        verify_event_parent("event", None)  # type: ignore[arg-type]
    with pytest.raises(EventV1ContractError, match="cannot have a parent"):
        verify_event_parent(first, first)
    with pytest.raises(EventV1ContractError, match="requires its parent"):
        verify_event_parent(second, None)

    skipped = _event(
        event_id="evt_memory_approved_002",
        event_type="tbm.memory.approved",
        stream_version=3,
        global_position=3,
        previous_stream_event_sha256=first.event_sha256,
    )
    with pytest.raises(EventV1ContractError, match="advance by one"):
        verify_event_parent(skipped, first)
    stale_position = _event(
        event_id="evt_memory_approved_003",
        event_type="tbm.memory.approved",
        stream_version=2,
        global_position=1,
        previous_stream_event_sha256=first.event_sha256,
    )
    with pytest.raises(EventV1ContractError, match="global_position"):
        verify_event_parent(stale_position, first)
    other_scope = _event(
        event_id="evt_memory_approved_004",
        event_type="tbm.memory.approved",
        stream_version=2,
        global_position=2,
        previous_stream_event_sha256=first.event_sha256,
        stream_id="memory_revision_002",
    )
    with pytest.raises(EventV1ContractError, match="stream_id"):
        verify_event_parent(other_scope, first)
    reversed_time = _event(
        event_id="evt_memory_approved_005",
        event_type="tbm.memory.approved",
        stream_version=2,
        global_position=2,
        previous_stream_event_sha256=first.event_sha256,
        occurred_at="2026-07-31T11:59:59Z",
        recorded_at="2026-07-31T11:59:59Z",
    )
    with pytest.raises(EventV1ContractError, match="precedes parent"):
        verify_event_parent(reversed_time, first)


def test_public_verifiers_and_dump_reject_wrong_runtime_types() -> None:
    with pytest.raises(EventV1ContractError, match="exactly CanonicalEvent"):
        verify_event_trusted_context("event", _trusted())  # type: ignore[arg-type]
    with pytest.raises(EventV1ContractError, match="exactly EventTrustedContext"):
        verify_event_trusted_context(_event(), {})  # type: ignore[arg-type]
    with pytest.raises(EventV1ContractError, match="exactly CanonicalEvent"):
        dumps_canonical_event("event")  # type: ignore[arg-type]


def test_parser_rejects_wrong_nested_shapes_and_scalar_types() -> None:
    raw = _event().to_dict()
    raw["source"] = 1
    with pytest.raises(EventV1ContractError, match="source must be"):
        parse_canonical_event(raw)

    raw = _event().to_dict()
    raw["artifact_refs"] = "not-an-array"
    with pytest.raises(EventV1ContractError, match="must be an array"):
        parse_canonical_event(raw)
    raw = _event().to_dict()
    raw["artifact_refs"] = [{}] * 129
    with pytest.raises(EventV1ContractError, match="item limit"):
        parse_canonical_event(raw)
    raw = _event().to_dict()
    raw["artifact_refs"] = [1]
    with pytest.raises(EventV1ContractError, match="must be an object"):
        parse_canonical_event(raw)
    raw = _event().to_dict()
    raw["payload"] = []
    with pytest.raises(EventV1ContractError, match="payload must be"):
        parse_canonical_event(raw)

    for field, value, message in (
        ("event_id", 1, "must be a string"),
        ("causation_id", 1, "string or null"),
        ("global_position", "1", "must be an integer"),
    ):
        raw = _event().to_dict()
        raw[field] = value
        with pytest.raises(EventV1ContractError, match=message):
            parse_canonical_event(raw)


def test_json_loader_rejects_wrong_top_level_and_input_type() -> None:
    for value in ("[]", "null", "1"):
        with pytest.raises(EventV1ContractError, match="must be an object"):
            loads_canonical_event(value)
    with pytest.raises(EventV1ContractError) as type_error:
        loads_canonical_event(123)  # type: ignore[arg-type]
    assert type_error.value.code == "TBM_EVENT_INVALID_JSON"


def test_payload_accepts_all_json_scalars_and_rejects_invalid_values() -> None:
    event = _event(
        payload={"items": [None, True, 1, 1.5, "value", ["nested"]]}
    )
    assert loads_canonical_event(dumps_canonical_event(event)) == event

    list_cycle: list[object] = []
    list_cycle.append(list_cycle)
    with pytest.raises(EventV1ContractError, match="cycle"):
        _event(payload={"items": list_cycle})
    with pytest.raises(EventV1ContractError, match="node count"):
        _event(payload={"items": [None] * 8192})
    with pytest.raises(EventV1ContractError, match="payload keys"):
        _event(payload={"": "empty key"})
    with pytest.raises(EventV1ContractError, match="non-finite"):
        _event(payload={"value": float("inf")})
    with pytest.raises(EventV1ContractError, match="non-JSON"):
        _event(payload={"value": object()})
    with pytest.raises(EventV1ContractError, match="secret metadata"):
        _event(payload={"items": [{"Authorization": "Bearer secret"}]})


def test_payload_and_identity_reject_invalid_utf8() -> None:
    invalid_surrogate = "\ud800"
    with pytest.raises(EventV1ContractError, match="valid UTF-8"):
        _event(payload={"value": invalid_surrogate})
    with pytest.raises(EventV1ContractError, match="valid UTF-8"):
        _trusted(principal_id=invalid_surrogate)


def test_direct_artifact_tuple_validation_is_defensive() -> None:
    with pytest.raises(EventV1ContractError, match="bounded tuple"):
        _direct_event(artifact_refs=[_artifact()])
    with pytest.raises(EventV1ContractError, match="contain EventArtifactRef"):
        _direct_event(artifact_refs=("artifact",))
    with pytest.raises(EventV1ContractError, match="sorted and unique"):
        _direct_event(artifact_refs=(_artifact("b"), _artifact("a")))
