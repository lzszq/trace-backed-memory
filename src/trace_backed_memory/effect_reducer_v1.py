from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import NoReturn, cast

from ._timestamps import parse_rfc3339
from .completion_outbox_v3 import (
    CompletionOutboxDelivery,
    CompletionOutboxEvent,
    parse_completion_outbox_delivery,
    parse_completion_outbox_event,
    verify_completion_outbox_delivery_transition,
)
from .effect_event_v1 import (
    EFFECT_COMPENSATED_EVENT,
    EFFECT_COMPENSATION_REQUESTED_EVENT,
    EFFECT_DEAD_LETTERED_EVENT,
    EFFECT_EVENT_TYPES,
    EFFECT_FAILED_EVENT,
    EFFECT_PROVIDER_TRANSITION_EVENT,
    EFFECT_REQUESTED_EVENT,
    EFFECT_RETRY_SCHEDULED_EVENT,
    EFFECT_STARTED_EVENT,
    EFFECT_SUCCEEDED_EVENT,
    EffectContract,
    EffectDeliveryTransitionRef,
    EffectEventV1Error,
    ProviderEffectTransitionRef,
    parse_effect_compensated_event,
    parse_effect_compensation_requested_event,
    parse_effect_delivery_event,
    parse_effect_requested_event,
    parse_provider_effect_transition_event,
    parse_provider_effect_transition_reference,
)
from .event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from .event_v1 import CanonicalEvent
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
)


EFFECT_QUEUE_REDUCER_ID = "effect-queue"
EFFECT_QUEUE_PROJECTION_NAME = "effect_queue_v1"
EFFECT_QUEUE_PROJECTION_SCHEMA_VERSION = 2

_EVENT_SCOPE_FIELDS = (
    "organization_id",
    "tenant_id",
    "repository_id",
    "environment_id",
    "principal_id",
    "agent_client_id",
    "actor_type",
    "actor_id",
)
_EVENT_AUTHORITY_SCOPE_FIELDS = (
    "organization_id",
    "tenant_id",
    "repository_id",
    "environment_id",
    "principal_id",
    "agent_client_id",
)


@dataclass(frozen=True)
class EffectProjectionAuthority:
    outbox_event: CompletionOutboxEvent
    delivery_history: tuple[CompletionOutboxDelivery, ...]

    def __post_init__(self) -> None:
        if type(self.outbox_event) is not CompletionOutboxEvent:
            _reject("outbox_event must be exactly CompletionOutboxEvent")
        if (
            type(self.delivery_history) is not tuple
            or not self.delivery_history
            or any(
                type(delivery) is not CompletionOutboxDelivery
                for delivery in self.delivery_history
            )
        ):
            _reject("delivery_history must contain exact delivery revisions")
        first = self.delivery_history[0]
        if (
            first.event_id != self.outbox_event.event_id
            or first.version != 1
            or first.status != "pending"
        ):
            _reject("delivery history does not begin with its pending revision")
        try:
            for previous, current in zip(
                self.delivery_history,
                self.delivery_history[1:],
                strict=False,
            ):
                verify_completion_outbox_delivery_transition(previous, current)
        except Exception as error:
            raise ReducerExecutionError(
                "TBM_EFFECT_REDUCER_AUTHORITY_INVALID",
                "delivery authority history is invalid",
            ) from error


