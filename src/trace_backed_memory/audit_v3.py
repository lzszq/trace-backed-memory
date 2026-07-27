from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import canonical_sha256
from .gate_session_v3 import (
    GateSession,
    GateSessionContractError,
    GateSessionStatus,
    transition_gate_session,
)
from .models import MemoryRunRemediation


AUDIT_EVENT_CONTRACT_VERSION = "tbm.audit-event.v3"
RECOVERY_ACTION_CONTRACT_VERSION = "tbm.recovery-action.v3"
AUDIT_JSON_MAX_BYTES = 1024 * 1024
AUDIT_JSON_MAX_DEPTH = 32
AUDIT_JSON_MAX_NODES = 10_000
AUDIT_MAX_REFERENCES = 64
AUDIT_MAX_SEQUENCE = 2_147_483_647

_IDENTIFIER_MAX_CHARS = 128
_CODE_MAX_CHARS = 256
_AUDIT_EVENT_ID_RE = re.compile(r"^audit_event_sha256_[0-9a-f]{64}$")
_RECOVERY_ACTION_ID_RE = re.compile(
    r"^recovery_action_sha256_[0-9a-f]{64}$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_TYPES = frozenset(
    {
        "session_created",
        "session_transitioned",
        "authorization_evaluated",
        "retrieval_recorded",
        "system_gate_evaluated",
        "semantic_gate_attempted",
        "decision_finalized",
        "injection_created",
        "execution_completed",
        "outcome_attributed",
        "recovery_succeeded",
        "recovery_failed",
        "session_canceled",
        "session_expired",
        "session_abandoned",
    }
)
_ACTOR_TYPES = frozenset({"principal", "service", "worker"})
_REFERENCE_KINDS = frozenset(
    {
        "authorization_event",
        "retrieval_snapshot",
        "system_gate_evaluation",
        "semantic_gate_attempt",
        "memory_revision",
        "decision",
        "injection_artifact",
        "usage_decision",
        "run_outcome",
        "outcome_attribution",
        "recovery_action",
    }
)
_TARGET_KINDS = frozenset({"memory_run", "gate_session"})
_RECOVERY_ACTIONS = frozenset(
    {
        "measure",
        "recover",
        "recover_with_attribution",
        "investigate",
        "cancel_session",
        "expire_session",
        "abandon_session",
    }
)
_MEMORY_RUN_ACTIONS = frozenset(
    {"measure", "recover", "recover_with_attribution", "investigate"}
)
_SESSION_ACTIONS = frozenset(
    {"cancel_session", "expire_session", "abandon_session"}
)
_MEMORY_RUN_STATUSES = frozenset(
    {"pending", "trace_only", "decision_only", "complete", "conflict"}
)
_RESULTS = frozenset({"succeeded", "failed"})
_REFERENCE_FIELDS = frozenset({"kind", "record_id"})
_AUDIT_EVENT_FIELDS = frozenset(
    {
        "contract_version",
        "event_id",
        "stream_id",
        "sequence",
        "previous_event_id",
        "tenant_id",
        "repository_id",
        "session_id",
        "trace_id",
        "run_id",
        "actor_type",
        "actor_id",
        "event_type",
        "reason_code",
        "payload_sha256",
        "references",
        "occurred_at",
    }
)
_RECOVERY_FIELDS = frozenset(
    {
        "contract_version",
        "recovery_action_id",
        "target_kind",
        "action",
        "result",
        "session_id",
        "trace_id",
        "run_id",
        "usage_decision_id",
        "expected_session_version",
        "expected_memory_run_status",
        "memory_caused_failure",
        "request_sha256",
        "requested_by_principal_id",
        "executor_id",
        "error_code",
        "started_at",
        "finished_at",
    }
)

