from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import re
from typing import Literal, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .policy import (
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)


GATE_SESSION_CONTRACT_VERSION = "tbm.gate-session.v3"
GATE_SESSION_MAX_BYTES = 1024 * 1024
GATE_SESSION_MAX_DEPTH = 32
GATE_SESSION_MAX_NODES = 10_000
GATE_SESSION_MAX_SEMANTIC_ATTEMPTS = 100
GATE_SESSION_MAX_MEMORY_REVISIONS = 50

GateSessionStatus = Literal[
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

_STATUSES = {
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
}
_TERMINAL_STATUSES = {"completed", "canceled", "expired", "abandoned"}
_ACTIVE_LEASE_STATUSES = {
    "prepared",
    "awaiting_decision",
    "decided",
    "finalized",
    "executing",
}
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"prepared", "canceled"}),
    "prepared": frozenset({"awaiting_decision", "canceled", "expired"}),
    "awaiting_decision": frozenset({"decided", "canceled", "expired"}),
    "decided": frozenset({"finalized"}),
    "finalized": frozenset({"executing"}),
    "executing": frozenset({"completed", "abandoned"}),
    "completed": frozenset(),
    "canceled": frozenset(),
    "expired": frozenset(),
    "abandoned": frozenset(),
}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SERIALIZED_FIELDS = frozenset(
    {
        "contract_version",
        "session_id",
        "tenant_id",
        "repository_id",
        "principal_id",
        "agent_client_id",
        "trace_id",
        "run_id",
        "request_fingerprint",
        "idempotency_key",
        "status",
        "version",
        "created_at",
        "updated_at",
        "expires_at",
        "lease_expires_at",
        "retrieval_snapshot_id",
        "system_gate_evaluation_id",
        "semantic_gate_attempt_ids",
        "decision_id",
        "final_memory_revision_ids",
        "injection_artifact_id",
        "usage_decision_id",
        "run_outcome_id",
        "terminal_reason",
    }
)


class GateSessionContractError(V3ContractError):
    """Stable failure raised for malformed or stale GateSession operations."""


