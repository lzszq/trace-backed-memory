from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Literal, Mapping, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import canonical_sha256
from .retrieval_v3 import RetrievalSnapshot


SYSTEM_GATE_EVALUATION_CONTRACT_VERSION = "tbm.system-gate-evaluation.v3"
SEMANTIC_GATE_ATTEMPT_CONTRACT_VERSION = "tbm.semantic-gate-attempt.v3"
GATE_EVALUATION_JSON_MAX_BYTES = 1024 * 1024
GATE_EVALUATION_JSON_MAX_DEPTH = 32
GATE_EVALUATION_JSON_MAX_NODES = 20_000
GATE_EVALUATION_MAX_DECISIONS = 100
GATE_EVALUATION_MAX_SEQUENCE = 2_147_483_647

_IDENTIFIER_MAX_CHARS = 128
_TEXT_MAX_CHARS = 4_096
_SYSTEM_ID_RE = re.compile(r"^system_gate_sha256_[0-9a-f]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^semantic_attempt_sha256_[0-9a-f]{64}$")
_SNAPSHOT_ID_RE = re.compile(r"^retrieval_snapshot_sha256_[0-9a-f]{64}$")
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_AUTHORIZATION_ID_RE = re.compile(r"^authz_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])$"
)
_DECISION_FIELDS = frozenset(
    {
        "memory_revision_id",
        "candidate_sha256",
        "outcome",
        "reason_code",
        "rule_id",
    }
)
_SYSTEM_FIELDS = frozenset(
    {
        "contract_version",
        "evaluation_id",
        "session_id",
        "retrieval_snapshot_id",
        "authorization_event_id",
        "policy_bundle_sha256",
        "evaluator_id",
        "evaluator_version",
        "decisions",
        "evaluated_at",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "contract_version",
        "attempt_id",
        "session_id",
        "retrieval_snapshot_id",
        "system_gate_evaluation_id",
        "sequence",
        "previous_attempt_id",
        "provider_id",
        "model_id",
        "model_version",
        "endpoint_id",
        "prompt_template_id",
        "prompt_template_version",
        "prompt_artifact_sha256",
        "response_artifact_sha256",
        "generation_config_sha256",
        "provider_request_id",
        "status",
        "decision_id",
        "final_allowed_revision_ids",
        "final_blocked_revision_ids",
        "reason",
        "risk",
        "recommended_injection",
        "error_code",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "started_at",
        "finished_at",
    }
)

GateOutcome = Literal["allowed", "blocked"]
SemanticAttemptStatus = Literal["succeeded", "failed"]
SemanticRisk = Literal["low", "medium", "high", "unknown"]
RecommendedInjection = Literal["none", "summary", "full"]


class GateEvaluationContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise GateEvaluationContractError("TBM_GATE_EVALUATION_INVALID", message)


@dataclass(frozen=True)
class SystemGateDecision:
    memory_revision_id: str
    candidate_sha256: str
    outcome: GateOutcome
    reason_code: str
    rule_id: str

    def __post_init__(self) -> None:
        _revision_id(self.memory_revision_id)
        _digest(self.candidate_sha256, "candidate_sha256")
        if type(self.outcome) is not str or self.outcome not in {
            "allowed",
            "blocked",
        }:
            _invalid("System Gate outcome must be allowed or blocked")
        _identifier(self.reason_code, "reason_code")
        _identifier(self.rule_id, "rule_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_revision_id": self.memory_revision_id,
            "candidate_sha256": self.candidate_sha256,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True)