def build_effect_queue_reducer() -> FunctionalReducer:
    descriptor = ReducerDescriptor(
        reducer_id=EFFECT_QUEUE_REDUCER_ID,
        reducer_version=3,
        input_event_types=EFFECT_EVENT_TYPES,
        output_projection=EFFECT_QUEUE_PROJECTION_NAME,
        output_schema_version=EFFECT_QUEUE_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "effect-queue",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "linear-effect-stream",
                    "exact-outbox-delivery-history",
                    "failed-before-retry-or-dead-letter",
                    "terminal-effect-monotonicity",
                    "compensation-is-a-new-effect",
                    "compensation-requires-provider-receipt",
                    "provider-receipt-content-addressed",
                    "receipt-after-unknown-requires-reconciliation",
                    "unknown-before-reconciliation",
                    "retry-only-after-not-found",
                    "retry-time-before-next-attempt",
                    "bounded-provider-dead-letter-after-not-found",
                    "provider-transition-same-scope-reauthorization",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1 for event_type in EFFECT_EVENT_TYPES},
    )

    def initial() -> Mapping[str, object]:
        return {"effects": {}, "heads": {}}

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        _validate_typed(reducer_event)
        source = reducer_event.source_event
        effects = _mapping_copy(state.get("effects"), "effects")
        heads = _mapping_copy(state.get("heads"), "heads")

        if source.event_type == EFFECT_REQUESTED_EVENT:
            reference = _parse_requested(source)
            effect = reference.effect
            if effect.effect_id in effects or source.stream_id in heads:
                _reject("EffectRequested appears more than once")
            initial = reference.initial_delivery
            effects[effect.effect_id] = {
                "effect": effect.to_dict(),
                "outbox_event": (
                    None
                    if reference.outbox_event is None
                    else reference.outbox_event.to_dict()
                ),
                "status": "ready",
                "attempt_count": 0 if initial is None else initial.attempt_count,
                "delivery": None if initial is None else initial.to_dict(),
                "delivery_history": (
                    []
                    if initial is None
                    else [_delivery_history_entry(source, initial)]
                ),
                "history": [_history_entry(source)],
                "pending_failure": None,
                "compensation_effect_id": None,
                "compensation_for_effect_id": None,
                "provider_status": "not_started",
                "provider_attempts": [],
                "provider_transitions": [],
                "provider_retry_at": None,
                **_event_metadata(source),
            }
            heads[source.stream_id] = _head(source)
        elif source.event_type in {
            EFFECT_STARTED_EVENT,
            EFFECT_SUCCEEDED_EVENT,
            EFFECT_FAILED_EVENT,
            EFFECT_RETRY_SCHEDULED_EVENT,
            EFFECT_DEAD_LETTERED_EVENT,
        }:
            reference = _parse_delivery(source)
            effect = _effect_entry(effects, reference.effect_id)
            head = _mapping_copy(
                heads.get(source.stream_id),
                "effect stream head",
            )
            if not _head_extends(head, source):
                _reject("effect delivery event has no exact stream parent")
            updated = _apply_delivery_transition(effect, source, reference)
            effects[reference.effect_id] = updated
            heads[source.stream_id] = _head(source)
        elif source.event_type == EFFECT_PROVIDER_TRANSITION_EVENT:
            reference = _parse_provider_transition(source)
            effect = _effect_entry(effects, reference.effect_id)
            head = _mapping_copy(
                heads.get(source.stream_id),
                "provider effect stream head",
            )
            if not _head_extends(
                head,
                source,
                actor_types=frozenset({"service", "worker"}),
                include_authorization=False,
            ):
                _reject("provider effect event has no exact stream parent")
            updated = _apply_provider_transition(effect, source, reference)
            effects[reference.effect_id] = updated
            heads[source.stream_id] = _head(source)
        elif source.event_type == EFFECT_COMPENSATION_REQUESTED_EVENT:
            reference = _parse_compensation_requested(source)
            original = _effect_entry(effects, reference.original_effect_id)
            compensation = reference.compensation_effect
            if (
                compensation.effect_id in effects
                or source.stream_id in heads
                or original.get("status") != "succeeded"
                or not _effect_supports_compensation(original)
                or original.get("event_id")
                != reference.original_terminal_event_id
                or source.causation_id
                != reference.original_terminal_event_id
                or original.get("compensation_effect_id") is not None
                or not _projection_scope_matches_event(
                    original,
                    source,
                    include_authorization=True,
                )
            ):
                _reject("EffectCompensationRequested has no compensable parent")
            original["compensation_effect_id"] = compensation.effect_id
            original_history = _list_copy(original.get("history"), "history")
            original_history.append(_history_entry(source))
            original["history"] = original_history
            effects[reference.original_effect_id] = original
            effects[compensation.effect_id] = {
                "effect": compensation.to_dict(),
                "outbox_event": None,
                "status": "ready",
                "attempt_count": 0,
                "delivery": None,
                "delivery_history": [],
                "history": [_history_entry(source)],
                "pending_failure": None,
                "compensation_effect_id": None,
                "compensation_for_effect_id": reference.original_effect_id,
                "provider_status": "not_started",
                "provider_attempts": [],
                "provider_transitions": [],
                "provider_retry_at": None,
                **_event_metadata(source),
            }
            heads[source.stream_id] = _head(source)
        elif source.event_type == EFFECT_COMPENSATED_EVENT:
            reference = _parse_compensated(source)
            compensation = _effect_entry(
                effects,
                reference.compensation_effect_id,
            )
            original = _effect_entry(effects, reference.original_effect_id)
            head = _mapping_copy(
                heads.get(source.stream_id),
                "compensation stream head",
            )
            compensation_history = _list_copy(
                compensation.get("history"),
                "history",
            )
            request_history = _mapping_copy(
                compensation_history[0] if compensation_history else None,
                "compensation request history",
            )
            if (
                not _head_extends(
                    head,
                    source,
                    actor_types=frozenset({"service", "worker"}),
                    include_authorization=False,
                )
                or compensation.get("status") != "succeeded"
                or compensation.get("provider_status") != "succeeded"
                or request_history.get("event_id")
                != reference.compensation_request_event_id
                or compensation.get("compensation_for_effect_id")
                != reference.original_effect_id
                or original.get("status") != "succeeded"
                or original.get("compensation_effect_id")
                != reference.compensation_effect_id
                or not _projection_scope_matches_event(
                    compensation,
                    source,
                    include_authorization=False,
                )
            ):
                _reject("EffectCompensated has no exact compensation request")
            compensation_history.append(_history_entry(source))
            compensation.update(
                {
                    "status": "succeeded",
                    "history": compensation_history,
                    **_event_metadata(source),
                }
            )
            original_history = _list_copy(original.get("history"), "history")
            original_history.append(_history_entry(source))
            original["status"] = "compensated"
            original["history"] = original_history
            effects[reference.compensation_effect_id] = compensation
            effects[reference.original_effect_id] = original
            heads[source.stream_id] = _head(source)
        else:
            _reject("EffectQueue received an unrelated event")
        return {"effects": effects, "heads": heads}

    return FunctionalReducer(descriptor, initial, transition)


