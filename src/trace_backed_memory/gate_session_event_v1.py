from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import NoReturn, cast

from .contracts_v3 import V3ContractError
from ._timestamps import RFC3339_PATTERN, canonical_rfc3339
from .event_v1 import (
    CanonicalEvent,
    EventTrustedContext,
    build_canonical_event,
    verify_event_parent,
)
from .gate_session_v3 import (
    GATE_SESSION_CONTRACT_VERSION,
    GATE_SESSION_MAX_MEMORY_REVISIONS,
    GATE_SESSION_MAX_SEMANTIC_ATTEMPTS,
    GateSession,
    GateSessionStatus,
    parse_gate_session,
    renew_gate_session_lease,
    transition_gate_session,
)


GATE_SESSION_EVENT_CONTRACT_VERSION = "tbm.gate-session-event.v1"
GATE_SESSION_EVENT_VERSION = 1
GATE_SESSION_EVENT_STREAM_TYPE = "gate_session"
GATE_SESSION_EVENT_PRODUCER = "trace_backed_memory"
GATE_SESSION_EVENT_PRODUCER_VERSION = "0.1.0"
GATE_SESSION_EVENT_RETENTION_POLICY_ID = "retention_engineering_memory"

GATE_SESSION_CREATED_EVENT = "tbm.gate_session.created"
GATE_SESSION_PREPARED_EVENT = "tbm.gate_session.prepared"
SEMANTIC_GATE_REQUESTED_EVENT = "tbm.semantic_gate.requested"
SEMANTIC_GATE_DECIDED_EVENT = "tbm.semantic_gate.decided"
USAGE_DECISION_FINALIZED_EVENT = "tbm.usage_decision.finalized"
EXECUTION_STARTED_EVENT = "tbm.execution.started"
GATE_SESSION_COMPLETED_EVENT = "tbm.gate_session.completed"
GATE_SESSION_CANCELED_EVENT = "tbm.gate_session.canceled"
GATE_SESSION_EXPIRED_EVENT = "tbm.gate_session.expired"
EXECUTION_ABANDONED_EVENT = "tbm.execution.abandoned"
GATE_SESSION_LEASE_RENEWED_EVENT = "tbm.gate_session.lease_renewed"

GATE_SESSION_EVENT_TYPES = tuple(
    sorted(
        (
            GATE_SESSION_CREATED_EVENT,
            GATE_SESSION_PREPARED_EVENT,
            SEMANTIC_GATE_REQUESTED_EVENT,
            SEMANTIC_GATE_DECIDED_EVENT,
            USAGE_DECISION_FINALIZED_EVENT,
            EXECUTION_STARTED_EVENT,
            GATE_SESSION_COMPLETED_EVENT,
            GATE_SESSION_CANCELED_EVENT,
            GATE_SESSION_EXPIRED_EVENT,
            EXECUTION_ABANDONED_EVENT,
            GATE_SESSION_LEASE_RENEWED_EVENT,
        )
    )
)

_STATUS_EVENT_TYPES: dict[GateSessionStatus, str] = {
    "created": GATE_SESSION_CREATED_EVENT,
    "prepared": GATE_SESSION_PREPARED_EVENT,
    "awaiting_decision": SEMANTIC_GATE_REQUESTED_EVENT,
    "decided": SEMANTIC_GATE_DECIDED_EVENT,
    "finalized": USAGE_DECISION_FINALIZED_EVENT,
    "executing": EXECUTION_STARTED_EVENT,
    "completed": GATE_SESSION_COMPLETED_EVENT,
    "canceled": GATE_SESSION_CANCELED_EVENT,
    "expired": GATE_SESSION_EXPIRED_EVENT,
    "abandoned": EXECUTION_ABANDONED_EVENT,
}


class GateSessionEventV1Error(V3ContractError):
    """Stable failure for canonical GateSession revision events."""