AuditActorType = Literal["principal", "service", "worker"]
AuditEventType = Literal[
    "session_created",
    "session_transitioned",
    "authorization_evaluated",
    "retrieval_recorded",
    "system_gate_evaluated",
    "semantic_gate_attempted",
    "decision_finalized",
    "injection_created",
    "execution_completed",
    "outcome_attributed",
    "recovery_succeeded",
    "recovery_failed",
    "session_canceled",
    "session_expired",
    "session_abandoned",
]
AuditReferenceKind = Literal[
    "authorization_event",
    "retrieval_snapshot",
    "system_gate_evaluation",
    "semantic_gate_attempt",
    "memory_revision",
    "decision",
    "injection_artifact",
    "usage_decision",
    "run_outcome",
    "outcome_attribution",
    "recovery_action",
]
RecoveryTargetKind = Literal["memory_run", "gate_session"]
RecoveryActionKind = Literal[
    "measure",
    "recover",
    "recover_with_attribution",
    "investigate",
    "cancel_session",
    "expire_session",
    "abandon_session",
]
RecoveryResult = Literal["succeeded", "failed"]


class AuditContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise AuditContractError("TBM_AUDIT_INVALID", message)


@dataclass(frozen=True)
class AuditReference:
    kind: AuditReferenceKind
    record_id: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _REFERENCE_KINDS:
            _invalid("AuditReference kind is not supported")
        _identifier(self.record_id, "AuditReference record_id")

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "record_id": self.record_id}


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    stream_id: str
    sequence: int
    previous_event_id: str | None
    tenant_id: str
    repository_id: str
    session_id: str
    trace_id: str
    run_id: str
    actor_type: AuditActorType
    actor_id: str
    event_type: AuditEventType
    reason_code: str
    payload_sha256: str
    references: tuple[AuditReference, ...]
    occurred_at: str
    contract_version: str = AUDIT_EVENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != AUDIT_EVENT_CONTRACT_VERSION:
            _invalid(
                f"contract_version must be {AUDIT_EVENT_CONTRACT_VERSION}"
            )
        if type(self.event_id) is not str or not _AUDIT_EVENT_ID_RE.fullmatch(
            self.event_id
        ):
            _invalid("event_id must be a canonical content identifier")
        for name in (
            "stream_id",
            "tenant_id",
            "repository_id",
            "session_id",
            "trace_id",
            "run_id",
            "actor_id",
        ):
            _identifier(getattr(self, name), name)
        if (
            type(self.sequence) is not int
            or not 1 <= self.sequence <= AUDIT_MAX_SEQUENCE
        ):
            _invalid("sequence must be a bounded positive integer")
        if self.sequence == 1:
            if self.previous_event_id is not None:
                _invalid("first AuditEvent cannot name previous_event_id")
        elif (
            type(self.previous_event_id) is not str
            or not _AUDIT_EVENT_ID_RE.fullmatch(self.previous_event_id)
        ):
            _invalid("non-first AuditEvent requires canonical previous_event_id")
        if type(self.actor_type) is not str or self.actor_type not in _ACTOR_TYPES:
            _invalid("actor_type is not supported")
        if type(self.event_type) is not str or self.event_type not in _EVENT_TYPES:
            _invalid("event_type is not supported")
        _code(self.reason_code, "reason_code")
        _digest(self.payload_sha256, "payload_sha256")
        _reference_tuple(self.references)
        _timestamp(self.occurred_at, "occurred_at")
        if self.event_id != audit_event_id(self.to_dict(include_id=False)):
            _invalid("event_id does not match canonical payload")

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "previous_event_id": self.previous_event_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "event_type": self.event_type,
            "reason_code": self.reason_code,
            "payload_sha256": self.payload_sha256,
            "references": [item.to_dict() for item in self.references],
            "occurred_at": self.occurred_at,
        }
        if include_id:
            value["event_id"] = self.event_id
        return value