def projected_effect_contract(
    state: Mapping[str, object],
    effect_id: str,
) -> EffectContract:
    effect = _effect_entry_from_state(state, effect_id)
    payload = _mapping_copy(effect.get("effect"), "effect contract")
    try:
        return EffectContract(
            effect_id=cast(str, payload["effect_id"]),
            effect_type=cast(str, payload["effect_type"]),
            idempotency_key=cast(str, payload["idempotency_key"]),
            requested_by_event_id=cast(
                str,
                payload["requested_by_event_id"],
            ),
            input_artifact_sha256=cast(
                str,
                payload["input_artifact_sha256"],
            ),
            authorization_event_id=cast(
                str,
                payload["authorization_event_id"],
            ),
            compensation_supported=cast(
                bool,
                payload["compensation_supported"],
            ),
        )
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_STATE_INVALID",
            "projected effect contract is invalid",
        ) from error


def projected_effect_status(
    state: Mapping[str, object],
    effect_id: str,
) -> str:
    effect = _effect_entry_from_state(state, effect_id)
    status = effect.get("status")
    if status not in {
        "ready",
        "leased",
        "retry",
        "dead_letter",
        "succeeded",
        "compensated",
    }:
        _reject("projected effect status is invalid")
    return cast(str, status)


def projected_provider_effect_status(
    state: Mapping[str, object],
    effect_id: str,
) -> str:
    effect = _effect_entry_from_state(state, effect_id)
    status = effect.get("provider_status")
    if status not in {
        "not_started",
        "in_flight",
        "submitted",
        "unknown",
        "not_found",
        "retry_wait",
        "dead_lettered",
        "succeeded",
    }:
        _reject("projected provider effect status is invalid")
    return cast(str, status)


