from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.audit_v3 import (
    AUDIT_EVENT_CONTRACT_VERSION,
    AUDIT_JSON_MAX_BYTES,
    AUDIT_MAX_REFERENCES,
    AUDIT_MAX_SEQUENCE,
    RECOVERY_ACTION_CONTRACT_VERSION,
    AuditContractError,
    AuditEvent,
    AuditReference,
    RecoveryAction,
    audit_event_id,
    build_audit_event,
    build_recovery_action,
    dumps_audit_event,
    dumps_recovery_action,
    loads_audit_event,
    loads_recovery_action,
    parse_audit_event,
    parse_recovery_action,
    recovery_action_id,
    verify_audit_event_parent,
    verify_recovery_action,
)
from trace_backed_memory.gate_session_v3 import (
    create_gate_session,
    transition_gate_session,
)
from trace_backed_memory.models import MemoryRunRemediation


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
ROOT = Path(__file__).resolve().parents[1]


def _event(**overrides) -> AuditEvent:
    values = {
        "stream_id": "audit_stream_001",
        "sequence": 1,
        "previous_event_id": None,
        "tenant_id": "tenant_001",
        "repository_id": "repository_001",
        "session_id": "gate_session_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "actor_type": "service",
        "actor_id": "tbmd",
        "event_type": "session_created",
        "reason_code": "SESSION_CREATED",
        "payload_sha256": DIGEST_A,
        "references": (),
        "occurred_at": "2026-07-27T00:00:00Z",
    }
    values.update(overrides)
    return build_audit_event(**values)


def _session():
    return create_gate_session(
        session_id="gate_session_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=DIGEST_A,
        idempotency_key="request-001",
        created_at="2026-07-27T00:00:00Z",
        expires_at="2026-07-27T01:00:00Z",
    )


def _remediation(**overrides):
    values = {
        "decision_id": "usage_decision_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "status": "trace_only",
        "action": "recover",
        "trace_eval_result": "pass",
        "decision_eval_result": None,
        "memory_caused_failure": False,
        "resolved_eval_result": "pass",
        "resolved_memory_caused_failure": False,
    }
    values.update(overrides)
    return MemoryRunRemediation(**values)


def _completed_remediation(**overrides):
    values = {
        "decision_id": "usage_decision_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "status": "complete",
        "action": "none",
        "trace_eval_result": "pass",
        "decision_eval_result": "pass",
        "memory_caused_failure": False,
        "resolved_eval_result": "pass",
        "resolved_memory_caused_failure": False,
    }
    values.update(overrides)
    return MemoryRunRemediation(**values)


def _canceled_session():
    return transition_gate_session(
        _session(),
        "canceled",
        expected_version=1,
        updated_at="2026-07-27T00:10:01Z",
        terminal_reason="operator canceled stale request",
    )


def _recovery(**overrides) -> RecoveryAction:
    values = {
        "target_kind": "memory_run",
        "action": "recover",
        "result": "succeeded",
        "session_id": "gate_session_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "usage_decision_id": "usage_decision_001",
        "expected_session_version": None,
        "expected_memory_run_status": "trace_only",
        "memory_caused_failure": None,
        "request_sha256": DIGEST_A,
        "requested_by_principal_id": "principal_001",
        "executor_id": "recovery_worker",
        "error_code": None,
        "started_at": "2026-07-27T00:10:00Z",
        "finished_at": "2026-07-27T00:10:01Z",
    }
    values.update(overrides)
    return build_recovery_action(**values)


def _recovery_event(recovery: RecoveryAction, **overrides) -> AuditEvent:
    values = {
        "event_type": (
            "recovery_succeeded"
            if recovery.result == "succeeded"
            else "recovery_failed"
        ),
        "reason_code": "RECOVERY_COMPLETED",
        "actor_type": "worker",
        "actor_id": recovery.executor_id,
        "references": (
            AuditReference("recovery_action", recovery.recovery_action_id),
        ),
        "occurred_at": "2026-07-27T00:10:01Z",
    }
    values.update(overrides)
    return _event(**values)