@dataclass(frozen=True)
class RecoveryAction:
    recovery_action_id: str
    target_kind: RecoveryTargetKind
    action: RecoveryActionKind
    result: RecoveryResult
    session_id: str
    trace_id: str
    run_id: str
    usage_decision_id: str | None
    expected_session_version: int | None
    expected_memory_run_status: str | None
    memory_caused_failure: bool | None
    request_sha256: str
    requested_by_principal_id: str
    executor_id: str
    error_code: str | None
    started_at: str
    finished_at: str
    contract_version: str = RECOVERY_ACTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RECOVERY_ACTION_CONTRACT_VERSION:
            _invalid(
                f"contract_version must be {RECOVERY_ACTION_CONTRACT_VERSION}"
            )
        if (
            type(self.recovery_action_id) is not str
            or not _RECOVERY_ACTION_ID_RE.fullmatch(self.recovery_action_id)
        ):
            _invalid(
                "recovery_action_id must be a canonical content identifier"
            )
        if type(self.target_kind) is not str or self.target_kind not in _TARGET_KINDS:
            _invalid("target_kind is not supported")
        if type(self.action) is not str or self.action not in _RECOVERY_ACTIONS:
            _invalid("action is not supported")
        if type(self.result) is not str or self.result not in _RESULTS:
            _invalid("result must be succeeded or failed")
        for name in (
            "session_id",
            "trace_id",
            "run_id",
            "requested_by_principal_id",
            "executor_id",
        ):
            _identifier(getattr(self, name), name)
        _optional_identifier(self.usage_decision_id, "usage_decision_id")
        _digest(self.request_sha256, "request_sha256")
        if self.result == "failed":
            _code(self.error_code, "error_code")
        elif self.error_code is not None:
            _invalid("error_code is only permitted for failed recovery")
        started = _timestamp(self.started_at, "started_at")
        finished = _timestamp(self.finished_at, "finished_at")
        if finished < started:
            _invalid("finished_at must not precede started_at")
        self._validate_target_shape()
        if self.recovery_action_id != recovery_action_id(
            self.to_dict(include_id=False)
        ):
            _invalid("recovery_action_id does not match canonical payload")

    def _validate_target_shape(self) -> None:
        if self.target_kind == "memory_run":
            if self.action not in _MEMORY_RUN_ACTIONS:
                _invalid("memory_run target requires a memory-run action")
            _identifier(self.usage_decision_id, "usage_decision_id")
            if (
                type(self.expected_memory_run_status) is not str
                or self.expected_memory_run_status not in _MEMORY_RUN_STATUSES
            ):
                _invalid(
                    "memory_run target requires expected_memory_run_status"
                )
            if self.expected_session_version is not None:
                _invalid(
                    "memory_run target cannot name expected_session_version"
                )
            if self.action == "recover_with_attribution":
                if type(self.memory_caused_failure) is not bool:
                    _invalid(
                        "recover_with_attribution requires "
                        "memory_caused_failure"
                    )
            elif self.memory_caused_failure is not None:
                _invalid(
                    "memory_caused_failure is only permitted for "
                    "recover_with_attribution"
                )
        else:
            if self.action not in _SESSION_ACTIONS:
                _invalid("gate_session target requires a session action")
            if (
                type(self.expected_session_version) is not int
                or not 1 <= self.expected_session_version <= AUDIT_MAX_SEQUENCE
            ):
                _invalid(
                    "gate_session target requires expected_session_version"
                )
            if (
                self.expected_memory_run_status is not None
                or self.memory_caused_failure is not None
            ):
                _invalid(
                    "gate_session target cannot contain memory-run fields"
                )

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "target_kind": self.target_kind,
            "action": self.action,
            "result": self.result,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "usage_decision_id": self.usage_decision_id,
            "expected_session_version": self.expected_session_version,
            "expected_memory_run_status": self.expected_memory_run_status,
            "memory_caused_failure": self.memory_caused_failure,
            "request_sha256": self.request_sha256,
            "requested_by_principal_id": self.requested_by_principal_id,
            "executor_id": self.executor_id,
            "error_code": self.error_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_id:
            value["recovery_action_id"] = self.recovery_action_id
        return value


def audit_event_id(payload: Mapping[str, object]) -> str:
    return "audit_event_sha256_" + canonical_sha256(payload).removeprefix(
        "sha256:"
    )


def recovery_action_id(payload: Mapping[str, object]) -> str:
    return "recovery_action_sha256_" + canonical_sha256(payload).removeprefix(
        "sha256:"
    )