def gate_session_event_type(
    session: GateSession,
    previous_session: GateSession | None,
) -> str:
    if type(session) is not GateSession:
        _invalid("session must be exactly GateSession")
    if previous_session is None:
        if session.status != "created" or session.version != 1:
            _invalid("the first GateSession event must contain revision 1 created")
        return GATE_SESSION_CREATED_EVENT
    if type(previous_session) is not GateSession:
        _invalid("previous_session must be exactly GateSession or null")
    if session.session_id != previous_session.session_id:
        _invalid("GateSession event revisions must retain session_id")
    if session.version != previous_session.version + 1:
        _invalid("GateSession event revisions must advance by one")
    if session.status == previous_session.status:
        return GATE_SESSION_LEASE_RENEWED_EVENT
    return _STATUS_EVENT_TYPES[session.status]


def gate_session_event_payload_schema(event_type: str) -> dict[str, object]:
    if event_type not in GATE_SESSION_EVENT_TYPES:
        _invalid("event_type is not a GateSession revision event")
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^\S(?:[\s\S]*\S)?$",
    }
    optional_identifier = {
        "oneOf": [
            {"type": "null"},
            identifier,
        ]
    }
    timestamp = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{RFC3339_PATTERN}$",
    }
    digest = {
        "type": "string",
        "pattern": r"sha256:[0-9a-f]{64}",
    }
    session_properties: dict[str, object] = {
        "contract_version": {"const": GATE_SESSION_CONTRACT_VERSION},
        "session_id": identifier,
        "tenant_id": identifier,
        "repository_id": identifier,
        "principal_id": identifier,
        "agent_client_id": identifier,
        "trace_id": identifier,
        "run_id": identifier,
        "request_fingerprint": digest,
        "idempotency_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
        },
        "status": {
            "enum": [
                "created",
                "prepared",
                "awaiting_decision",
                "decided",
                "finalized",
                "executing",
                "completed",
                "canceled",
                "expired",
                "abandoned",
            ]
        },
        "version": {"type": "integer", "minimum": 1},
        "created_at": timestamp,
        "updated_at": timestamp,
        "expires_at": timestamp,
        "lease_expires_at": {
            "oneOf": [{"type": "null"}, timestamp]
        },
        "retrieval_snapshot_id": optional_identifier,
        "system_gate_evaluation_id": optional_identifier,
        "semantic_gate_attempt_ids": {
            "type": "array",
            "items": identifier,
            "maxItems": GATE_SESSION_MAX_SEMANTIC_ATTEMPTS,
            "uniqueItems": True,
        },
        "decision_id": optional_identifier,
        "final_memory_revision_ids": {
            "type": "array",
            "items": identifier,
            "maxItems": GATE_SESSION_MAX_MEMORY_REVISIONS,
            "uniqueItems": True,
        },
        "injection_artifact_id": optional_identifier,
        "usage_decision_id": optional_identifier,
        "run_outcome_id": optional_identifier,
        "terminal_reason": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                },
            ]
        },
    }
    session_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(session_properties),
        "properties": session_properties,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "contract_version",
            "session",
            "session_sha256",
            "previous_session_sha256",
            "transition_authorization_event_id",
        ],
        "properties": {
            "contract_version": {
                "const": GATE_SESSION_EVENT_CONTRACT_VERSION,
            },
            "session": session_schema,
            "session_sha256": digest,
            "previous_session_sha256": {
                "oneOf": [{"type": "null"}, digest]
            },
            "transition_authorization_event_id": identifier,
        },
    }


def gate_session_revision_sha256(session: GateSession) -> str:
    if type(session) is not GateSession:
        _invalid("session must be exactly GateSession")
    return _domain_sha256(
        b"tbm.gate-session-revision.v1\x00",
        session.to_dict(),
    )


def gate_session_event_id(session: GateSession) -> str:
    if type(session) is not GateSession:
        _invalid("session must be exactly GateSession")
    identity_sha256 = _event_identity_sha256(
        session.session_id,
        session.version,
    )
    return "evt_gate_" + identity_sha256.removeprefix("sha256:")