def test_audit_event_is_content_addressed_canonical_and_immutable():
    first = _event(
        references=(
            AuditReference("usage_decision", "usage_001"),
            AuditReference("decision", "decision_001"),
        )
    )
    second = _event(
        references=(
            AuditReference("decision", "decision_001"),
            AuditReference("usage_decision", "usage_001"),
        )
    )
    assert first == second
    assert first.contract_version == AUDIT_EVENT_CONTRACT_VERSION
    assert first.event_id == audit_event_id(first.to_dict(include_id=False))
    assert loads_audit_event(dumps_audit_event(first)) == first
    with pytest.raises(FrozenInstanceError):
        first.sequence = 2  # type: ignore[misc]


def test_audit_parent_verifier_requires_exact_monotonic_chain():
    parent = _event()
    child = _event(
        sequence=2,
        previous_event_id=parent.event_id,
        event_type="authorization_evaluated",
        reason_code="AUTHORIZED",
        occurred_at="2026-07-27T00:00:01Z",
    )
    verify_audit_event_parent(parent, None)
    verify_audit_event_parent(child, parent)
    with pytest.raises(AuditContractError, match="stream_id"):
        verify_audit_event_parent(
            _event(
                stream_id="other",
                sequence=2,
                previous_event_id=parent.event_id,
                occurred_at="2026-07-27T00:00:01Z",
            ),
            parent,
        )
    with pytest.raises(AuditContractError, match="trace_id"):
        verify_audit_event_parent(
            _event(
                sequence=2,
                previous_event_id=parent.event_id,
                trace_id="other_trace",
                occurred_at="2026-07-27T00:00:01Z",
            ),
            parent,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sequence": 0}, "sequence"),
        ({"sequence": AUDIT_MAX_SEQUENCE + 1}, "sequence"),
        ({"previous_event_id": "bad"}, "first"),
        ({"actor_type": "model"}, "actor_type"),
        ({"event_type": "arbitrary"}, "event_type"),
        ({"reason_code": ""}, "reason_code"),
        ({"payload_sha256": "bad"}, "payload_sha256"),
        ({"references": (AuditReference("decision", "x"),) * 2}, "unique"),
        ({"occurred_at": "2026-07-27T00:00:00+00:00"}, "canonical"),
    ],
)
def test_audit_event_rejects_invalid_shapes(changes, message):
    with pytest.raises(AuditContractError, match=message):
        replace(_event(), **changes)


def test_nonfirst_event_requires_parent_identifier():
    with pytest.raises(AuditContractError, match="previous_event_id"):
        _event(sequence=2)


def test_audit_hash_detects_tampering():
    with pytest.raises(AuditContractError, match="does not match"):
        replace(_event(), reason_code="DIFFERENT")


def test_recovery_action_round_trips_and_is_content_addressed():
    recovery = _recovery()
    assert recovery.contract_version == RECOVERY_ACTION_CONTRACT_VERSION
    assert recovery.recovery_action_id == recovery_action_id(
        recovery.to_dict(include_id=False)
    )
    assert loads_recovery_action(dumps_recovery_action(recovery)) == recovery
    assert parse_recovery_action(recovery.to_dict()) == recovery


def test_memory_run_recovery_matches_derived_remediation_and_event():
    recovery = _recovery()
    event = _recovery_event(recovery)
    verify_recovery_action(
        recovery,
        event,
        _session(),
        _remediation(),
        remediation_after=_completed_remediation(),
    )
    with pytest.raises(AuditContractError, match="stale"):
        verify_recovery_action(
            recovery,
            event,
            _session(),
            _remediation(
                status="decision_only",
                trace_eval_result="unknown",
                decision_eval_result="pass",
            ),
            remediation_after=_completed_remediation(),
        )


@pytest.mark.parametrize(
    "remediation",
    [
        _remediation(
            status="pending",
            action="measure",
            trace_eval_result="unknown",
            decision_eval_result="unknown",
            resolved_eval_result=None,
            resolved_memory_caused_failure=None,
        ),
        _remediation(decision_eval_result="unknown"),
    ],
)
def test_memory_run_recovery_accepts_unknown_as_unevaluated(remediation):
    recovery = _recovery(
        action=remediation.action,
        expected_memory_run_status=remediation.status,
    )
    verify_recovery_action(
        recovery,
        _recovery_event(recovery),
        _session(),
        remediation,
        remediation_after=_completed_remediation(),
    )