@dataclass(frozen=True)
class GateSession:
    """Immutable version-3 contract for one durable Gate lifecycle."""

    session_id: str
    tenant_id: str
    repository_id: str
    principal_id: str
    agent_client_id: str
    trace_id: str
    run_id: str
    request_fingerprint: str
    idempotency_key: str
    status: GateSessionStatus
    version: int
    created_at: str
    updated_at: str
    expires_at: str
    lease_expires_at: str | None = None
    retrieval_snapshot_id: str | None = None
    system_gate_evaluation_id: str | None = None
    semantic_gate_attempt_ids: tuple[str, ...] = ()
    decision_id: str | None = None
    final_memory_revision_ids: tuple[str, ...] = ()
    injection_artifact_id: str | None = None
    usage_decision_id: str | None = None
    run_outcome_id: str | None = None
    terminal_reason: str | None = None
    contract_version: str = GATE_SESSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != GATE_SESSION_CONTRACT_VERSION:
            _invalid(
                "contract_version must be "
                f"{GATE_SESSION_CONTRACT_VERSION}"
            )
        for field_name in (
            "session_id",
            "tenant_id",
            "repository_id",
            "principal_id",
            "agent_client_id",
            "trace_id",
            "run_id",
        ):
            _required_identifier(getattr(self, field_name), field_name)
        _digest(self.request_fingerprint, "request_fingerprint")
        _required_metadata(self.idempotency_key, "idempotency_key")
        if type(self.status) is not str or self.status not in _STATUSES:
            _invalid("status must be a supported GateSession status")
        if type(self.version) is not int or self.version < 1:
            _invalid("version must be a positive integer")
        created = _timestamp(self.created_at, "created_at")
        updated = _timestamp(self.updated_at, "updated_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if updated < created:
            _invalid("updated_at must not precede created_at")
        if expires <= created:
            _invalid("expires_at must be later than created_at")
        if updated > expires and self.status not in _TERMINAL_STATUSES:
            _invalid(
                "nonterminal updated_at must not be later than expires_at"
            )
        if self.status == "expired" and updated < expires:
            _invalid("expired session updated_at cannot precede expires_at")
        if self.lease_expires_at is not None:
            lease = _timestamp(self.lease_expires_at, "lease_expires_at")
            if lease <= updated or lease > expires:
                _invalid(
                    "lease_expires_at must be later than updated_at and "
                    "not later than expires_at"
                )
        _optional_identifier(
            self.retrieval_snapshot_id,
            "retrieval_snapshot_id",
        )
        _optional_identifier(
            self.system_gate_evaluation_id,
            "system_gate_evaluation_id",
        )
        _identifier_tuple(
            self.semantic_gate_attempt_ids,
            "semantic_gate_attempt_ids",
            max_items=GATE_SESSION_MAX_SEMANTIC_ATTEMPTS,
        )
        _optional_identifier(self.decision_id, "decision_id")
        _identifier_tuple(
            self.final_memory_revision_ids,
            "final_memory_revision_ids",
            max_items=GATE_SESSION_MAX_MEMORY_REVISIONS,
        )
        _optional_identifier(
            self.injection_artifact_id,
            "injection_artifact_id",
        )
        _optional_identifier(self.usage_decision_id, "usage_decision_id")
        _optional_identifier(self.run_outcome_id, "run_outcome_id")
        if self.terminal_reason is not None:
            _required_reason(self.terminal_reason, "terminal_reason")
        self._validate_record_shape()

    def _validate_record_shape(self) -> None:
        has_retrieval = self.retrieval_snapshot_id is not None
        has_system_gate = self.system_gate_evaluation_id is not None
        has_decision = self.decision_id is not None
        has_injection = self.injection_artifact_id is not None
        has_usage = self.usage_decision_id is not None
        has_outcome = self.run_outcome_id is not None

        if has_retrieval != has_system_gate:
            _invalid(
                "retrieval_snapshot_id and system_gate_evaluation_id "
                "must be recorded together"
            )
        if self.semantic_gate_attempt_ids and not has_decision:
            _invalid("semantic Gate attempts require decision_id")
        if has_decision and not has_retrieval:
            _invalid("decision_id requires prepared retrieval evidence")
        if has_injection != has_usage:
            _invalid(
                "injection_artifact_id and usage_decision_id "
                "must be recorded together"
            )
        if has_injection and not has_decision:
            _invalid("finalized artifacts require decision_id")
        if self.final_memory_revision_ids and not has_injection:
            _invalid(
                "final_memory_revision_ids require finalized artifacts"
            )
        if has_outcome and not has_injection:
            _invalid("run_outcome_id requires finalized artifacts")

        if self.status == "created":
            if (
                self.lease_expires_at is not None
                or has_retrieval
                or has_decision
                or has_injection
                or has_outcome
                or self.semantic_gate_attempt_ids
                or self.final_memory_revision_ids
            ):
                _invalid("created session cannot contain lifecycle results")
        elif self.status in {"prepared", "awaiting_decision"}:
            if not has_retrieval or has_decision or has_injection or has_outcome:
                _invalid(
                    f"{self.status} session must contain only prepared "
                    "retrieval evidence"
                )
        elif self.status == "decided":
            if not has_retrieval or not has_decision or has_injection or has_outcome:
                _invalid(
                    "decided session requires a decision and forbids "
                    "finalized artifacts"
                )
        elif self.status in {"finalized", "executing"}:
            if not has_injection or has_outcome:
                _invalid(
                    f"{self.status} session requires finalized artifacts "
                    "and forbids run outcome"
                )
        elif self.status == "completed":
            if not has_outcome:
                _invalid("completed session requires run_outcome_id")
        elif self.status == "abandoned":
            if not has_injection or has_outcome:
                _invalid(
                    "abandoned session requires finalized artifacts and "
                    "forbids run outcome"
                )
        elif self.status == "canceled":
            if has_decision or has_injection or has_outcome:
                _invalid(
                    "canceled session cannot contain decided or finalized "
                    "lifecycle results"
                )
        elif self.status == "expired":
            if not has_retrieval or has_decision or has_injection or has_outcome:
                _invalid(
                    "expired session requires prepared retrieval evidence "
                    "and forbids decided or finalized lifecycle results"
                )

        if self.status in _ACTIVE_LEASE_STATUSES:
            if self.lease_expires_at is None:
                _invalid(f"{self.status} session requires an active lease")
        elif self.lease_expires_at is not None:
            _invalid(f"{self.status} session cannot retain an active lease")

        if self.status in {"canceled", "expired", "abandoned"}:
            if self.terminal_reason is None:
                _invalid(f"{self.status} session requires terminal_reason")
        elif self.terminal_reason is not None:
            _invalid(f"{self.status} session cannot contain terminal_reason")

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "principal_id": self.principal_id,
            "agent_client_id": self.agent_client_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "request_fingerprint": self.request_fingerprint,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "version": self.version,
            "created_at": canonical_rfc3339(self.created_at),
            "updated_at": canonical_rfc3339(self.updated_at),
            "expires_at": canonical_rfc3339(self.expires_at),
            "lease_expires_at": (
                canonical_rfc3339(self.lease_expires_at)
                if self.lease_expires_at is not None
                else None
            ),
            "retrieval_snapshot_id": self.retrieval_snapshot_id,
            "system_gate_evaluation_id": self.system_gate_evaluation_id,
            "semantic_gate_attempt_ids": list(
                self.semantic_gate_attempt_ids
            ),
            "decision_id": self.decision_id,
            "final_memory_revision_ids": list(
                self.final_memory_revision_ids
            ),
            "injection_artifact_id": self.injection_artifact_id,
            "usage_decision_id": self.usage_decision_id,
            "run_outcome_id": self.run_outcome_id,
            "terminal_reason": self.terminal_reason,
        }