def projected_provider_effect_transitions(
    state: Mapping[str, object],
    effect_id: str,
) -> tuple[ProviderEffectTransitionRef, ...]:
    effect = _effect_entry_from_state(state, effect_id)
    entries = _list_copy(
        effect.get("provider_transitions"),
        "provider transitions",
    )
    parsed: list[ProviderEffectTransitionRef] = []
    for entry in entries:
        payload = _mapping_copy(entry, "provider transition")
        reference_payload = _mapping_copy(
            payload.get("transition"),
            "provider transition reference",
        )
        try:
            parsed.append(
                parse_provider_effect_transition_reference(reference_payload)
            )
        except EffectEventV1Error as error:
            raise ReducerExecutionError(
                "TBM_EFFECT_REDUCER_STATE_INVALID",
                "projected provider transition is invalid",
            ) from error
    return tuple(parsed)


def projected_completion_outbox_event(
    state: Mapping[str, object],
    effect_id: str,
) -> CompletionOutboxEvent:
    effect = _effect_entry_from_state(state, effect_id)
    payload = _mapping_copy(effect.get("outbox_event"), "outbox event")
    try:
        return parse_completion_outbox_event(payload)
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_STATE_INVALID",
            "projected outbox event is invalid",
        ) from error


def projected_completion_delivery(
    state: Mapping[str, object],
    effect_id: str,
) -> CompletionOutboxDelivery:
    effect = _effect_entry_from_state(state, effect_id)
    payload = _mapping_copy(effect.get("delivery"), "completion delivery")
    try:
        return parse_completion_outbox_delivery(payload)
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_STATE_INVALID",
            "projected completion delivery is invalid",
        ) from error


def verify_effect_projection_parity(
    state: Mapping[str, object],
    authorities: tuple[EffectProjectionAuthority, ...],
    events: tuple[CanonicalEvent, ...],
) -> None:
    if type(authorities) is not tuple or any(
        type(authority) is not EffectProjectionAuthority
        for authority in authorities
    ):
        _reject("effect parity authority input is invalid")
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _reject("effect parity event input is invalid")
    expected = {
        authority.outbox_event.event_id: authority
        for authority in authorities
    }
    if len(expected) != len(authorities):
        _reject("effect parity authority input has duplicates")
    requested: dict[str, CompletionOutboxEvent] = {}
    revisions: dict[str, list[CompletionOutboxDelivery]] = {
        effect_id: [] for effect_id in expected
    }
    failed_dispositions: dict[str, set[str]] = {
        effect_id: set() for effect_id in expected
    }
    for event in events:
        if event.event_type == EFFECT_REQUESTED_EVENT:
            reference = _parse_requested(event)
            if reference.outbox_event is None or reference.initial_delivery is None:
                _reject("effect parity contains a non-outbox request")
            effect_id = reference.effect.effect_id
            if effect_id in requested or effect_id not in expected:
                _reject("effect parity request identities differ")
            requested[effect_id] = reference.outbox_event
            revisions[effect_id].append(reference.initial_delivery)
        elif event.event_type in {
            EFFECT_STARTED_EVENT,
            EFFECT_SUCCEEDED_EVENT,
            EFFECT_RETRY_SCHEDULED_EVENT,
            EFFECT_DEAD_LETTERED_EVENT,
        }:
            reference = _parse_delivery(event)
            if reference.effect_id not in expected:
                _reject("effect parity contains an unrelated delivery")
            revisions[reference.effect_id].append(reference.delivery)
            if event.event_type in {
                EFFECT_RETRY_SCHEDULED_EVENT,
                EFFECT_DEAD_LETTERED_EVENT,
            }:
                failed_dispositions[reference.effect_id].discard(
                    reference.delivery.delivery_revision_id
                )
        elif event.event_type == EFFECT_FAILED_EVENT:
            reference = _parse_delivery(event)
            if reference.effect_id not in expected:
                _reject("effect parity contains an unrelated failure")
            failed_dispositions[reference.effect_id].add(
                reference.delivery.delivery_revision_id
            )
        elif event.event_type == EFFECT_PROVIDER_TRANSITION_EVENT:
            reference = _parse_provider_transition(event)
            if reference.effect_id not in expected:
                _reject("effect parity contains an unrelated provider transition")
        else:
            _reject("effect authority parity cannot include compensation events")
    if set(requested) != set(expected):
        _reject("effect request identities differ from authority rows")
    for effect_id, authority in expected.items():
        if (
            requested[effect_id] != authority.outbox_event
            or tuple(revisions[effect_id]) != authority.delivery_history
            or projected_completion_outbox_event(state, effect_id)
            != authority.outbox_event
            or projected_completion_delivery(state, effect_id)
            != authority.delivery_history[-1]
            or failed_dispositions[effect_id]
        ):
            _reject("EffectQueue differs from exact outbox authority rows")
    expected_state = _reduce_events(events)
    if _plain_json(state) != _plain_json(expected_state):
        _reject("EffectQueue differs from canonical effect replay")