def test_memory_run_recovery_rejects_integer_boolean_fields():
    recovery = _recovery()
    with pytest.raises(AuditContractError, match="valid derived"):
        verify_recovery_action(
            recovery,
            _recovery_event(recovery),
            _session(),
            _remediation(memory_caused_failure=0),
            remediation_after=_completed_remediation(),
        )
    with pytest.raises(AuditContractError, match="valid derived"):
        verify_recovery_action(
            recovery,
            _recovery_event(recovery),
            _session(),
            _remediation(resolved_memory_caused_failure=0),
            remediation_after=_completed_remediation(),
        )


def test_recover_with_attribution_requires_explicit_boolean():
    with pytest.raises(AuditContractError, match="memory_caused_failure"):
        _recovery(
            action="recover_with_attribution",
            expected_memory_run_status="trace_only",
        )
    recovery = _recovery(
        action="recover_with_attribution",
        memory_caused_failure=True,
    )
    remediation = _remediation(
        action="recover_with_attribution",
        trace_eval_result="fail",
        resolved_eval_result="fail",
        resolved_memory_caused_failure=None,
    )
    verify_recovery_action(
        recovery,
        _recovery_event(recovery),
        _session(),
        remediation,
        remediation_after=_completed_remediation(
            trace_eval_result="fail",
            decision_eval_result="fail",
            memory_caused_failure=True,
            resolved_eval_result="fail",
            resolved_memory_caused_failure=True,
        ),
    )


def test_gate_session_recovery_requires_exact_version_and_source_status():
    recovery = _recovery(
        target_kind="gate_session",
        action="cancel_session",
        usage_decision_id=None,
        expected_session_version=1,
        expected_memory_run_status=None,
    )
    verify_recovery_action(
        recovery,
        _recovery_event(recovery),
        _session(),
        session_after=_canceled_session(),
    )
    with pytest.raises(AuditContractError, match="stale"):
        stale = _recovery(
            target_kind="gate_session",
            action="cancel_session",
            usage_decision_id=None,
            expected_session_version=2,
            expected_memory_run_status=None,
        )
        verify_recovery_action(
            stale,
            _recovery_event(stale),
            _session(),
            session_after=_canceled_session(),
        )


def test_gate_session_recovery_binds_transition_to_action_time():
    recovery = _recovery(
        target_kind="gate_session",
        action="cancel_session",
        usage_decision_id=None,
        expected_session_version=1,
        expected_memory_run_status=None,
    )
    stale_transition = transition_gate_session(
        _session(),
        "canceled",
        expected_version=1,
        updated_at="2026-07-27T00:00:01Z",
        terminal_reason="operator canceled stale request",
    )
    with pytest.raises(AuditContractError, match="transition time"):
        verify_recovery_action(
            recovery,
            _recovery_event(recovery),
            _session(),
            session_after=stale_transition,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target_kind": "bad"}, "target_kind"),
        ({"action": "cancel_session"}, "memory_run"),
        ({"result": "pending"}, "result"),
        ({"expected_memory_run_status": None}, "expected_memory_run_status"),
        ({"expected_session_version": 1}, "cannot name"),
        ({"memory_caused_failure": True}, "only permitted"),
        ({"result": "failed"}, "error_code"),
        ({"finished_at": "2026-07-27T00:09:59Z"}, "finished_at"),
    ],
)
def test_recovery_action_rejects_invalid_shapes(changes, message):
    with pytest.raises(AuditContractError, match=message):
        replace(_recovery(), **changes)


def test_failed_recovery_records_bounded_error_and_failed_event():
    recovery = _recovery(result="failed", error_code="STALE_STATE")
    event = _recovery_event(recovery)
    verify_recovery_action(
        recovery,
        event,
        _session(),
        _remediation(),
        remediation_after=_remediation(),
    )
    with pytest.raises(AuditContractError, match="only permitted"):
        replace(_recovery(), error_code="unexpected")


def test_recovery_verifier_requires_action_reference():
    recovery = _recovery()
    with pytest.raises(AuditContractError, match="reference"):
        verify_recovery_action(
            recovery,
            _recovery_event(recovery, references=()),
            _session(),
            _remediation(),
            remediation_after=_completed_remediation(),
        )