def create_gate_session(
    *,
    session_id: str,
    tenant_id: str,
    repository_id: str,
    principal_id: str,
    agent_client_id: str,
    trace_id: str,
    run_id: str,
    request_fingerprint: str,
    idempotency_key: str,
    created_at: str,
    expires_at: str,
) -> GateSession:
    """Create a first revision using trusted service-owned timestamps."""

    return GateSession(
        session_id=session_id,
        tenant_id=tenant_id,
        repository_id=repository_id,
        principal_id=principal_id,
        agent_client_id=agent_client_id,
        trace_id=trace_id,
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        idempotency_key=idempotency_key,
        status="created",
        version=1,
        created_at=created_at,
        updated_at=created_at,
        expires_at=expires_at,
    )


def transition_gate_session(
    session: GateSession,
    target_status: GateSessionStatus,
    *,
    expected_version: int,
    updated_at: str,
    lease_expires_at: str | None = None,
    retrieval_snapshot_id: str | None = None,
    system_gate_evaluation_id: str | None = None,
    semantic_gate_attempt_ids: tuple[str, ...] | None = None,
    decision_id: str | None = None,
    final_memory_revision_ids: tuple[str, ...] | None = None,
    injection_artifact_id: str | None = None,
    usage_decision_id: str | None = None,
    run_outcome_id: str | None = None,
    terminal_reason: str | None = None,
) -> GateSession:
    """Advance a revision using a trusted service-owned updated_at."""

    if type(session) is not GateSession:
        _invalid("session must be exactly GateSession")
    if type(expected_version) is not int or expected_version != session.version:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_STALE_VERSION",
            "expected_version does not match the current session revision",
        )
    if type(target_status) is not str or target_status not in _STATUSES:
        _invalid("target_status must be a supported GateSession status")
    if target_status not in _ALLOWED_TRANSITIONS[session.status]:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID_TRANSITION",
            f"cannot transition GateSession from {session.status} "
            f"to {target_status}",
        )
    supplied_fields = {
        name
        for name, value in (
            ("lease_expires_at", lease_expires_at),
            ("retrieval_snapshot_id", retrieval_snapshot_id),
            ("system_gate_evaluation_id", system_gate_evaluation_id),
            ("semantic_gate_attempt_ids", semantic_gate_attempt_ids),
            ("decision_id", decision_id),
            ("final_memory_revision_ids", final_memory_revision_ids),
            ("injection_artifact_id", injection_artifact_id),
            ("usage_decision_id", usage_decision_id),
            ("run_outcome_id", run_outcome_id),
            ("terminal_reason", terminal_reason),
        )
        if value is not None
    }
    allowed_fields = {
        "prepared": {
            "lease_expires_at",
            "retrieval_snapshot_id",
            "system_gate_evaluation_id",
        },
        "awaiting_decision": set(),
        "decided": {"semantic_gate_attempt_ids", "decision_id"},
        "finalized": {
            "final_memory_revision_ids",
            "injection_artifact_id",
            "usage_decision_id",
        },
        "executing": set(),
        "completed": {"run_outcome_id"},
        "canceled": {"terminal_reason"},
        "expired": {"terminal_reason"},
        "abandoned": {"terminal_reason"},
    }[target_status]
    unexpected_fields = sorted(supplied_fields - allowed_fields)
    if unexpected_fields:
        _invalid(
            f"{target_status} transition cannot set "
            f"{unexpected_fields[0]}"
        )
    required_fields = {
        "prepared": {
            "lease_expires_at",
            "retrieval_snapshot_id",
            "system_gate_evaluation_id",
        },
        "awaiting_decision": set(),
        "decided": {"semantic_gate_attempt_ids", "decision_id"},
        "finalized": {
            "final_memory_revision_ids",
            "injection_artifact_id",
            "usage_decision_id",
        },
        "executing": set(),
        "completed": {"run_outcome_id"},
        "canceled": {"terminal_reason"},
        "expired": {"terminal_reason"},
        "abandoned": {"terminal_reason"},
    }[target_status]
    missing_fields = sorted(required_fields - supplied_fields)
    if missing_fields:
        _invalid(
            f"{target_status} transition requires {missing_fields[0]}"
        )
    previous_updated = parse_rfc3339(session.updated_at)
    try:
        next_updated = parse_rfc3339(updated_at)
    except ValueError as error:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID",
            "updated_at must be a timezone-aware RFC 3339 date-time",
        ) from error
    if next_updated <= previous_updated:
        _invalid("transition updated_at must be later than current updated_at")
    if (
        target_status not in _TERMINAL_STATUSES
        and next_updated > parse_rfc3339(session.expires_at)
    ):
        _invalid("transition updated_at must not be later than expires_at")
    if target_status == "expired" and next_updated < parse_rfc3339(
        session.expires_at
    ):
        _invalid("expired transition cannot precede expires_at")

    next_lease = (
        None
        if target_status in _TERMINAL_STATUSES
        else (
            lease_expires_at
            if lease_expires_at is not None
            else session.lease_expires_at
        )
    )
    return replace(
        session,
        status=target_status,
        version=session.version + 1,
        updated_at=updated_at,
        lease_expires_at=next_lease,
        retrieval_snapshot_id=(
            retrieval_snapshot_id
            if retrieval_snapshot_id is not None
            else session.retrieval_snapshot_id
        ),
        system_gate_evaluation_id=(
            system_gate_evaluation_id
            if system_gate_evaluation_id is not None
            else session.system_gate_evaluation_id
        ),
        semantic_gate_attempt_ids=(
            semantic_gate_attempt_ids
            if semantic_gate_attempt_ids is not None
            else session.semantic_gate_attempt_ids
        ),
        decision_id=(
            decision_id if decision_id is not None else session.decision_id
        ),
        final_memory_revision_ids=(
            final_memory_revision_ids
            if final_memory_revision_ids is not None
            else session.final_memory_revision_ids
        ),
        injection_artifact_id=(
            injection_artifact_id
            if injection_artifact_id is not None
            else session.injection_artifact_id
        ),
        usage_decision_id=(
            usage_decision_id
            if usage_decision_id is not None
            else session.usage_decision_id
        ),
        run_outcome_id=(
            run_outcome_id
            if run_outcome_id is not None
            else session.run_outcome_id
        ),
        terminal_reason=terminal_reason,
    )