def _apply_delivery_transition(
    effect: dict[str, object],
    source: CanonicalEvent,
    reference: EffectDeliveryTransitionRef,
) -> dict[str, object]:
    current_delivery_payload = _mapping_copy(
        effect.get("delivery"),
        "projected completion delivery",
    )
    try:
        current_delivery = parse_completion_outbox_delivery(
            current_delivery_payload
        )
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_STATE_INVALID",
            "projected completion delivery is invalid",
        ) from error
    history = _list_copy(effect.get("history"), "history")
    delivery_history = _list_copy(
        effect.get("delivery_history"),
        "delivery_history",
    )
    pending = effect.get("pending_failure")
    status = effect.get("status")
    if reference.previous_delivery != current_delivery:
        _reject("effect delivery transition does not extend the projected row")

    if source.event_type == EFFECT_FAILED_EVENT:
        if status != "leased" or pending is not None:
            _reject("EffectFailed requires one active local delivery lease")
        effect["pending_failure"] = reference.to_dict()
    elif source.event_type in {
        EFFECT_RETRY_SCHEDULED_EVENT,
        EFFECT_DEAD_LETTERED_EVENT,
    }:
        if (
            not isinstance(pending, Mapping)
            or _plain_json(pending) != _plain_json(reference.to_dict())
        ):
            _reject("failure disposition does not match EffectFailed")
        effect["status"] = (
            "retry"
            if source.event_type == EFFECT_RETRY_SCHEDULED_EVENT
            else "dead_letter"
        )
        effect["attempt_count"] = reference.delivery.attempt_count
        effect["delivery"] = reference.delivery.to_dict()
        effect["pending_failure"] = None
        delivery_history.append(
            _delivery_history_entry(source, reference.delivery)
        )
    elif source.event_type == EFFECT_STARTED_EVENT:
        if status not in {"ready", "retry", "leased"} or pending is not None:
            _reject("EffectStarted cannot extend the current effect state")
        effect["status"] = "leased"
        effect["attempt_count"] = reference.delivery.attempt_count
        effect["delivery"] = reference.delivery.to_dict()
        delivery_history.append(
            _delivery_history_entry(source, reference.delivery)
        )
    else:
        if status != "leased" or pending is not None:
            _reject("EffectSucceeded requires one active local delivery lease")
        effect["status"] = "succeeded"
        effect["attempt_count"] = reference.delivery.attempt_count
        effect["delivery"] = reference.delivery.to_dict()
        delivery_history.append(
            _delivery_history_entry(source, reference.delivery)
        )
    history.append(_history_entry(source))
    effect["history"] = history
    effect["delivery_history"] = delivery_history
    effect.update(_event_metadata(source))
    return effect