class SystemGateEvaluation:
    evaluation_id: str
    session_id: str
    retrieval_snapshot_id: str
    authorization_event_id: str
    policy_bundle_sha256: str
    evaluator_id: str
    evaluator_version: str
    decisions: tuple[SystemGateDecision, ...]
    evaluated_at: str
    contract_version: str = SYSTEM_GATE_EVALUATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.evaluated_at, "evaluated_at")
        object.__setattr__(
            self,
            "evaluated_at",
            canonical_rfc3339(self.evaluated_at),
        )
        if self.contract_version != SYSTEM_GATE_EVALUATION_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.evaluation_id) is not str or not _SYSTEM_ID_RE.fullmatch(
            self.evaluation_id
        ):
            _invalid("evaluation_id must be system_gate_sha256_<64 lowercase hex>")
        _identifier(self.session_id, "session_id")
        _snapshot_id(self.retrieval_snapshot_id)
        if (
            type(self.authorization_event_id) is not str
            or not _AUTHORIZATION_ID_RE.fullmatch(self.authorization_event_id)
        ):
            _invalid("authorization_event_id must reference an authorization event")
        _digest(self.policy_bundle_sha256, "policy_bundle_sha256")
        _identifier(self.evaluator_id, "evaluator_id")
        _identifier(self.evaluator_version, "evaluator_version")
        _system_decisions(self.decisions)
        expected = system_gate_evaluation_id(self._unsigned_dict())
        if self.evaluation_id != expected:
            raise GateEvaluationContractError(
                "TBM_SYSTEM_GATE_HASH_MISMATCH",
                "evaluation_id does not match canonical System Gate content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "retrieval_snapshot_id": self.retrieval_snapshot_id,
            "authorization_event_id": self.authorization_event_id,
            "policy_bundle_sha256": self.policy_bundle_sha256,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "evaluated_at": self.evaluated_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"evaluation_id": self.evaluation_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class SemanticGateAttempt:
    attempt_id: str
    session_id: str
    retrieval_snapshot_id: str
    system_gate_evaluation_id: str
    sequence: int
    previous_attempt_id: str | None
    provider_id: str
    model_id: str
    model_version: str
    endpoint_id: str | None
    prompt_template_id: str
    prompt_template_version: str
    prompt_artifact_sha256: str
    response_artifact_sha256: str | None
    generation_config_sha256: str
    provider_request_id: str | None
    status: SemanticAttemptStatus
    decision_id: str | None
    final_allowed_revision_ids: tuple[str, ...]
    final_blocked_revision_ids: tuple[str, ...]
    reason: str | None
    risk: SemanticRisk | None
    recommended_injection: RecommendedInjection | None
    error_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    started_at: str
    finished_at: str
    contract_version: str = SEMANTIC_GATE_ATTEMPT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.started_at, "started_at")
        _timestamp(self.finished_at, "finished_at")
        object.__setattr__(self, "started_at", canonical_rfc3339(self.started_at))
        object.__setattr__(self, "finished_at", canonical_rfc3339(self.finished_at))
        if self.contract_version != SEMANTIC_GATE_ATTEMPT_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.attempt_id) is not str or not _ATTEMPT_ID_RE.fullmatch(
            self.attempt_id
        ):
            _invalid(
                "attempt_id must be semantic_attempt_sha256_<64 lowercase hex>"
            )
        _identifier(self.session_id, "session_id")
        _snapshot_id(self.retrieval_snapshot_id)
        if (
            type(self.system_gate_evaluation_id) is not str
            or not _SYSTEM_ID_RE.fullmatch(self.system_gate_evaluation_id)
        ):
            _invalid("system_gate_evaluation_id must reference a System Gate record")
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or self.sequence > GATE_EVALUATION_MAX_SEQUENCE
        ):
            _invalid("sequence must be a bounded positive integer")
        if self.sequence == 1:
            if self.previous_attempt_id is not None:
                _invalid("first semantic attempt must not have a parent")
        elif (
            type(self.previous_attempt_id) is not str
            or not _ATTEMPT_ID_RE.fullmatch(self.previous_attempt_id)
        ):
            _invalid("later semantic attempt must reference its parent")
        if self.previous_attempt_id == self.attempt_id:
            _invalid("semantic attempt must not reference itself")
        for field in (
            "provider_id",
            "model_id",
            "model_version",
            "prompt_template_id",
            "prompt_template_version",
        ):
            _identifier(getattr(self, field), field)
        _optional_identifier(self.endpoint_id, "endpoint_id")
        _digest(self.prompt_artifact_sha256, "prompt_artifact_sha256")
        if self.response_artifact_sha256 is not None:
            _digest(self.response_artifact_sha256, "response_artifact_sha256")
        _digest(self.generation_config_sha256, "generation_config_sha256")
        _optional_identifier(self.provider_request_id, "provider_request_id")
        if type(self.status) is not str or self.status not in {"succeeded", "failed"}:
            _invalid("status must be succeeded or failed")
        _revision_ids(
            self.final_allowed_revision_ids,
            "final_allowed_revision_ids",
        )
        _revision_ids(
            self.final_blocked_revision_ids,
            "final_blocked_revision_ids",
        )
        if set(self.final_allowed_revision_ids).intersection(
            self.final_blocked_revision_ids
        ):
            _invalid("final allowed and blocked revision IDs must be disjoint")
        _bounded_nonnegative(self.input_tokens, "input_tokens", optional=True)
        _bounded_nonnegative(self.output_tokens, "output_tokens", optional=True)
        _bounded_nonnegative(self.latency_ms, "latency_ms", optional=False)
        if parse_rfc3339(self.finished_at) < parse_rfc3339(self.started_at):
            _invalid("finished_at must not precede started_at")
        if self.status == "succeeded":
            for value, label in (
                (self.response_artifact_sha256, "response_artifact_sha256"),
                (self.decision_id, "decision_id"),
                (self.endpoint_id, "endpoint_id"),
                (self.provider_request_id, "provider_request_id"),
                (self.reason, "reason"),
                (self.risk, "risk"),
                (self.recommended_injection, "recommended_injection"),
            ):
                if value is None:
                    _invalid(f"succeeded semantic attempt requires {label}")
            _identifier(self.decision_id, "decision_id")
            _text(self.reason, "reason")
            if self.risk not in {"low", "medium", "high", "unknown"}:
                _invalid("risk is not supported")
            if self.recommended_injection not in {"none", "summary", "full"}:
                _invalid("recommended_injection is not supported")
            if self.error_code is not None:
                _invalid("succeeded semantic attempt forbids error_code")
        else:
            if any(
                value is not None
                for value in (
                    self.response_artifact_sha256,
                    self.decision_id,
                    self.reason,
                    self.risk,
                    self.recommended_injection,
                )
            ):
                _invalid("failed semantic attempt forbids decision output")
            if self.final_allowed_revision_ids or self.final_blocked_revision_ids:
                _invalid("failed semantic attempt forbids final revision IDs")
            _identifier(self.error_code, "error_code")
        expected = semantic_gate_attempt_id(self._unsigned_dict())
        if self.attempt_id != expected:
            raise GateEvaluationContractError(
                "TBM_SEMANTIC_GATE_HASH_MISMATCH",
                "attempt_id does not match canonical Semantic Gate content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "retrieval_snapshot_id": self.retrieval_snapshot_id,
            "system_gate_evaluation_id": self.system_gate_evaluation_id,
            "sequence": self.sequence,
            "previous_attempt_id": self.previous_attempt_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "endpoint_id": self.endpoint_id,
            "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": self.prompt_template_version,
            "prompt_artifact_sha256": self.prompt_artifact_sha256,
            "response_artifact_sha256": self.response_artifact_sha256,
            "generation_config_sha256": self.generation_config_sha256,
            "provider_request_id": self.provider_request_id,
            "status": self.status,
            "decision_id": self.decision_id,
            "final_allowed_revision_ids": list(self.final_allowed_revision_ids),
            "final_blocked_revision_ids": list(self.final_blocked_revision_ids),
            "reason": self.reason,
            "risk": self.risk,
            "recommended_injection": self.recommended_injection,
            "error_code": self.error_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"attempt_id": self.attempt_id, **self._unsigned_dict()}