def renew_gate_session_lease(
    session: GateSession,
    *,
    expected_version: int,
    updated_at: str,
    lease_expires_at: str,
) -> GateSession:
    """Renew a lease using trusted service-owned timestamps."""

    if type(session) is not GateSession:
        _invalid("session must be exactly GateSession")
    if type(expected_version) is not int or expected_version != session.version:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_STALE_VERSION",
            "expected_version does not match the current session revision",
        )
    if session.status not in _ACTIVE_LEASE_STATUSES:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID_TRANSITION",
            f"cannot renew lease for {session.status} GateSession",
        )
    try:
        current_lease = parse_rfc3339(session.lease_expires_at)
        next_lease = parse_rfc3339(lease_expires_at)
        next_updated = parse_rfc3339(updated_at)
    except ValueError as error:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID",
            "lease renewal timestamps must be timezone-aware RFC 3339",
        ) from error
    if next_updated <= parse_rfc3339(session.updated_at):
        _invalid("lease renewal updated_at must advance")
    if next_updated >= current_lease:
        _invalid("lease must be renewed before the current lease expires")
    if next_lease <= current_lease:
        _invalid("renewed lease_expires_at must extend the current lease")
    return replace(
        session,
        version=session.version + 1,
        updated_at=updated_at,
        lease_expires_at=lease_expires_at,
    )