def build_audit_event(
    *,
    stream_id: str,
    sequence: int,
    previous_event_id: str | None,
    tenant_id: str,
    repository_id: str,
    session_id: str,
    trace_id: str,
    run_id: str,
    actor_type: AuditActorType,
    actor_id: str,
    event_type: AuditEventType,
    reason_code: str,
    payload_sha256: str,
    references: tuple[AuditReference, ...],
    occurred_at: str,
) -> AuditEvent:
    canonical_references = tuple(
        sorted(references, key=lambda item: (item.kind, item.record_id))
    )
    payload: dict[str, object] = {
        "contract_version": AUDIT_EVENT_CONTRACT_VERSION,
        "stream_id": stream_id,
        "sequence": sequence,
        "previous_event_id": previous_event_id,
        "tenant_id": tenant_id,
        "repository_id": repository_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "event_type": event_type,
        "reason_code": reason_code,
        "payload_sha256": payload_sha256,
        "references": [item.to_dict() for item in canonical_references],
        "occurred_at": canonical_rfc3339(occurred_at),
    }
    return AuditEvent(
        event_id=audit_event_id(payload),
        stream_id=stream_id,
        sequence=sequence,
        previous_event_id=previous_event_id,
        tenant_id=tenant_id,
        repository_id=repository_id,
        session_id=session_id,
        trace_id=trace_id,
        run_id=run_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        reason_code=reason_code,
        payload_sha256=payload_sha256,
        references=canonical_references,
        occurred_at=cast(str, payload["occurred_at"]),
    )


def build_recovery_action(
    *,
    target_kind: RecoveryTargetKind,
    action: RecoveryActionKind,
    result: RecoveryResult,
    session_id: str,
    trace_id: str,
    run_id: str,
    usage_decision_id: str | None,
    expected_session_version: int | None,
    expected_memory_run_status: str | None,
    memory_caused_failure: bool | None,
    request_sha256: str,
    requested_by_principal_id: str,
    executor_id: str,
    error_code: str | None,
    started_at: str,
    finished_at: str,
) -> RecoveryAction:
    payload: dict[str, object] = {
        "contract_version": RECOVERY_ACTION_CONTRACT_VERSION,
        "target_kind": target_kind,
        "action": action,
        "result": result,
        "session_id": session_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "usage_decision_id": usage_decision_id,
        "expected_session_version": expected_session_version,
        "expected_memory_run_status": expected_memory_run_status,
        "memory_caused_failure": memory_caused_failure,
        "request_sha256": request_sha256,
        "requested_by_principal_id": requested_by_principal_id,
        "executor_id": executor_id,
        "error_code": error_code,
        "started_at": canonical_rfc3339(started_at),
        "finished_at": canonical_rfc3339(finished_at),
    }
    return RecoveryAction(
        recovery_action_id=recovery_action_id(payload),
        target_kind=target_kind,
        action=action,
        result=result,
        session_id=session_id,
        trace_id=trace_id,
        run_id=run_id,
        usage_decision_id=usage_decision_id,
        expected_session_version=expected_session_version,
        expected_memory_run_status=expected_memory_run_status,
        memory_caused_failure=memory_caused_failure,
        request_sha256=request_sha256,
        requested_by_principal_id=requested_by_principal_id,
        executor_id=executor_id,
        error_code=error_code,
        started_at=cast(str, payload["started_at"]),
        finished_at=cast(str, payload["finished_at"]),
    )


def verify_audit_event_parent(
    event: AuditEvent,
    parent: AuditEvent | None,
) -> None:
    if event.sequence == 1:
        if parent is not None:
            _invalid("first AuditEvent cannot have a parent")
        return
    if parent is None:
        _invalid("non-first AuditEvent requires its parent record")
    if event.previous_event_id != parent.event_id:
        _invalid("AuditEvent previous_event_id does not match parent")
    if event.sequence != parent.sequence + 1:
        _invalid("AuditEvent sequence must advance by one")
    for name in (
        "stream_id",
        "tenant_id",
        "repository_id",
        "session_id",
        "trace_id",
        "run_id",
    ):
        if getattr(event, name) != getattr(parent, name):
            _invalid(f"AuditEvent parent {name} does not match")
    if parse_rfc3339(event.occurred_at) < parse_rfc3339(parent.occurred_at):
        _invalid("AuditEvent occurred_at precedes parent")