def system_gate_evaluation_id(payload: Mapping[str, object]) -> str:
    return "system_gate_sha256_" + canonical_sha256(payload).removeprefix("sha256:")


def semantic_gate_attempt_id(payload: Mapping[str, object]) -> str:
    return "semantic_attempt_sha256_" + canonical_sha256(payload).removeprefix(
        "sha256:"
    )


def build_system_gate_evaluation(
    *,
    session_id: str,
    retrieval_snapshot_id: str,
    authorization_event_id: str,
    policy_bundle_sha256: str,
    evaluator_id: str,
    evaluator_version: str,
    decisions: tuple[SystemGateDecision, ...],
    evaluated_at: str,
) -> SystemGateEvaluation:
    if type(decisions) is not tuple or any(
        type(item) is not SystemGateDecision for item in decisions
    ):
        _invalid("decisions must be a tuple of SystemGateDecision records")
    canonical_time = _canonical_timestamp(evaluated_at, "evaluated_at")
    values: dict[str, object] = {
        "contract_version": SYSTEM_GATE_EVALUATION_CONTRACT_VERSION,
        "session_id": session_id,
        "retrieval_snapshot_id": retrieval_snapshot_id,
        "authorization_event_id": authorization_event_id,
        "policy_bundle_sha256": policy_bundle_sha256,
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
        "decisions": [decision.to_dict() for decision in decisions],
        "evaluated_at": canonical_time,
    }
    return SystemGateEvaluation(
        evaluation_id=system_gate_evaluation_id(values),
        session_id=session_id,
        retrieval_snapshot_id=retrieval_snapshot_id,
        authorization_event_id=authorization_event_id,
        policy_bundle_sha256=policy_bundle_sha256,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        decisions=decisions,
        evaluated_at=canonical_time,
    )