def parse_gate_session(payload: Mapping[str, object]) -> GateSession:
    """Parse one strict external GateSession object."""

    if type(payload) is not dict:
        _invalid("GateSession payload must be a JSON object")
    fields = frozenset(payload)
    unknown = sorted(fields - _SERIALIZED_FIELDS)
    missing = sorted(_SERIALIZED_FIELDS - fields)
    if unknown:
        _invalid(f"GateSession payload has unknown field: {unknown[0]}")
    if missing:
        _invalid(f"GateSession payload is missing field: {missing[0]}")
    return GateSession(
        contract_version=_string(payload, "contract_version"),
        session_id=_string(payload, "session_id"),
        tenant_id=_string(payload, "tenant_id"),
        repository_id=_string(payload, "repository_id"),
        principal_id=_string(payload, "principal_id"),
        agent_client_id=_string(payload, "agent_client_id"),
        trace_id=_string(payload, "trace_id"),
        run_id=_string(payload, "run_id"),
        request_fingerprint=_string(payload, "request_fingerprint"),
        idempotency_key=_string(payload, "idempotency_key"),
        status=cast(GateSessionStatus, _string(payload, "status")),
        version=_integer(payload, "version"),
        created_at=_string(payload, "created_at"),
        updated_at=_string(payload, "updated_at"),
        expires_at=_string(payload, "expires_at"),
        lease_expires_at=_optional_string(payload, "lease_expires_at"),
        retrieval_snapshot_id=_optional_string(
            payload,
            "retrieval_snapshot_id",
        ),
        system_gate_evaluation_id=_optional_string(
            payload,
            "system_gate_evaluation_id",
        ),
        semantic_gate_attempt_ids=_string_list(
            payload,
            "semantic_gate_attempt_ids",
        ),
        decision_id=_optional_string(payload, "decision_id"),
        final_memory_revision_ids=_string_list(
            payload,
            "final_memory_revision_ids",
        ),
        injection_artifact_id=_optional_string(
            payload,
            "injection_artifact_id",
        ),
        usage_decision_id=_optional_string(payload, "usage_decision_id"),
        run_outcome_id=_optional_string(payload, "run_outcome_id"),
        terminal_reason=_optional_string(payload, "terminal_reason"),
    )