def verify_recovery_action(
    recovery: RecoveryAction,
    event: AuditEvent,
    session_before: GateSession,
    remediation_before: MemoryRunRemediation | None = None,
    *,
    session_after: GateSession | None = None,
    remediation_after: MemoryRunRemediation | None = None,
) -> None:
    if event.event_type != (
        "recovery_succeeded"
        if recovery.result == "succeeded"
        else "recovery_failed"
    ):
        _invalid("RecoveryAction result does not match AuditEvent type")
    if not any(
        reference.kind == "recovery_action"
        and reference.record_id == recovery.recovery_action_id
        for reference in event.references
    ):
        _invalid("AuditEvent must reference the RecoveryAction")
    if event.actor_id != recovery.executor_id:
        _invalid("AuditEvent actor_id must match RecoveryAction executor_id")
    for name in ("session_id", "trace_id", "run_id"):
        if getattr(recovery, name) != getattr(session_before, name):
            _invalid(f"RecoveryAction {name} does not match GateSession")
        if getattr(event, name) != getattr(session_before, name):
            _invalid(f"AuditEvent {name} does not match GateSession")
    if (
        event.tenant_id != session_before.tenant_id
        or event.repository_id != session_before.repository_id
    ):
        _invalid("AuditEvent tenant/repository does not match GateSession")
    if recovery.request_sha256 != session_before.request_fingerprint:
        _invalid("RecoveryAction request_sha256 does not match GateSession")
    if (
        recovery.requested_by_principal_id
        != session_before.principal_id
    ):
        _invalid(
            "RecoveryAction requested principal does not match GateSession"
        )
    if parse_rfc3339(event.occurred_at) < parse_rfc3339(recovery.finished_at):
        _invalid("AuditEvent occurred_at precedes RecoveryAction finish")

    if recovery.target_kind == "gate_session":
        if remediation_before is not None or remediation_after is not None:
            _invalid("gate_session recovery cannot use memory remediation")
        if recovery.expected_session_version != session_before.version:
            _invalid("RecoveryAction expected_session_version is stale")
        allowed_sources = {
            "cancel_session": {"created", "prepared", "awaiting_decision"},
            "expire_session": {"prepared", "awaiting_decision"},
            "abandon_session": {"executing"},
        }
        if session_before.status not in allowed_sources[recovery.action]:
            _invalid("RecoveryAction is not valid for GateSession status")
        if session_after is None:
            _invalid("gate_session recovery requires session_after")
        _verify_session_recovery_result(
            recovery,
            session_before,
            session_after,
        )
    else:
        if session_after is not None:
            _invalid("memory_run recovery cannot contain session_after")
        if remediation_before is None or remediation_after is None:
            _invalid("memory_run recovery requires remediation")
        _validate_remediation(remediation_before, "remediation_before")
        _validate_remediation(remediation_after, "remediation_after")
        if (
            recovery.usage_decision_id != remediation_before.decision_id
            or recovery.trace_id != remediation_before.trace_id
            or recovery.run_id != remediation_before.run_id
        ):
            _invalid("RecoveryAction does not match remediation identity")
        if recovery.expected_memory_run_status != remediation_before.status:
            _invalid("RecoveryAction memory-run status is stale")
        if recovery.action != remediation_before.action:
            _invalid("RecoveryAction does not match derived remediation")
        if (
            recovery.action == "recover_with_attribution"
            and remediation_before.resolved_memory_caused_failure is not None
            and recovery.memory_caused_failure
            != remediation_before.resolved_memory_caused_failure
        ):
            _invalid("RecoveryAction attribution does not match remediation")
        _verify_memory_recovery_result(
            recovery,
            remediation_before,
            remediation_after,
        )