def build_semantic_gate_attempt(**values: object) -> SemanticGateAttempt:
    allowed = _builder_revision_ids(
        values.get("final_allowed_revision_ids"),
        "final_allowed_revision_ids",
    )
    blocked = _builder_revision_ids(
        values.get("final_blocked_revision_ids"),
        "final_blocked_revision_ids",
    )
    started = _canonical_timestamp(values.get("started_at"), "started_at")
    finished = _canonical_timestamp(values.get("finished_at"), "finished_at")
    typed = dict(values)
    typed["final_allowed_revision_ids"] = list(allowed)
    typed["final_blocked_revision_ids"] = list(blocked)
    typed["started_at"] = started
    typed["finished_at"] = finished
    typed["contract_version"] = SEMANTIC_GATE_ATTEMPT_CONTRACT_VERSION
    attempt_id = semantic_gate_attempt_id(typed)
    typed["attempt_id"] = attempt_id
    typed["final_allowed_revision_ids"] = allowed
    typed["final_blocked_revision_ids"] = blocked
    return SemanticGateAttempt(**typed)  # type: ignore[arg-type]


def verify_system_gate_evaluation(
    evaluation: SystemGateEvaluation,
    snapshot: RetrievalSnapshot,
) -> None:
    if type(evaluation) is not SystemGateEvaluation:
        _invalid("evaluation must be exactly SystemGateEvaluation")
    if type(snapshot) is not RetrievalSnapshot:
        _invalid("snapshot must be exactly RetrievalSnapshot")
    if evaluation.session_id != snapshot.session_id:
        _invalid("System Gate session does not match retrieval snapshot")
    if evaluation.retrieval_snapshot_id != snapshot.snapshot_id:
        _invalid("System Gate retrieval snapshot reference does not match")
    if evaluation.authorization_event_id != snapshot.authorization_event_id:
        _invalid("System Gate authorization event does not match retrieval")
    expected = tuple(
        (hit.memory_revision_id, hit.candidate_sha256) for hit in snapshot.hits
    )
    actual = tuple(
        (decision.memory_revision_id, decision.candidate_sha256)
        for decision in evaluation.decisions
    )
    if actual != expected:
        _invalid("System Gate decisions must exactly cover ordered retrieval hits")
    if parse_rfc3339(evaluation.evaluated_at) < parse_rfc3339(snapshot.created_at):
        _invalid("System Gate evaluation must not precede retrieval")