def build_gate_session_event(
    session: GateSession,
    *,
    previous_session: GateSession | None,
    parent_event: CanonicalEvent | None,
    global_position: int,
    trusted_context: EventTrustedContext,
) -> CanonicalEvent:
    event_type = gate_session_event_type(session, previous_session)
    if type(trusted_context) is not EventTrustedContext:
        _invalid("trusted_context must be exactly EventTrustedContext")
    _verify_context(session, trusted_context)
    if (previous_session is None) != (parent_event is None):
        _invalid("GateSession event parent and previous revision must align")
    if parent_event is not None:
        if (
            type(parent_event) is not CanonicalEvent
            or parent_event.stream_id != session.session_id
            or parent_event.stream_type != GATE_SESSION_EVENT_STREAM_TYPE
            or parent_event.stream_version != session.version - 1
            or parent_event.event_type not in GATE_SESSION_EVENT_TYPES
        ):
            _invalid("GateSession event parent is invalid")
        _verify_parent_revision(parent_event, cast(GateSession, previous_session))
    payload = {
        "contract_version": GATE_SESSION_EVENT_CONTRACT_VERSION,
        "session": session.to_dict(),
        "session_sha256": gate_session_revision_sha256(session),
        "previous_session_sha256": (
            None
            if previous_session is None
            else gate_session_revision_sha256(previous_session)
        ),
        "transition_authorization_event_id": (
            trusted_context.authorization_decision_id
        ),
    }
    command_sha256 = _event_command_sha256(
        event_type,
        payload,
        trusted_context.authorization_decision_id,
    )
    identity_sha256 = _event_identity_sha256(
        session.session_id,
        session.version,
    )
    event = build_canonical_event(
        event_id=gate_session_event_id(session),
        event_type=event_type,
        event_version=GATE_SESSION_EVENT_VERSION,
        event_kind="domain",
        origin="native",
        source=None,
        stream_id=session.session_id,
        stream_type=GATE_SESSION_EVENT_STREAM_TYPE,
        stream_version=session.version,
        global_position=global_position,
        trusted_context=trusted_context,
        request_id=(
            "gate_request_" + identity_sha256.removeprefix("sha256:")[:48]
        ),
        idempotency_key_sha256=identity_sha256,
        request_sha256=command_sha256,
        correlation_id=(
            "gate_correlation_"
            + _domain_sha256(
                b"tbm.gate-session-correlation.v1\x00",
                {"session_id": session.session_id},
            ).removeprefix("sha256:")[:40]
        ),
        causation_id=None if parent_event is None else parent_event.event_id,
        occurred_at=session.updated_at,
        recorded_at=session.updated_at,
        producer=GATE_SESSION_EVENT_PRODUCER,
        producer_version=GATE_SESSION_EVENT_PRODUCER_VERSION,
        payload_schema=f"{event_type}.v1",
        previous_stream_event_sha256=(
            None if parent_event is None else parent_event.event_sha256
        ),
        classification="internal",
        retention_policy_id=GATE_SESSION_EVENT_RETENTION_POLICY_ID,
        artifact_refs=(),
        payload=payload,
    )
    try:
        verify_event_parent(event, parent_event)
    except Exception as error:
        raise GateSessionEventV1Error(
            "TBM_GATE_SESSION_EVENT_INVALID",
            "GateSession event parent chain is invalid",
        ) from error
    parse_gate_session_event(
        event,
        previous_session=previous_session,
        parent_event=parent_event,
    )
    return event