def _verify_session_recovery_result(
    recovery: RecoveryAction,
    before: GateSession,
    after: GateSession,
) -> None:
    for name in (
        "session_id",
        "tenant_id",
        "repository_id",
        "principal_id",
        "agent_client_id",
        "trace_id",
        "run_id",
    ):
        if getattr(after, name) != getattr(before, name):
            _invalid(f"session_after {name} does not match session_before")
    if recovery.result == "failed":
        if after != before:
            _invalid("failed session recovery must leave session unchanged")
        return
    transition_time = parse_rfc3339(after.updated_at)
    if not (
        parse_rfc3339(recovery.started_at)
        <= transition_time
        <= parse_rfc3339(recovery.finished_at)
    ):
        _invalid(
            "succeeded session recovery transition time is outside action"
        )
    target_status = cast(
        GateSessionStatus,
        {
        "cancel_session": "canceled",
        "expire_session": "expired",
        "abandon_session": "abandoned",
        }[recovery.action],
    )
    try:
        expected = transition_gate_session(
            before,
            target_status,
            expected_version=before.version,
            updated_at=after.updated_at,
            terminal_reason=after.terminal_reason,
        )
    except GateSessionContractError as exc:
        raise AuditContractError(
            "TBM_AUDIT_INVALID",
            "succeeded session recovery is not a valid transition",
        ) from exc
    if after != expected:
        _invalid("succeeded session recovery lacks expected transition")


def _verify_memory_recovery_result(
    recovery: RecoveryAction,
    before: MemoryRunRemediation,
    after: MemoryRunRemediation,
) -> None:
    for name in ("decision_id", "trace_id", "run_id"):
        if getattr(after, name) != getattr(before, name):
            _invalid(f"remediation_after {name} does not match before")
    if recovery.result == "failed":
        if after != before:
            _invalid("failed memory recovery must leave remediation unchanged")
        return
    if recovery.action == "investigate":
        if after != before:
            _invalid("investigate action cannot claim a lifecycle transition")
        return
    if after.status != "complete" or after.action != "none":
        _invalid("succeeded memory recovery lacks complete remediation")
    if recovery.action == "recover":
        if (
            after.resolved_eval_result != before.resolved_eval_result
            or after.resolved_memory_caused_failure
            != before.resolved_memory_caused_failure
        ):
            _invalid("completed remediation differs from derived recovery")
    if recovery.action == "recover_with_attribution" and (
        after.memory_caused_failure != recovery.memory_caused_failure
    ):
        _invalid("completed remediation attribution does not match action")


def _validate_remediation(
    value: MemoryRunRemediation,
    label: str,
) -> None:
    measured = {"pass", "fail", "error"}
    if value.status == "pending":
        valid = (
            value.action == "measure"
            and value.trace_eval_result == "unknown"
            and value.decision_eval_result in (None, "unknown")
            and value.resolved_eval_result is None
            and value.resolved_memory_caused_failure is None
        )
    elif value.status == "trace_only":
        valid = (
            value.trace_eval_result in measured
            and value.decision_eval_result in (None, "unknown")
            and value.resolved_eval_result == value.trace_eval_result
            and (
                (
                    value.trace_eval_result == "pass"
                    and value.action == "recover"
                    and value.resolved_memory_caused_failure is False
                )
                or (
                    value.trace_eval_result in {"fail", "error"}
                    and value.action == "recover_with_attribution"
                    and value.resolved_memory_caused_failure is None
                )
            )
        )
    elif value.status == "decision_only":
        valid = (
            value.action == "recover"
            and value.trace_eval_result == "unknown"
            and value.decision_eval_result in measured
            and value.resolved_eval_result == value.decision_eval_result
            and value.resolved_memory_caused_failure
            == value.memory_caused_failure
        )
    elif value.status == "complete":
        valid = (
            value.action == "none"
            and value.trace_eval_result in measured
            and value.decision_eval_result == value.trace_eval_result
            and value.resolved_eval_result == value.trace_eval_result
            and value.resolved_memory_caused_failure
            == value.memory_caused_failure
        )
    else:
        valid = (
            value.status == "conflict"
            and value.action == "investigate"
            and value.trace_eval_result in measured
            and value.decision_eval_result in measured
            and value.trace_eval_result != value.decision_eval_result
            and value.resolved_eval_result is None
            and value.resolved_memory_caused_failure is None
        )
    if (
        type(value.memory_caused_failure) is not bool
        or (
            value.resolved_memory_caused_failure is not None
            and type(value.resolved_memory_caused_failure) is not bool
        )
    ):
        valid = False
    if value.memory_caused_failure and (
        value.decision_eval_result not in {"fail", "error"}
    ):
        valid = False
    if not valid:
        _invalid(f"{label} is not a valid derived remediation")