def verify_semantic_gate_attempt(
    attempt: SemanticGateAttempt,
    evaluation: SystemGateEvaluation,
    snapshot: RetrievalSnapshot,
) -> None:
    if type(attempt) is not SemanticGateAttempt:
        _invalid("attempt must be exactly SemanticGateAttempt")
    if type(evaluation) is not SystemGateEvaluation:
        _invalid("evaluation must be exactly SystemGateEvaluation")
    verify_system_gate_evaluation(evaluation, snapshot)
    if attempt.session_id != evaluation.session_id:
        _invalid("Semantic Gate session does not match System Gate")
    if attempt.retrieval_snapshot_id != evaluation.retrieval_snapshot_id:
        _invalid("Semantic Gate retrieval snapshot does not match System Gate")
    if attempt.system_gate_evaluation_id != evaluation.evaluation_id:
        _invalid("Semantic Gate System Gate reference does not match")
    if parse_rfc3339(attempt.started_at) < parse_rfc3339(evaluation.evaluated_at):
        _invalid("Semantic Gate attempt must not precede System Gate")
    if attempt.status == "failed":
        return
    system_allowed = {
        decision.memory_revision_id
        for decision in evaluation.decisions
        if decision.outcome == "allowed"
    }
    system_blocked = {
        decision.memory_revision_id
        for decision in evaluation.decisions
        if decision.outcome == "blocked"
    }
    final_allowed = set(attempt.final_allowed_revision_ids)
    final_blocked = set(attempt.final_blocked_revision_ids)
    if not final_allowed.issubset(system_allowed):
        _invalid("Semantic Gate cannot reopen a System Gate block")
    if final_allowed.union(final_blocked) != system_allowed.union(system_blocked):
        _invalid("Semantic Gate final decision must cover every System Gate candidate")
    if not system_blocked.issubset(final_blocked):
        _invalid("Semantic Gate final blocked set must preserve System Gate blocks")


def verify_semantic_gate_attempt_parent(
    attempt: SemanticGateAttempt,
    parent: SemanticGateAttempt | None,
) -> None:
    if type(attempt) is not SemanticGateAttempt:
        _invalid("attempt must be exactly SemanticGateAttempt")
    if attempt.sequence == 1:
        if parent is not None:
            _invalid("first semantic attempt must not have a parent record")
        return
    if type(parent) is not SemanticGateAttempt:
        _invalid("later semantic attempt requires its exact parent record")
    if attempt.previous_attempt_id != parent.attempt_id:
        _invalid("semantic attempt parent identity does not match")
    if attempt.sequence != parent.sequence + 1:
        _invalid("semantic attempt sequence must follow its parent")
    if (
        attempt.session_id != parent.session_id
        or attempt.retrieval_snapshot_id != parent.retrieval_snapshot_id
        or attempt.system_gate_evaluation_id
        != parent.system_gate_evaluation_id
    ):
        _invalid("semantic attempt parent belongs to a different gate chain")
    if parse_rfc3339(attempt.started_at) < parse_rfc3339(parent.finished_at):
        _invalid("semantic retry must not precede parent completion")


def dumps_system_gate_evaluation(evaluation: SystemGateEvaluation) -> str:
    return _dumps_exact(evaluation, SystemGateEvaluation, "evaluation")


def dumps_semantic_gate_attempt(attempt: SemanticGateAttempt) -> str:
    return _dumps_exact(attempt, SemanticGateAttempt, "attempt")


def loads_system_gate_evaluation(document: str | bytes) -> SystemGateEvaluation:
    return parse_system_gate_evaluation(_loads_object(document, "System Gate"))