def parse_gate_session_event(
    event: CanonicalEvent,
    *,
    previous_session: GateSession | None,
    parent_event: CanonicalEvent | None = None,
) -> GateSession:
    if type(event) is not CanonicalEvent:
        _invalid("event must be exactly CanonicalEvent")
    if event.event_type not in GATE_SESSION_EVENT_TYPES:
        _invalid("event is not a GateSession revision event")
    if (
        event.event_version != GATE_SESSION_EVENT_VERSION
        or event.event_kind != "domain"
        or event.origin != "native"
        or event.source is not None
        or event.stream_type != GATE_SESSION_EVENT_STREAM_TYPE
        or event.payload_schema != f"{event.event_type}.v1"
        or event.classification != "internal"
        or event.retention_policy_id
        != GATE_SESSION_EVENT_RETENTION_POLICY_ID
        or event.artifact_refs
    ):
        _invalid("GateSession event envelope is invalid")
    payload = _plain_mapping(event.payload, "GateSession event payload")
    expected_fields = {
        "contract_version",
        "session",
        "session_sha256",
        "previous_session_sha256",
        "transition_authorization_event_id",
    }
    if set(payload) != expected_fields:
        _invalid("GateSession event payload fields are invalid")
    if payload["contract_version"] != GATE_SESSION_EVENT_CONTRACT_VERSION:
        _invalid("GateSession event payload contract version is invalid")
    session_payload = _plain_mapping(
        payload["session"],
        "GateSession event session",
    )
    try:
        session = parse_gate_session(session_payload)
    except Exception as error:
        raise GateSessionEventV1Error(
            "TBM_GATE_SESSION_EVENT_INVALID",
            "GateSession event contains an invalid revision",
        ) from error
    if payload["session_sha256"] != gate_session_revision_sha256(session):
        _invalid("GateSession event session digest is invalid")
    expected_previous_sha256 = (
        None
        if previous_session is None
        else gate_session_revision_sha256(previous_session)
    )
    if payload["previous_session_sha256"] != expected_previous_sha256:
        _invalid("GateSession event previous revision digest is invalid")
    if (
        payload["transition_authorization_event_id"]
        != event.authorization_decision_id
    ):
        _invalid("GateSession event authorization linkage is invalid")
    if event.stream_id != session.session_id or event.stream_version != session.version:
        _invalid("GateSession event stream identity is invalid")
    _verify_event_identity(session, event)
    if gate_session_event_type(session, previous_session) != event.event_type:
        _invalid("GateSession event type does not match its revision")
    _verify_deterministic_envelope(session, event, payload)
    if (previous_session is None) != (parent_event is None):
        if parent_event is not None:
            _invalid("first GateSession event cannot have a parent")
    if parent_event is not None:
        _verify_parent_revision(parent_event, cast(GateSession, previous_session))
        try:
            verify_event_parent(event, parent_event)
        except Exception as error:
            raise GateSessionEventV1Error(
                "TBM_GATE_SESSION_EVENT_INVALID",
                "GateSession event parent chain is invalid",
            ) from error
    _verify_revision_transition(previous_session, session)
    return session


def _verify_parent_revision(
    parent_event: CanonicalEvent,
    previous_session: GateSession,
) -> None:
    if type(parent_event) is not CanonicalEvent:
        _invalid("GateSession event parent is invalid")
    if (
        parent_event.event_type not in _plausible_event_types(previous_session)
        or parent_event.event_version != GATE_SESSION_EVENT_VERSION
        or parent_event.event_kind != "domain"
        or parent_event.origin != "native"
        or parent_event.source is not None
        or parent_event.stream_id != previous_session.session_id
        or parent_event.stream_type != GATE_SESSION_EVENT_STREAM_TYPE
        or parent_event.stream_version != previous_session.version
        or parent_event.payload_schema != f"{parent_event.event_type}.v1"
        or parent_event.classification != "internal"
        or parent_event.retention_policy_id
        != GATE_SESSION_EVENT_RETENTION_POLICY_ID
        or parent_event.artifact_refs
    ):
        _invalid("GateSession event parent is invalid")
    payload = _plain_mapping(
        parent_event.payload,
        "GateSession parent event payload",
    )
    if set(payload) != {
        "contract_version",
        "session",
        "session_sha256",
        "previous_session_sha256",
        "transition_authorization_event_id",
    }:
        _invalid("GateSession parent event payload fields are invalid")
    if payload["contract_version"] != GATE_SESSION_EVENT_CONTRACT_VERSION:
        _invalid("GateSession parent event contract version is invalid")
    parent_session_payload = _plain_mapping(
        payload["session"],
        "GateSession parent event session",
    )
    try:
        parent_session = parse_gate_session(parent_session_payload)
    except Exception as error:
        raise GateSessionEventV1Error(
            "TBM_GATE_SESSION_EVENT_INVALID",
            "GateSession parent event contains an invalid revision",
        ) from error
    if parent_session != previous_session:
        _invalid("GateSession parent does not contain the previous revision")
    if payload["session_sha256"] != gate_session_revision_sha256(parent_session):
        _invalid("GateSession parent revision digest is invalid")
    prior_digest = payload["previous_session_sha256"]
    if parent_session.version == 1:
        if prior_digest is not None:
            _invalid("first GateSession parent cannot name a prior revision")
    elif not _is_digest(prior_digest):
        _invalid("GateSession parent prior revision digest is invalid")
    if (
        payload["transition_authorization_event_id"]
        != parent_event.authorization_decision_id
    ):
        _invalid("GateSession parent authorization linkage is invalid")
    _verify_event_identity(parent_session, parent_event)
    _verify_deterministic_envelope(parent_session, parent_event, payload)