def dumps_audit_event(event: AuditEvent) -> str:
    return _dumps(event.to_dict())


def dumps_recovery_action(recovery: RecoveryAction) -> str:
    return _dumps(recovery.to_dict())


def loads_audit_event(data: str | bytes | bytearray) -> AuditEvent:
    return parse_audit_event(_loads_object(data))


def loads_recovery_action(data: str | bytes | bytearray) -> RecoveryAction:
    return parse_recovery_action(_loads_object(data))


def parse_audit_event(value: Mapping[str, object]) -> AuditEvent:
    obj = _strict_object(value, _AUDIT_EVENT_FIELDS, "AuditEvent")
    references_value = obj["references"]
    if not isinstance(references_value, list):
        _invalid("references must be an array")
    if len(references_value) > AUDIT_MAX_REFERENCES:
        _invalid("references exceeds the item limit")
    references = tuple(
        _parse_reference(item) for item in references_value
    )
    return AuditEvent(
        event_id=_as_str(obj["event_id"], "event_id"),
        stream_id=_as_str(obj["stream_id"], "stream_id"),
        sequence=_as_int(obj["sequence"], "sequence"),
        previous_event_id=_as_optional_str(
            obj["previous_event_id"], "previous_event_id"
        ),
        tenant_id=_as_str(obj["tenant_id"], "tenant_id"),
        repository_id=_as_str(obj["repository_id"], "repository_id"),
        session_id=_as_str(obj["session_id"], "session_id"),
        trace_id=_as_str(obj["trace_id"], "trace_id"),
        run_id=_as_str(obj["run_id"], "run_id"),
        actor_type=cast(AuditActorType, obj["actor_type"]),
        actor_id=_as_str(obj["actor_id"], "actor_id"),
        event_type=cast(AuditEventType, obj["event_type"]),
        reason_code=_as_str(obj["reason_code"], "reason_code"),
        payload_sha256=_as_str(obj["payload_sha256"], "payload_sha256"),
        references=references,
        occurred_at=_as_str(obj["occurred_at"], "occurred_at"),
        contract_version=_as_str(
            obj["contract_version"], "contract_version"
        ),
    )


def parse_recovery_action(value: Mapping[str, object]) -> RecoveryAction:
    obj = _strict_object(value, _RECOVERY_FIELDS, "RecoveryAction")
    memory_caused_failure = obj["memory_caused_failure"]
    if memory_caused_failure is not None and type(memory_caused_failure) is not bool:
        _invalid("memory_caused_failure must be boolean or null")
    return RecoveryAction(
        recovery_action_id=_as_str(
            obj["recovery_action_id"], "recovery_action_id"
        ),
        target_kind=cast(RecoveryTargetKind, obj["target_kind"]),
        action=cast(RecoveryActionKind, obj["action"]),
        result=cast(RecoveryResult, obj["result"]),
        session_id=_as_str(obj["session_id"], "session_id"),
        trace_id=_as_str(obj["trace_id"], "trace_id"),
        run_id=_as_str(obj["run_id"], "run_id"),
        usage_decision_id=_as_optional_str(
            obj["usage_decision_id"], "usage_decision_id"
        ),
        expected_session_version=_as_optional_int(
            obj["expected_session_version"],
            "expected_session_version",
        ),
        expected_memory_run_status=_as_optional_str(
            obj["expected_memory_run_status"],
            "expected_memory_run_status",
        ),
        memory_caused_failure=cast(bool | None, memory_caused_failure),
        request_sha256=_as_str(obj["request_sha256"], "request_sha256"),
        requested_by_principal_id=_as_str(
            obj["requested_by_principal_id"],
            "requested_by_principal_id",
        ),
        executor_id=_as_str(obj["executor_id"], "executor_id"),
        error_code=_as_optional_str(obj["error_code"], "error_code"),
        started_at=_as_str(obj["started_at"], "started_at"),
        finished_at=_as_str(obj["finished_at"], "finished_at"),
        contract_version=_as_str(
            obj["contract_version"], "contract_version"
        ),
    )