def _apply_provider_transition(
    effect: dict[str, object],
    source: CanonicalEvent,
    reference: ProviderEffectTransitionRef,
) -> dict[str, object]:
    attempts = _list_copy(effect.get("provider_attempts"), "provider attempts")
    transitions = _list_copy(
        effect.get("provider_transitions"),
        "provider transitions",
    )
    history = _list_copy(effect.get("history"), "history")
    provider_status = effect.get("provider_status")
    contract = _mapping_copy(effect.get("effect"), "effect contract")
    if reference.request_sha256 != contract.get("input_artifact_sha256"):
        _reject("provider request digest differs from the effect contract")
    if effect.get("status") in {"succeeded", "compensated"}:
        _reject("provider transition cannot reopen a terminal effect")
    if effect.get("pending_failure") is not None:
        _reject("provider transition cannot interrupt failure disposition")

    transition_entry = {
        "transition": reference.to_dict(),
        **_event_metadata(source),
    }
    if reference.stage == "attempt_started":
        if (
            provider_status not in {"not_started", "retry_wait"}
            or effect.get("status") == "dead_letter"
            or reference.attempt_sequence != len(attempts) + 1
        ):
            _reject("provider attempt cannot start from the current state")
        if attempts:
            previous_attempt = _mapping_copy(
                attempts[-1],
                "previous provider attempt",
            )
            retry_at = previous_attempt.get("retry_at")
            if (
                previous_attempt.get("status") != "retry_wait"
                or type(retry_at) is not str
            ):
                _reject("provider retry does not follow an explicit schedule")
            try:
                if parse_rfc3339(source.occurred_at) < parse_rfc3339(retry_at):
                    _reject("provider retry started before its scheduled time")
            except ReducerExecutionError:
                raise
            except Exception:
                _reject("provider retry schedule is invalid")
        attempt = {
            "attempt_id": reference.attempt_id,
            "attempt_sequence": reference.attempt_sequence,
            "provider_invocation_id": reference.provider_invocation_id,
            "provider_id": reference.provider_id,
            "model_id": reference.model_id,
            "model_version": reference.model_version,
            "endpoint_id": reference.endpoint_id,
            "request_sha256": reference.request_sha256,
            "status": "in_flight",
            "provider_request_id": None,
            "response_sha256": None,
            "provider_receipt_id": None,
            "error_code": None,
            "retry_at": None,
            "reconciliations": [],
            "history": [transition_entry],
        }
        attempts.append(attempt)
        provider_status = "in_flight"
        effect["provider_retry_at"] = None
    else:
        if not attempts:
            _reject("provider transition requires an existing attempt")
        attempt = _mapping_copy(attempts[-1], "provider attempt")
        _verify_provider_attempt_identity(attempt, reference)
        attempt_history = _list_copy(
            attempt.get("history"),
            "provider attempt history",
        )
        current_status = attempt.get("status")
        retained_request_id = attempt.get("provider_request_id")
        if (
            retained_request_id is not None
            and reference.provider_request_id is not None
            and retained_request_id != reference.provider_request_id
        ):
            _reject("provider request ID changed within one attempt")

        if reference.stage == "request_submitted":
            if current_status != "in_flight":
                _reject("provider submission does not follow attempt start")
            attempt["status"] = "submitted"
            attempt["provider_request_id"] = reference.provider_request_id
            provider_status = "submitted"
        elif reference.stage == "result_unknown":
            if current_status not in {"in_flight", "submitted"}:
                _reject("unknown provider result has no active attempt")
            attempt["status"] = "unknown"
            attempt["provider_request_id"] = (
                retained_request_id
                if reference.provider_request_id is None
                else reference.provider_request_id
            )
            attempt["error_code"] = reference.error_code
            provider_status = "unknown"
        elif reference.stage == "receipt_recorded":
            if current_status not in {"in_flight", "submitted"}:
                _reject("provider receipt has no unresolved attempt")
            attempt["status"] = "succeeded"
            attempt["provider_request_id"] = reference.provider_request_id
            attempt["response_sha256"] = reference.response_sha256
            attempt["provider_receipt_id"] = reference.provider_receipt_id
            attempt["error_code"] = None
            provider_status = "succeeded"
            if effect.get("outbox_event") is None:
                effect["status"] = "succeeded"
        elif reference.stage == "reconciled":
            if current_status != "unknown":
                _reject("provider reconciliation requires an unknown result")
            attempt["provider_request_id"] = (
                retained_request_id
                if reference.provider_request_id is None
                else reference.provider_request_id
            )
            reconciliations = _list_copy(
                attempt.get("reconciliations"),
                "provider reconciliations",
            )
            if reference.reconciliation_sequence != len(reconciliations) + 1:
                _reject("provider reconciliation sequence is not contiguous")
            reconciliations.append(transition_entry)
            attempt["reconciliations"] = reconciliations
            if reference.reconciliation_result == "confirmed":
                attempt["status"] = "succeeded"
                attempt["provider_request_id"] = reference.provider_request_id
                attempt["response_sha256"] = reference.response_sha256
                attempt["provider_receipt_id"] = reference.provider_receipt_id
                attempt["error_code"] = None
                provider_status = "succeeded"
                if effect.get("outbox_event") is None:
                    effect["status"] = "succeeded"
            elif reference.reconciliation_result == "not_found":
                attempt["status"] = "not_found"
                provider_status = "not_found"
            else:
                provider_status = "unknown"
        elif reference.stage == "retry_scheduled":
            if current_status != "not_found" or provider_status != "not_found":
                _reject("provider retry requires a reconciled not-found result")
            attempt["status"] = "retry_wait"
            attempt["retry_at"] = reference.retry_at
            provider_status = "retry_wait"
            effect["provider_retry_at"] = reference.retry_at
        else:
            if current_status != "not_found" or provider_status != "not_found":
                _reject("provider dead-letter requires a not-found result")
            attempt["status"] = "dead_lettered"
            attempt["error_code"] = reference.error_code
            provider_status = "dead_lettered"
            effect["status"] = "dead_letter"
            effect["provider_retry_at"] = None

        attempt_history.append(transition_entry)
        attempt["history"] = attempt_history
        attempts[-1] = attempt

    transitions.append(transition_entry)
    history.append(_history_entry(source))
    effect["provider_status"] = provider_status
    effect["provider_attempts"] = attempts
    effect["provider_transitions"] = transitions
    effect["history"] = history
    effect.update(_event_metadata(source))
    return effect