def test_recovery_verifier_binds_request_principal_and_executor():
    recovery = _recovery()
    after = _completed_remediation()
    with pytest.raises(AuditContractError, match="request_sha256"):
        wrong_request = _recovery(request_sha256=DIGEST_B)
        verify_recovery_action(
            wrong_request,
            _recovery_event(wrong_request),
            _session(),
            _remediation(),
            remediation_after=after,
        )
    with pytest.raises(AuditContractError, match="principal"):
        wrong_principal = _recovery(
            requested_by_principal_id="other_principal"
        )
        verify_recovery_action(
            wrong_principal,
            _recovery_event(wrong_principal),
            _session(),
            _remediation(),
            remediation_after=after,
        )
    with pytest.raises(AuditContractError, match="actor_id"):
        verify_recovery_action(
            recovery,
            _recovery_event(recovery, actor_id="other_executor"),
            _session(),
            _remediation(),
            remediation_after=after,
        )


def test_recovery_verifier_rejects_forged_complete_remediation():
    recovery = _recovery()
    forged = _completed_remediation(
        trace_eval_result="fail",
        decision_eval_result="pass",
        memory_caused_failure=True,
        resolved_eval_result="error",
        resolved_memory_caused_failure=True,
    )
    with pytest.raises(AuditContractError, match="valid derived"):
        verify_recovery_action(
            recovery,
            _recovery_event(recovery),
            _session(),
            _remediation(),
            remediation_after=forged,
        )


def test_audit_json_is_strict_and_bounded():
    duplicate = dumps_audit_event(_event()).replace(
        '"sequence":1', '"sequence":1,"sequence":2'
    )
    for document in (
        duplicate,
        '{"value":NaN}',
        b"\xff",
        " " * (AUDIT_JSON_MAX_BYTES + 1),
    ):
        with pytest.raises(AuditContractError) as error:
            loads_audit_event(document)
        assert error.value.code == "TBM_AUDIT_INVALID_JSON"


def test_audit_parsers_require_exact_fields_and_array_shapes():
    value = _event().to_dict()
    value["extra"] = True
    with pytest.raises(AuditContractError, match="fields"):
        parse_audit_event(value)
    value = _event().to_dict()
    value["references"] = {}
    with pytest.raises(AuditContractError, match="array"):
        parse_audit_event(value)
    recovery = _recovery().to_dict()
    del recovery["request_sha256"]
    with pytest.raises(AuditContractError, match="fields"):
        parse_recovery_action(recovery)


def test_audit_dump_is_canonical_json():
    event = _event()
    assert json.loads(dumps_audit_event(event)) == event.to_dict()