def _plausible_event_types(session: GateSession) -> frozenset[str]:
    if session.version == 1:
        return frozenset({GATE_SESSION_CREATED_EVENT})
    event_types = {_STATUS_EVENT_TYPES[session.status]}
    if session.status in {
        "prepared",
        "awaiting_decision",
        "decided",
        "finalized",
        "executing",
    }:
        event_types.add(GATE_SESSION_LEASE_RENEWED_EVENT)
    return frozenset(event_types)


def _verify_deterministic_envelope(
    session: GateSession,
    event: CanonicalEvent,
    payload: Mapping[str, object],
) -> None:
    identity_sha256 = _event_identity_sha256(
        session.session_id,
        session.version,
    )
    expected_event_id = "evt_gate_" + identity_sha256.removeprefix("sha256:")
    expected_request_id = (
        "gate_request_" + identity_sha256.removeprefix("sha256:")[:48]
    )
    expected_correlation_id = (
        "gate_correlation_"
        + _domain_sha256(
            b"tbm.gate-session-correlation.v1\x00",
            {"session_id": session.session_id},
        ).removeprefix("sha256:")[:40]
    )
    expected_causation_id = (
        None
        if session.version == 1
        else "evt_gate_"
        + _event_identity_sha256(
            session.session_id,
            session.version - 1,
        ).removeprefix("sha256:")
    )
    expected_timestamp = canonical_rfc3339(session.updated_at)
    if (
        event.event_id != expected_event_id
        or event.request_id != expected_request_id
        or event.idempotency_key_sha256 != identity_sha256
        or event.request_sha256
        != _event_command_sha256(
            event.event_type,
            payload,
            event.authorization_decision_id,
        )
        or event.correlation_id != expected_correlation_id
        or event.causation_id != expected_causation_id
        or event.occurred_at != expected_timestamp
        or event.recorded_at != expected_timestamp
        or event.producer != GATE_SESSION_EVENT_PRODUCER
        or event.producer_version != GATE_SESSION_EVENT_PRODUCER_VERSION
    ):
        _invalid("GateSession event deterministic envelope is invalid")


def _event_identity_sha256(session_id: str, session_version: int) -> str:
    return _domain_sha256(
        b"tbm.gate-session-event-identity.v1\x00",
        {
            "session_id": session_id,
            "session_version": session_version,
        },
    )