def _verify_provider_attempt_identity(
    attempt: Mapping[str, object],
    reference: ProviderEffectTransitionRef,
) -> None:
    expected = {
        "attempt_id": reference.attempt_id,
        "attempt_sequence": reference.attempt_sequence,
        "provider_invocation_id": reference.provider_invocation_id,
        "provider_id": reference.provider_id,
        "model_id": reference.model_id,
        "model_version": reference.model_version,
        "endpoint_id": reference.endpoint_id,
        "request_sha256": reference.request_sha256,
    }
    if any(attempt.get(name) != value for name, value in expected.items()):
        _reject("provider attempt identity or provenance changed")


def _effect_supports_compensation(effect: Mapping[str, object]) -> bool:
    contract = effect.get("effect")
    return isinstance(contract, Mapping) and (
        contract.get("compensation_supported") is True
    )


def _effect_entry(
    effects: Mapping[str, object],
    effect_id: str,
) -> dict[str, object]:
    return _mapping_copy(effects.get(effect_id), "effect")


def _effect_entry_from_state(
    state: Mapping[str, object],
    effect_id: str,
) -> dict[str, object]:
    effects = state.get("effects")
    if not isinstance(effects, Mapping) or effect_id not in effects:
        _reject("effect is absent from projection state")
    return _mapping_copy(effects[effect_id], "effect")


def _reduce_events(events: tuple[CanonicalEvent, ...]) -> Mapping[str, object]:
    ordered = tuple(sorted(events, key=lambda event: event.global_position))
    if len({event.global_position for event in ordered}) != len(ordered):
        _reject("effect replay has duplicate global positions")
    reducer = build_effect_queue_reducer()
    state = reducer.initial_state()
    for event in ordered:
        state = reducer.transition(
            state,
            ReducerEvent(event, DEFAULT_EVENT_TYPE_REGISTRY.consume(event)),
        )
    return state


def _validate_typed(reducer_event: ReducerEvent) -> None:
    typed = reducer_event.typed_event
    source = reducer_event.source_event
    if typed is None or (
        typed.target_version != 1
        or typed.event_type != source.event_type
        or _plain_json(typed.payload) != _plain_json(source.payload)
    ):
        _reject("typed effect event is invalid")


def _parse_requested(event: CanonicalEvent):
    try:
        return parse_effect_requested_event(event)
    except EffectEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_EVENT_INVALID",
            "EffectRequested cannot update the projection",
        ) from error


def _parse_delivery(event: CanonicalEvent) -> EffectDeliveryTransitionRef:
    try:
        return parse_effect_delivery_event(event)
    except EffectEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_EVENT_INVALID",
            "effect delivery event cannot update the projection",
        ) from error


def _parse_provider_transition(
    event: CanonicalEvent,
) -> ProviderEffectTransitionRef:
    try:
        return parse_provider_effect_transition_event(event)
    except EffectEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_EVENT_INVALID",
            "provider effect event cannot update the projection",
        ) from error