def test_audit_schema_examples_and_public_exports_match_runtime():
    event_value = json.loads(
        (ROOT / "examples" / "audit_event_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    recovery_value = json.loads(
        (ROOT / "examples" / "recovery_action_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    event_schema = json.loads(
        (ROOT / "schemas" / "audit_event_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    recovery_schema = json.loads(
        (ROOT / "schemas" / "recovery_action_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert parse_audit_event(event_value).to_dict() == event_value
    assert parse_recovery_action(recovery_value).to_dict() == recovery_value
    assert set(event_schema["required"]) == set(event_value)
    assert set(recovery_schema["required"]) == set(recovery_value)
    assert tbm.AuditEvent is AuditEvent
    assert tbm.RecoveryAction is RecoveryAction
    for name in (
        "AuditEvent",
        "RecoveryAction",
        "build_audit_event",
        "build_recovery_action",
        "verify_audit_event_parent",
        "verify_recovery_action",
    ):
        assert name in tbm.__all__


def test_audit_value_objects_reject_invalid_contract_and_identifiers():
    with pytest.raises(AuditContractError, match="kind"):
        AuditReference("unsupported", "record_001")  # type: ignore[arg-type]
    for changes, message in (
        ({"contract_version": "wrong"}, "contract_version"),
        ({"event_id": "bad"}, "event_id"),
        ({"stream_id": ""}, "stream_id"),
    ):
        with pytest.raises(AuditContractError, match=message):
            replace(_event(), **changes)
    for changes, message in (
        ({"contract_version": "wrong"}, "contract_version"),
        ({"recovery_action_id": "bad"}, "recovery_action_id"),
        ({"action": "unsupported"}, "action"),
    ):
        with pytest.raises(AuditContractError, match=message):
            replace(_recovery(), **changes)


def test_gate_recovery_shape_rejects_wrong_action_version_and_fields():
    base = {
        "target_kind": "gate_session",
        "action": "cancel_session",
        "usage_decision_id": None,
        "expected_session_version": 1,
        "expected_memory_run_status": None,
    }
    for changes, message in (
        ({"action": "recover"}, "session action"),
        ({"expected_session_version": True}, "expected_session_version"),
        ({"expected_session_version": 0}, "expected_session_version"),
        ({"expected_memory_run_status": "pending"}, "memory-run fields"),
    ):
        with pytest.raises(AuditContractError, match=message):
            _recovery(**(base | changes))
    with pytest.raises(AuditContractError, match="canonical payload"):
        replace(_recovery(), recovery_action_id="recovery_action_sha256_" + "0" * 64)


def test_audit_parent_rejects_every_broken_chain_dimension():
    parent = _event()
    child = _event(
        sequence=2,
        previous_event_id=parent.event_id,
        occurred_at="2026-07-27T00:00:01Z",
    )
    cases = (
        (parent, parent, "first"),
        (child, None, "parent record"),
        (
            _event(
                sequence=2,
                previous_event_id="audit_event_sha256_" + "0" * 64,
                occurred_at="2026-07-27T00:00:01Z",
            ),
            parent,
            "previous_event_id",
        ),
        (_event(sequence=3, previous_event_id=parent.event_id), parent, "advance"),
        (
            _event(
                sequence=2,
                previous_event_id=parent.event_id,
                occurred_at="2026-07-26T23:59:59Z",
            ),
            parent,
            "precedes",
        ),
    )
    for event, supplied_parent, message in cases:
        with pytest.raises(AuditContractError, match=message):
            verify_audit_event_parent(event, supplied_parent)


def test_recovery_verifier_rejects_cross_record_and_missing_state_inputs():
    recovery = _recovery()
    event = _recovery_event(recovery)
    before = _remediation()
    after = _completed_remediation()
    other_session = _recovery(session_id="other_session")
    cases = (
        (_recovery_event(recovery, event_type="recovery_failed"), recovery, _session(), before, after, {}, "result"),
        (_recovery_event(other_session), other_session, _session(), before, after, {}, "session_id"),
        (_recovery_event(recovery, trace_id="other_trace"), recovery, _session(), before, after, {}, "trace_id"),
        (_recovery_event(recovery, tenant_id="other_tenant"), recovery, _session(), before, after, {}, "tenant/repository"),
        (_recovery_event(recovery, occurred_at="2026-07-27T00:10:00Z"), recovery, _session(), before, after, {}, "precedes"),
        (event, recovery, _session(), before, after, {"session_after": _session()}, "cannot contain"),
        (event, recovery, _session(), None, after, {}, "requires remediation"),
        (event, recovery, _session(), before, None, {}, "requires remediation"),
        (event, recovery, _session(), replace(before, decision_id="other_decision"), after, {}, "identity"),
        (event, recovery, _session(), replace(before, action="measure"), after, {}, "derived"),
    )
    for supplied_event, action, session, remediation, completed, kwargs, message in cases:
        with pytest.raises(AuditContractError, match=message):
            verify_recovery_action(
                action,
                supplied_event,
                session,
                remediation,
                remediation_after=completed,
                **kwargs,
            )


def test_gate_recovery_verifier_rejects_invalid_sources_and_outputs():
    recovery = _recovery(
        target_kind="gate_session",
        action="cancel_session",
        usage_decision_id=None,
        expected_session_version=1,
        expected_memory_run_status=None,
    )
    event = _recovery_event(recovery)
    with pytest.raises(AuditContractError, match="cannot use"):
        verify_recovery_action(
            recovery,
            event,
            _session(),
            _remediation(),
            session_after=_canceled_session(),
        )
    with pytest.raises(AuditContractError, match="requires session_after"):
        verify_recovery_action(recovery, event, _session())
    canceled = _canceled_session()
    wrong_source = _recovery(
        target_kind="gate_session",
        action="cancel_session",
        usage_decision_id=None,
        expected_session_version=canceled.version,
        expected_memory_run_status=None,
    )
    with pytest.raises(AuditContractError, match="not valid"):
        verify_recovery_action(
            wrong_source,
            _recovery_event(wrong_source),
            canceled,
            session_after=canceled,
        )
    with pytest.raises(AuditContractError, match="session_after principal_id"):
        verify_recovery_action(
            recovery,
            event,
            _session(),
            session_after=replace(_canceled_session(), principal_id="other"),
        )
    failed = _recovery(
        target_kind="gate_session",
        action="cancel_session",
        result="failed",
        usage_decision_id=None,
        expected_session_version=1,
        expected_memory_run_status=None,
        error_code="STALE_STATE",
    )
    verify_recovery_action(
        failed,
        _recovery_event(failed),
        _session(),
        session_after=_session(),
    )
    with pytest.raises(AuditContractError, match="leave session unchanged"):
        verify_recovery_action(
            failed,
            _recovery_event(failed),
            _session(),
            session_after=_canceled_session(),
        )
    forged = replace(
        _canceled_session(),
        created_at="2026-07-26T23:59:59Z",
    )
    with pytest.raises(AuditContractError, match="expected transition"):
        verify_recovery_action(
            recovery,
            event,
            _session(),
            session_after=forged,
        )


def test_memory_recovery_verifier_rejects_invalid_outputs_and_attribution():
    recovery = _recovery()
    event = _recovery_event(recovery)
    before = _remediation()
    invalid_outputs = (
        (replace(_completed_remediation(), decision_id="other"), "does not match"),
        (_remediation(), "lacks complete"),
    )
    for after, message in invalid_outputs:
        with pytest.raises(AuditContractError, match=message):
            verify_recovery_action(
                recovery,
                event,
                _session(),
                before,
                remediation_after=after,
            )
    failed = _recovery(result="failed", error_code="FAILED")
    with pytest.raises(AuditContractError, match="leave remediation unchanged"):
        verify_recovery_action(
            failed,
            _recovery_event(failed),
            _session(),
            before,
            remediation_after=_completed_remediation(),
        )
    investigate = _recovery(
        action="investigate",
        expected_memory_run_status="conflict",
    )
    conflict = _remediation(
        status="conflict",
        action="investigate",
        trace_eval_result="fail",
        decision_eval_result="pass",
        resolved_eval_result=None,
        resolved_memory_caused_failure=None,
    )
    verify_recovery_action(
        investigate,
        _recovery_event(investigate),
        _session(),
        conflict,
        remediation_after=conflict,
    )
    with pytest.raises(AuditContractError, match="cannot claim"):
        verify_recovery_action(
            investigate,
            _recovery_event(investigate),
            _session(),
            conflict,
            remediation_after=replace(conflict, decision_eval_result="error"),
        )
    attributed = _recovery(
        action="recover_with_attribution",
        memory_caused_failure=True,
    )
    attribution_before = _remediation(
        action="recover_with_attribution",
        trace_eval_result="fail",
        resolved_eval_result="fail",
        resolved_memory_caused_failure=None,
    )
    with pytest.raises(AuditContractError, match="does not match action"):
        verify_recovery_action(
            attributed,
            _recovery_event(attributed),
            _session(),
            attribution_before,
            remediation_after=_completed_remediation(
                trace_eval_result="fail",
                decision_eval_result="fail",
                memory_caused_failure=False,
                resolved_eval_result="fail",
                resolved_memory_caused_failure=False,
            ),
        )


def test_audit_parsers_reject_limits_types_and_nonobjects():
    event = _event().to_dict()
    event["references"] = [{}] * (AUDIT_MAX_REFERENCES + 1)
    with pytest.raises(AuditContractError, match="item limit"):
        parse_audit_event(event)
    event = _event().to_dict()
    event["references"] = [1]
    with pytest.raises(AuditContractError, match="must be an object"):
        parse_audit_event(event)
    recovery = _recovery().to_dict()
    recovery["memory_caused_failure"] = 1
    with pytest.raises(AuditContractError, match="boolean or null"):
        parse_recovery_action(recovery)
    for document in ([], 1, None):
        with pytest.raises(AuditContractError):
            loads_audit_event(json.dumps(document))
    with pytest.raises(AuditContractError, match="str, bytes"):
        loads_audit_event(1)  # type: ignore[arg-type]
    for field, value, message in (
        ("event_id", 1, "string"),
        ("previous_event_id", 1, "string or null"),
        ("sequence", True, "integer"),
    ):
        event = _event().to_dict()
        event[field] = value
        with pytest.raises(AuditContractError, match=message):
            parse_audit_event(event)
    recovery = _recovery().to_dict()
    recovery["expected_session_version"] = True
    with pytest.raises(AuditContractError, match="integer or null"):
        parse_recovery_action(recovery)