def _event_command_sha256(
    event_type: str,
    payload: Mapping[str, object],
    authorization_decision_id: str,
) -> str:
    return _domain_sha256(
        b"tbm.gate-session-event-command.v1\x00",
        {
            "event_type": event_type,
            "payload": payload,
            "authorization_decision_id": authorization_decision_id,
        },
    )


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _verify_revision_transition(
    previous_session: GateSession | None,
    session: GateSession,
) -> None:
    if previous_session is None:
        return
    if session.status == previous_session.status:
        try:
            expected = renew_gate_session_lease(
                previous_session,
                expected_version=previous_session.version,
                updated_at=session.updated_at,
                lease_expires_at=cast(str, session.lease_expires_at),
            )
        except Exception as error:
            raise GateSessionEventV1Error(
                "TBM_GATE_SESSION_EVENT_INVALID",
                "GateSession lease-renewal event is invalid",
            ) from error
    else:
        kwargs: dict[str, object] = {}
        if session.status == "prepared":
            kwargs.update(
                lease_expires_at=session.lease_expires_at,
                retrieval_snapshot_id=session.retrieval_snapshot_id,
                system_gate_evaluation_id=session.system_gate_evaluation_id,
            )
        elif session.status == "decided":
            kwargs.update(
                semantic_gate_attempt_ids=session.semantic_gate_attempt_ids,
                decision_id=session.decision_id,
            )
        elif session.status == "finalized":
            kwargs.update(
                final_memory_revision_ids=session.final_memory_revision_ids,
                injection_artifact_id=session.injection_artifact_id,
                usage_decision_id=session.usage_decision_id,
            )
        elif session.status == "completed":
            kwargs["run_outcome_id"] = session.run_outcome_id
        elif session.status in {"canceled", "expired", "abandoned"}:
            kwargs["terminal_reason"] = session.terminal_reason
        try:
            expected = transition_gate_session(
                previous_session,
                session.status,
                expected_version=previous_session.version,
                updated_at=session.updated_at,
                **kwargs,
            )
        except Exception as error:
            raise GateSessionEventV1Error(
                "TBM_GATE_SESSION_EVENT_INVALID",
                "GateSession transition event is invalid",
            ) from error
    if expected != session:
        _invalid("GateSession event revision is not the exact domain transition")


def _verify_context(
    session: GateSession,
    trusted_context: EventTrustedContext,
) -> None:
    for session_name, context_name in (
        ("tenant_id", "tenant_id"),
        ("repository_id", "repository_id"),
        ("principal_id", "principal_id"),
        ("agent_client_id", "agent_client_id"),
    ):
        if getattr(session, session_name) != getattr(trusted_context, context_name):
            _invalid("GateSession and trusted event context do not match")


def _verify_event_identity(session: GateSession, event: CanonicalEvent) -> None:
    for session_name, event_name in (
        ("tenant_id", "tenant_id"),
        ("repository_id", "repository_id"),
        ("principal_id", "principal_id"),
        ("agent_client_id", "agent_client_id"),
    ):
        if getattr(session, session_name) != getattr(event, event_name):
            _invalid("GateSession event trusted identity is invalid")


def _plain_mapping(value: object, name: str) -> dict[str, object]:
    plain = _thaw_json(value)
    if type(plain) is not dict:
        _invalid(f"{name} must be an object")
    return cast(dict[str, object], plain)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise GateSessionEventV1Error(
            "TBM_GATE_SESSION_EVENT_INVALID",
            "GateSession event value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise GateSessionEventV1Error("TBM_GATE_SESSION_EVENT_INVALID", message)


__all__ = [
    "EXECUTION_ABANDONED_EVENT",
    "EXECUTION_STARTED_EVENT",
    "GATE_SESSION_CANCELED_EVENT",
    "GATE_SESSION_COMPLETED_EVENT",
    "GATE_SESSION_CREATED_EVENT",
    "GATE_SESSION_EVENT_CONTRACT_VERSION",
    "GATE_SESSION_EVENT_PRODUCER",
    "GATE_SESSION_EVENT_PRODUCER_VERSION",
    "GATE_SESSION_EVENT_RETENTION_POLICY_ID",
    "GATE_SESSION_EVENT_STREAM_TYPE",
    "GATE_SESSION_EVENT_TYPES",
    "GATE_SESSION_EVENT_VERSION",
    "GATE_SESSION_EXPIRED_EVENT",
    "GATE_SESSION_LEASE_RENEWED_EVENT",
    "GATE_SESSION_PREPARED_EVENT",
    "SEMANTIC_GATE_DECIDED_EVENT",
    "SEMANTIC_GATE_REQUESTED_EVENT",
    "USAGE_DECISION_FINALIZED_EVENT",
    "GateSessionEventV1Error",
    "build_gate_session_event",
    "gate_session_event_payload_schema",
    "gate_session_event_id",
    "gate_session_event_type",
    "gate_session_revision_sha256",
    "parse_gate_session_event",
]