def _parse_compensation_requested(event: CanonicalEvent):
    try:
        return parse_effect_compensation_requested_event(event)
    except EffectEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_EVENT_INVALID",
            "EffectCompensationRequested cannot update the projection",
        ) from error


def _parse_compensated(event: CanonicalEvent):
    try:
        return parse_effect_compensated_event(event)
    except EffectEventV1Error as error:
        raise ReducerExecutionError(
            "TBM_EFFECT_REDUCER_EVENT_INVALID",
            "EffectCompensated cannot update the projection",
        ) from error


def _head_extends(
    head: Mapping[str, object],
    event: CanonicalEvent,
    *,
    actor_types: frozenset[str] = frozenset({"worker"}),
    include_authorization: bool = True,
) -> bool:
    return (
        head.get("stream_version") == event.stream_version - 1
        and head.get("event_id") == event.causation_id
        and head.get("event_sha256") == event.previous_stream_event_sha256
        and type(head.get("global_position")) is int
        and cast(int, head["global_position"]) < event.global_position
        and _projection_authority_matches_event(
            head,
            event,
            actor_types=actor_types,
            include_authorization=include_authorization,
        )
    )


def _head(event: CanonicalEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        "stream_version": event.stream_version,
        "authorization_decision_id": event.authorization_decision_id,
        **_event_scope(event),
    }


def _event_metadata(event: CanonicalEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_sha256": event.event_sha256,
        "global_position": event.global_position,
        "stream_id": event.stream_id,
        "stream_version": event.stream_version,
        "authorization_decision_id": event.authorization_decision_id,
        **_event_scope(event),
    }


def _history_entry(event: CanonicalEvent) -> dict[str, object]:
    return _event_metadata(event)


def _delivery_history_entry(
    event: CanonicalEvent,
    delivery: CompletionOutboxDelivery,
) -> dict[str, object]:
    return {
        "delivery": delivery.to_dict(),
        **_event_metadata(event),
    }


def _event_scope(event: CanonicalEvent) -> dict[str, object]:
    return {name: getattr(event, name) for name in _EVENT_SCOPE_FIELDS}


def _projection_scope_matches_event(
    projection: Mapping[str, object],
    event: CanonicalEvent,
    *,
    include_authorization: bool,
) -> bool:
    if any(
        projection.get(name) != getattr(event, name)
        for name in _EVENT_SCOPE_FIELDS
    ):
        return False
    return not include_authorization or (
        projection.get("authorization_decision_id")
        == event.authorization_decision_id
    )


def _projection_authority_matches_event(
    projection: Mapping[str, object],
    event: CanonicalEvent,
    *,
    actor_types: frozenset[str],
    include_authorization: bool,
) -> bool:
    return (
        all(
            projection.get(name) == getattr(event, name)
            for name in _EVENT_AUTHORITY_SCOPE_FIELDS
        )
        and (
            not include_authorization
            or projection.get("authorization_decision_id")
            == event.authorization_decision_id
        )
        and event.actor_type in actor_types
    )


def _mapping_copy(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject(f"{name} projection state is invalid")
    copied = _plain_json(value)
    if type(copied) is not dict:
        _reject(f"{name} projection state is invalid")
    return cast(dict[str, object], copied)


def _list_copy(value: object, name: str) -> list[object]:
    copied = _plain_json(value)
    if type(copied) is not list:
        _reject(f"{name} projection state is invalid")
    return copied


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json_bytes(value)
    ).hexdigest()


def _reject(message: str) -> NoReturn:
    raise ReducerExecutionError(
        "TBM_EFFECT_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "EFFECT_QUEUE_PROJECTION_NAME",
    "EFFECT_QUEUE_PROJECTION_SCHEMA_VERSION",
    "EFFECT_QUEUE_REDUCER_ID",
    "EffectProjectionAuthority",
    "build_effect_queue_reducer",
    "projected_completion_delivery",
    "projected_completion_outbox_event",
    "projected_effect_contract",
    "projected_effect_status",
    "projected_provider_effect_status",
    "projected_provider_effect_transitions",
    "verify_effect_projection_parity",
]