def _parse_reference(value: object) -> AuditReference:
    if not isinstance(value, Mapping):
        _invalid("AuditReference must be an object")
    obj = _strict_object(
        cast(Mapping[str, object], value),
        _REFERENCE_FIELDS,
        "AuditReference",
    )
    return AuditReference(
        kind=cast(AuditReferenceKind, obj["kind"]),
        record_id=_as_str(obj["record_id"], "record_id"),
    )


def _dumps(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads_object(data: str | bytes | bytearray) -> Mapping[str, object]:
    try:
        if isinstance(data, (bytes, bytearray)):
            text = decode_bounded_utf8(
                bytes(data),
                max_bytes=AUDIT_JSON_MAX_BYTES,
                description="audit JSON",
            )
        elif type(data) is str:
            text = data
            if len(text.encode("utf-8")) > AUDIT_JSON_MAX_BYTES:
                raise ValueError("audit JSON exceeds byte limit")
        else:
            raise TypeError("audit JSON must be str, bytes, or bytearray")
        value = parse_bounded_json(
            text,
            description="audit",
            max_depth=AUDIT_JSON_MAX_DEPTH,
            max_nodes=AUDIT_JSON_MAX_NODES,
        )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise AuditContractError(
            "TBM_AUDIT_INVALID_JSON", str(exc)
        ) from exc
    if not isinstance(value, Mapping):
        _invalid("audit JSON must be an object")
    return cast(Mapping[str, object], value)


def _strict_object(
    value: Mapping[str, object],
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{label} must be an object")
    obj = dict(value)
    if any(type(key) is not str for key in obj):
        _invalid(f"{label} keys must be strings")
    if set(obj) != fields:
        _invalid(f"{label} fields do not match contract")
    return obj


def _reference_tuple(values: tuple[AuditReference, ...]) -> None:
    if type(values) is not tuple or len(values) > AUDIT_MAX_REFERENCES:
        _invalid("references must be a bounded tuple")
    if any(type(item) is not AuditReference for item in values):
        _invalid("references must contain AuditReference values")
    order = tuple((item.kind, item.record_id) for item in values)
    if order != tuple(sorted(order)) or len(set(order)) != len(order):
        _invalid("references must be sorted and unique")


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _IDENTIFIER_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid(f"{name} must be a bounded identifier")


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _code(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _CODE_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid(f"{name} must be a bounded code")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{name} must be a canonical sha256 digest")


def _timestamp(value: object, name: str):
    if type(value) is not str:
        _invalid(f"{name} must be a canonical RFC3339 timestamp")
    try:
        parsed = parse_rfc3339(value)
    except (TypeError, ValueError) as exc:
        _invalid(f"{name} must be a canonical RFC3339 timestamp: {exc}")
    if canonical_rfc3339(value) != value:
        _invalid(f"{name} must be canonical RFC3339")
    return parsed


def _as_str(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return value


def _as_optional_str(value: object, name: str) -> str | None:
    if value is not None and type(value) is not str:
        _invalid(f"{name} must be a string or null")
    return cast(str | None, value)


def _as_int(value: object, name: str) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    return value


def _as_optional_int(value: object, name: str) -> int | None:
    if value is not None and type(value) is not int:
        _invalid(f"{name} must be an integer or null")
    return cast(int | None, value)