def loads_semantic_gate_attempt(document: str | bytes) -> SemanticGateAttempt:
    return parse_semantic_gate_attempt(_loads_object(document, "Semantic Gate"))


def parse_system_gate_evaluation(
    payload: Mapping[str, object],
) -> SystemGateEvaluation:
    item = _strict_object(payload, "System Gate evaluation", _SYSTEM_FIELDS)
    decision_values = item["decisions"]
    if type(decision_values) is not list:
        _invalid("decisions must be an array")
    if len(decision_values) > GATE_EVALUATION_MAX_DECISIONS:
        _invalid("decisions must be a bounded array")
    decisions = tuple(_parse_system_decision(value) for value in decision_values)
    return SystemGateEvaluation(
        evaluation_id=cast(str, item["evaluation_id"]),
        session_id=cast(str, item["session_id"]),
        retrieval_snapshot_id=cast(str, item["retrieval_snapshot_id"]),
        authorization_event_id=cast(str, item["authorization_event_id"]),
        policy_bundle_sha256=cast(str, item["policy_bundle_sha256"]),
        evaluator_id=cast(str, item["evaluator_id"]),
        evaluator_version=cast(str, item["evaluator_version"]),
        decisions=decisions,
        evaluated_at=cast(str, item["evaluated_at"]),
        contract_version=cast(str, item["contract_version"]),
    )


def parse_semantic_gate_attempt(
    payload: Mapping[str, object],
) -> SemanticGateAttempt:
    item = dict(
        _strict_object(payload, "Semantic Gate attempt", _ATTEMPT_FIELDS)
    )
    item["final_allowed_revision_ids"] = _parse_revision_ids(
        item["final_allowed_revision_ids"],
        "final_allowed_revision_ids",
    )
    item["final_blocked_revision_ids"] = _parse_revision_ids(
        item["final_blocked_revision_ids"],
        "final_blocked_revision_ids",
    )
    return SemanticGateAttempt(**item)  # type: ignore[arg-type]


def _parse_system_decision(value: object) -> SystemGateDecision:
    item = _strict_object(value, "System Gate decision", _DECISION_FIELDS)
    return SystemGateDecision(
        memory_revision_id=cast(str, item["memory_revision_id"]),
        candidate_sha256=cast(str, item["candidate_sha256"]),
        outcome=cast(GateOutcome, item["outcome"]),
        reason_code=cast(str, item["reason_code"]),
        rule_id=cast(str, item["rule_id"]),
    )