def loads_gate_session(source: str | bytes) -> GateSession:
    """Decode bounded strict JSON into one GateSession contract."""

    if type(source) is bytes:
        try:
            source_text = decode_bounded_utf8(
                source,
                max_bytes=GATE_SESSION_MAX_BYTES,
                description="GateSession JSON",
            )
        except UnicodeDecodeError as error:
            raise GateSessionContractError(
                "TBM_GATE_SESSION_INVALID_JSON",
                "GateSession JSON contains invalid UTF-8",
            ) from error
    else:
        source_text = source
    try:
        if type(source_text) is not str:
            raise ValueError("GateSession source must be str or bytes")
        if len(source_text.encode("utf-8")) > GATE_SESSION_MAX_BYTES:
            raise ValueError(
                "GateSession JSON exceeds maximum size of "
                f"{GATE_SESSION_MAX_BYTES} bytes"
            )
        payload = parse_bounded_json(
            source_text,
            description="GateSession",
            max_nodes=GATE_SESSION_MAX_NODES,
            max_depth=GATE_SESSION_MAX_DEPTH,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID_JSON",
            str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
        ) from error
    if type(payload) is not dict:
        _invalid("GateSession JSON must contain one object")
    return parse_gate_session(payload)


def dumps_gate_session(session: GateSession) -> str:
    """Serialize one GateSession as finite canonical JSON."""

    if type(session) is not GateSession:
        _invalid("session must be exactly GateSession")
    return json.dumps(
        session.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _string(payload: Mapping[str, object], field_name: str) -> str:
    value = payload[field_name]
    if type(value) is not str:
        _invalid(f"{field_name} must be a string")
    return cast(str, value)


def _optional_string(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload[field_name]
    if value is not None and type(value) is not str:
        _invalid(f"{field_name} must be a string or null")
    return cast(str | None, value)


def _integer(payload: Mapping[str, object], field_name: str) -> int:
    value = payload[field_name]
    if type(value) is not int:
        _invalid(f"{field_name} must be an integer")
    return cast(int, value)


def _string_list(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    value = payload[field_name]
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid(f"{field_name} must be an array of strings")
    return tuple(cast(list[str], value))


def _required_identifier(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_ID_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{MEMORY_ID_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _optional_identifier(value: object, field_name: str) -> None:
    if value is not None:
        _required_identifier(value, field_name)


def _required_metadata(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > METADATA_VALUE_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{METADATA_VALUE_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _required_reason(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MEMORY_DECISION_REASON_MAX_CHARS
    ):
        _invalid(
            f"{field_name} must be a nonblank string of at most "
            f"{MEMORY_DECISION_REASON_MAX_CHARS} characters"
        )
    _unicode(cast(str, value), field_name)


def _identifier_tuple(
    value: object,
    field_name: str,
    *,
    max_items: int,
) -> None:
    if type(value) is not tuple or len(value) > max_items:
        _invalid(
            f"{field_name} must be a tuple with at most "
            f"{max_items} entries"
        )
    for item in value:
        _required_identifier(item, field_name)
    if len(set(value)) != len(value):
        _invalid(f"{field_name} must not contain duplicates")


def _digest(value: object, field_name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid(f"{field_name} must be an algorithm-tagged SHA-256 digest")


def _timestamp(value: object, field_name: str):
    try:
        return parse_rfc3339(value)
    except ValueError as error:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID",
            f"{field_name} must be a timezone-aware RFC 3339 date-time",
        ) from error


def _unicode(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GateSessionContractError(
            "TBM_GATE_SESSION_INVALID",
            f"{field_name} must contain valid Unicode",
        ) from error


def _invalid(message: str) -> None:
    raise GateSessionContractError("TBM_GATE_SESSION_INVALID", message)


__all__ = [
    "GATE_SESSION_CONTRACT_VERSION",
    "GATE_SESSION_MAX_BYTES",
    "GATE_SESSION_MAX_DEPTH",
    "GATE_SESSION_MAX_MEMORY_REVISIONS",
    "GATE_SESSION_MAX_NODES",
    "GATE_SESSION_MAX_SEMANTIC_ATTEMPTS",
    "GateSession",
    "GateSessionContractError",
    "GateSessionStatus",
    "create_gate_session",
    "dumps_gate_session",
    "loads_gate_session",
    "parse_gate_session",
    "renew_gate_session_lease",
    "transition_gate_session",
]