def _dumps_exact(value: object, expected: type[object], label: str) -> str:
    if type(value) is not expected:
        _invalid(f"{label} has the wrong record type")
    return json.dumps(
        value.to_dict(),  # type: ignore[attr-defined]
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _loads_object(document: str | bytes, label: str) -> dict[str, object]:
    if type(document) is bytes:
        try:
            source = decode_bounded_utf8(
                document,
                max_bytes=GATE_EVALUATION_JSON_MAX_BYTES,
                description=f"{label} JSON",
            )
        except (UnicodeError, ValueError) as error:
            raise GateEvaluationContractError(
                "TBM_GATE_EVALUATION_INVALID_JSON",
                f"{label} must be bounded strict JSON",
            ) from error
    else:
        source = document
    try:
        if type(source) is not str:
            raise ValueError(f"{label} source must be str or bytes")
        if len(source.encode("utf-8")) > GATE_EVALUATION_JSON_MAX_BYTES:
            raise ValueError(f"{label} exceeds byte limit")
        payload = parse_bounded_json(
            source,
            description=label,
            max_nodes=GATE_EVALUATION_JSON_MAX_NODES,
            max_depth=GATE_EVALUATION_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise GateEvaluationContractError(
            "TBM_GATE_EVALUATION_INVALID_JSON",
            f"{label} must be bounded strict JSON",
        ) from error
    if type(payload) is not dict:
        _invalid(f"{label} must be an object")
    return cast(dict[str, object], payload)


def _system_decisions(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > GATE_EVALUATION_MAX_DECISIONS
        or any(type(item) is not SystemGateDecision for item in value)
    ):
        _invalid("decisions must be a bounded tuple")
    decisions = cast(tuple[SystemGateDecision, ...], value)
    ids = tuple(item.memory_revision_id for item in decisions)
    if len(set(ids)) != len(ids):
        _invalid("decisions must not repeat a memory revision")


def _builder_revision_ids(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        _invalid(f"{label} must be a tuple")
    _revision_ids(value, label)
    return cast(tuple[str, ...], value)


def _parse_revision_ids(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        _invalid(f"{label} must be an array")
    if len(value) > GATE_EVALUATION_MAX_DECISIONS:
        _invalid(f"{label} must be a bounded array")
    result = tuple(cast(list[object], value))
    _revision_ids(result, label)
    return cast(tuple[str, ...], result)


def _revision_ids(value: object, label: str) -> None:
    if (
        type(value) is not tuple
        or len(value) > GATE_EVALUATION_MAX_DECISIONS
    ):
        _invalid(f"{label} must be a bounded tuple")
    ids = cast(tuple[object, ...], value)
    if any(
        type(item) is not str or not _REVISION_ID_RE.fullmatch(item) for item in ids
    ):
        _invalid(f"{label} contains an invalid memory revision ID")
    if len(set(ids)) != len(ids):
        _invalid(f"{label} must not contain duplicates")
    if tuple(sorted(cast(tuple[str, ...], ids))) != ids:
        _invalid(f"{label} must use canonical order")


def _bounded_nonnegative(value: object, label: str, *, optional: bool) -> None:
    if optional and value is None:
        return
    if type(value) is not int or value < 0 or value > 2_147_483_647:
        _invalid(f"{label} must be a bounded non-negative integer")


def _strict_object(
    value: object,
    label: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _invalid(f"{label} must be an object")
    item = cast(dict[object, object], value)
    if any(type(key) is not str for key in item):
        _invalid(f"{label} keys must be strings")
    if frozenset(cast(dict[str, object], item)) != fields:
        _invalid(f"{label} fields do not match the contract")
    return cast(dict[str, object], item)


def _snapshot_id(value: object) -> None:
    if type(value) is not str or not _SNAPSHOT_ID_RE.fullmatch(value):
        _invalid("retrieval_snapshot_id must reference a retrieval snapshot")


def _revision_id(value: object) -> None:
    if type(value) is not str or not _REVISION_ID_RE.fullmatch(value):
        _invalid("memory_revision_id must reference a memory revision")


def _optional_identifier(value: object, label: str) -> None:
    if value is not None:
        _identifier(value, label)


def _identifier(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _IDENTIFIER_MAX_CHARS
    ):
        _invalid(f"{label} must be a nonblank bounded identifier")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeError as error:
        raise GateEvaluationContractError(
            "TBM_GATE_EVALUATION_INVALID",
            f"{label} must be valid UTF-8",
        ) from error


def _text(value: object, label: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > _TEXT_MAX_CHARS:
        _invalid(f"{label} must be nonblank bounded text")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeError as error:
        raise GateEvaluationContractError(
            "TBM_GATE_EVALUATION_INVALID",
            f"{label} must be valid UTF-8",
        ) from error


def _digest(value: object, label: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{label} must be sha256:<64 lowercase hex>")


def _canonical_timestamp(value: object, label: str) -> str:
    _timestamp(value, label)
    return canonical_rfc3339(cast(str, value))


def _timestamp(value: object, label: str) -> None:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(cast(str, value)):
        _invalid(f"{label} must be an RFC 3339 timestamp")
    try:
        parse_rfc3339(cast(str, value))
    except ValueError as error:
        raise GateEvaluationContractError(
            "TBM_GATE_EVALUATION_INVALID",
            f"{label} must be an RFC 3339 timestamp",
        ) from error
