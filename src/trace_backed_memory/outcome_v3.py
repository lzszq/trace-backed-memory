from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import canonical_sha256
from .gate_session_v3 import GateSession


RUN_OUTCOME_CONTRACT_VERSION = "tbm.run-outcome.v3"
OUTCOME_ATTRIBUTION_CONTRACT_VERSION = "tbm.outcome-attribution.v3"
OUTCOME_JSON_MAX_BYTES = 1024 * 1024
OUTCOME_JSON_MAX_DEPTH = 32
OUTCOME_JSON_MAX_NODES = 10_000
OUTCOME_MAX_ARTIFACTS = 64
OUTCOME_MAX_MEMORY_REVISIONS = 50
OUTCOME_MAX_LATENCY_MS = 2_147_483_647

_IDENTIFIER_MAX_CHARS = 128
_TEXT_MAX_CHARS = 1024
_RUN_OUTCOME_ID_RE = re.compile(r"^run_outcome_sha256_[0-9a-f]{64}$")
_ATTRIBUTION_ID_RE = re.compile(
    r"^outcome_attribution_sha256_[0-9a-f]{64}$"
)
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESULTS = frozenset({"pass", "fail", "error"})
_CLAIM_STRENGTHS = frozenset({"association", "causal"})
_EFFECTS = frozenset({"helped", "harmed", "neutral", "unknown"})
_METHODS = frozenset(
    {
        "runtime_observation",
        "controlled_experiment",
        "manual_review",
        "external_evaluation",
    }
)
_CAUSAL_METHODS = frozenset(
    {"controlled_experiment", "manual_review", "external_evaluation"}
)
_RUN_OUTCOME_FIELDS = frozenset(
    {
        "contract_version",
        "run_outcome_id",
        "session_id",
        "trace_id",
        "run_id",
        "usage_decision_id",
        "result",
        "evaluator_id",
        "evaluator_version",
        "output_sha256",
        "tool_outputs_sha256",
        "evidence_artifact_sha256s",
        "latency_ms",
        "cost_usd",
        "error_code",
        "measured_at",
    }
)
_ATTRIBUTION_FIELDS = frozenset(
    {
        "contract_version",
        "attribution_id",
        "run_outcome_id",
        "usage_decision_id",
        "memory_revision_ids",
        "claim_strength",
        "effect",
        "method",
        "evaluator_id",
        "evaluator_version",
        "verifier_id",
        "evidence_artifact_sha256s",
        "confidence",
        "reason",
        "recorded_at",
    }
)

RunResult = Literal["pass", "fail", "error"]
AttributionClaimStrength = Literal["association", "causal"]
AttributionEffect = Literal["helped", "harmed", "neutral", "unknown"]
AttributionMethod = Literal[
    "runtime_observation",
    "controlled_experiment",
    "manual_review",
    "external_evaluation",
]


class OutcomeContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise OutcomeContractError("TBM_OUTCOME_INVALID", message)


@dataclass(frozen=True)
class RunOutcome:
    """Immutable measured result for one completed GateSession run."""

    run_outcome_id: str
    session_id: str
    trace_id: str
    run_id: str
    usage_decision_id: str
    result: RunResult
    evaluator_id: str
    evaluator_version: str
    evidence_artifact_sha256s: tuple[str, ...]
    measured_at: str
    output_sha256: str | None = None
    tool_outputs_sha256: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    error_code: str | None = None
    contract_version: str = RUN_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUN_OUTCOME_CONTRACT_VERSION:
            _invalid(
                f"contract_version must be {RUN_OUTCOME_CONTRACT_VERSION}"
            )
        if type(self.run_outcome_id) is not str or not _RUN_OUTCOME_ID_RE.fullmatch(
            self.run_outcome_id
        ):
            _invalid("run_outcome_id must be a canonical content identifier")
        for name in (
            "session_id",
            "trace_id",
            "run_id",
            "usage_decision_id",
            "evaluator_id",
            "evaluator_version",
        ):
            _identifier(getattr(self, name), name)
        if type(self.result) is not str or self.result not in _RESULTS:
            _invalid("result must be pass, fail, or error")
        _optional_digest(self.output_sha256, "output_sha256")
        _optional_digest(self.tool_outputs_sha256, "tool_outputs_sha256")
        _digest_tuple(
            self.evidence_artifact_sha256s,
            "evidence_artifact_sha256s",
            minimum=1,
            maximum=OUTCOME_MAX_ARTIFACTS,
        )
        if self.output_sha256 is None and self.tool_outputs_sha256 is None:
            _invalid(
                "at least one of output_sha256 or tool_outputs_sha256 is required"
            )
        if self.latency_ms is not None and (
            type(self.latency_ms) is not int
            or not 0 <= self.latency_ms <= OUTCOME_MAX_LATENCY_MS
        ):
            _invalid("latency_ms must be a bounded non-negative integer or null")
        if self.cost_usd is not None:
            if (
                type(self.cost_usd) is not float
                or not math.isfinite(self.cost_usd)
                or self.cost_usd < 0
            ):
                _invalid(
                    "cost_usd must be a canonical finite non-negative "
                    "float or null"
                )
        if self.result == "error":
            _text(self.error_code, "error_code")
        elif self.error_code is not None:
            _invalid("error_code is only permitted for an error result")
        _timestamp(self.measured_at, "measured_at")
        if self.run_outcome_id != run_outcome_id(self.to_dict(include_id=False)):
            _invalid("run_outcome_id does not match the canonical payload")

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "usage_decision_id": self.usage_decision_id,
            "result": self.result,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "output_sha256": self.output_sha256,
            "tool_outputs_sha256": self.tool_outputs_sha256,
            "evidence_artifact_sha256s": list(
                self.evidence_artifact_sha256s
            ),
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "error_code": self.error_code,
            "measured_at": self.measured_at,
        }
        if include_id:
            value["run_outcome_id"] = self.run_outcome_id
        return value


@dataclass(frozen=True)
class OutcomeAttribution:
    """Explicit association or independently supported causal claim."""

    attribution_id: str
    run_outcome_id: str
    usage_decision_id: str
    memory_revision_ids: tuple[str, ...]
    claim_strength: AttributionClaimStrength
    effect: AttributionEffect
    method: AttributionMethod
    evaluator_id: str
    evaluator_version: str
    evidence_artifact_sha256s: tuple[str, ...]
    confidence: float
    reason: str
    recorded_at: str
    verifier_id: str | None = None
    contract_version: str = OUTCOME_ATTRIBUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != OUTCOME_ATTRIBUTION_CONTRACT_VERSION:
            _invalid(
                "contract_version must be "
                f"{OUTCOME_ATTRIBUTION_CONTRACT_VERSION}"
            )
        if type(self.attribution_id) is not str or not _ATTRIBUTION_ID_RE.fullmatch(
            self.attribution_id
        ):
            _invalid("attribution_id must be a canonical content identifier")
        if type(self.run_outcome_id) is not str or not _RUN_OUTCOME_ID_RE.fullmatch(
            self.run_outcome_id
        ):
            _invalid("run_outcome_id must be a canonical content identifier")
        for name in ("usage_decision_id", "evaluator_id", "evaluator_version"):
            _identifier(getattr(self, name), name)
        _revision_tuple(self.memory_revision_ids)
        if (
            type(self.claim_strength) is not str
            or self.claim_strength not in _CLAIM_STRENGTHS
        ):
            _invalid("claim_strength must be association or causal")
        if type(self.effect) is not str or self.effect not in _EFFECTS:
            _invalid("effect is not supported")
        if type(self.method) is not str or self.method not in _METHODS:
            _invalid("method is not supported")
        if self.claim_strength == "association":
            if self.method != "runtime_observation":
                _invalid(
                    "association claims must use runtime_observation"
                )
            if self.verifier_id is not None:
                _invalid("association claims cannot name verifier_id")
        else:
            if self.method not in _CAUSAL_METHODS:
                _invalid("causal claims require a non-observational method")
            _identifier(self.verifier_id, "verifier_id")
            if self.verifier_id == self.evaluator_id:
                _invalid("causal verifier_id must differ from evaluator_id")
            if self.effect == "unknown":
                _invalid("causal claims cannot have an unknown effect")
        _digest_tuple(
            self.evidence_artifact_sha256s,
            "evidence_artifact_sha256s",
            minimum=1,
            maximum=OUTCOME_MAX_ARTIFACTS,
        )
        if (
            type(self.confidence) is not float
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            _invalid("confidence must be a finite number from 0 to 1")
        _text(self.reason, "reason")
        _timestamp(self.recorded_at, "recorded_at")
        if self.attribution_id != outcome_attribution_id(
            self.to_dict(include_id=False)
        ):
            _invalid("attribution_id does not match the canonical payload")

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "run_outcome_id": self.run_outcome_id,
            "usage_decision_id": self.usage_decision_id,
            "memory_revision_ids": list(self.memory_revision_ids),
            "claim_strength": self.claim_strength,
            "effect": self.effect,
            "method": self.method,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "verifier_id": self.verifier_id,
            "evidence_artifact_sha256s": list(
                self.evidence_artifact_sha256s
            ),
            "confidence": self.confidence,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
        }
        if include_id:
            value["attribution_id"] = self.attribution_id
        return value


def run_outcome_id(payload: Mapping[str, object]) -> str:
    return "run_outcome_sha256_" + canonical_sha256(payload).removeprefix(
        "sha256:"
    )


def outcome_attribution_id(payload: Mapping[str, object]) -> str:
    return "outcome_attribution_sha256_" + canonical_sha256(
        payload
    ).removeprefix("sha256:")


def build_run_outcome(
    *,
    session_id: str,
    trace_id: str,
    run_id: str,
    usage_decision_id: str,
    result: RunResult,
    evaluator_id: str,
    evaluator_version: str,
    evidence_artifact_sha256s: tuple[str, ...],
    measured_at: str,
    output_sha256: str | None = None,
    tool_outputs_sha256: str | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    error_code: str | None = None,
) -> RunOutcome:
    canonical_artifacts = tuple(sorted(evidence_artifact_sha256s))
    canonical_cost = (
        None
        if cost_usd is None
        else _canonical_float(cost_usd, "cost_usd", minimum=0)
    )
    payload: dict[str, object] = {
        "contract_version": RUN_OUTCOME_CONTRACT_VERSION,
        "session_id": session_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "usage_decision_id": usage_decision_id,
        "result": result,
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
        "output_sha256": output_sha256,
        "tool_outputs_sha256": tool_outputs_sha256,
        "evidence_artifact_sha256s": list(canonical_artifacts),
        "latency_ms": latency_ms,
        "cost_usd": canonical_cost,
        "error_code": error_code,
        "measured_at": canonical_rfc3339(measured_at),
    }
    return RunOutcome(
        run_outcome_id=run_outcome_id(payload),
        session_id=session_id,
        trace_id=trace_id,
        run_id=run_id,
        usage_decision_id=usage_decision_id,
        result=result,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        output_sha256=output_sha256,
        tool_outputs_sha256=tool_outputs_sha256,
        evidence_artifact_sha256s=canonical_artifacts,
        latency_ms=latency_ms,
        cost_usd=canonical_cost,
        error_code=error_code,
        measured_at=cast(str, payload["measured_at"]),
    )


def build_outcome_attribution(
    *,
    run_outcome_id_value: str,
    usage_decision_id: str,
    memory_revision_ids: tuple[str, ...],
    claim_strength: AttributionClaimStrength,
    effect: AttributionEffect,
    method: AttributionMethod,
    evaluator_id: str,
    evaluator_version: str,
    evidence_artifact_sha256s: tuple[str, ...],
    confidence: float,
    reason: str,
    recorded_at: str,
    verifier_id: str | None = None,
) -> OutcomeAttribution:
    canonical_revisions = tuple(sorted(memory_revision_ids))
    canonical_artifacts = tuple(sorted(evidence_artifact_sha256s))
    canonical_confidence = _canonical_float(
        confidence,
        "confidence",
        minimum=0,
        maximum=1,
    )
    payload: dict[str, object] = {
        "contract_version": OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
        "run_outcome_id": run_outcome_id_value,
        "usage_decision_id": usage_decision_id,
        "memory_revision_ids": list(canonical_revisions),
        "claim_strength": claim_strength,
        "effect": effect,
        "method": method,
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
        "verifier_id": verifier_id,
        "evidence_artifact_sha256s": list(canonical_artifacts),
        "confidence": canonical_confidence,
        "reason": reason,
        "recorded_at": canonical_rfc3339(recorded_at),
    }
    return OutcomeAttribution(
        attribution_id=outcome_attribution_id(payload),
        run_outcome_id=run_outcome_id_value,
        usage_decision_id=usage_decision_id,
        memory_revision_ids=canonical_revisions,
        claim_strength=claim_strength,
        effect=effect,
        method=method,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        verifier_id=verifier_id,
        evidence_artifact_sha256s=canonical_artifacts,
        confidence=canonical_confidence,
        reason=reason,
        recorded_at=cast(str, payload["recorded_at"]),
    )


def verify_run_outcome(outcome: RunOutcome, session: GateSession) -> None:
    if outcome.session_id != session.session_id:
        _invalid("run outcome session_id does not match GateSession")
    if outcome.trace_id != session.trace_id or outcome.run_id != session.run_id:
        _invalid("run outcome trace/run identity does not match GateSession")
    if outcome.usage_decision_id != session.usage_decision_id:
        _invalid("run outcome usage_decision_id does not match GateSession")
    if session.status != "completed":
        _invalid("run outcome requires a completed GateSession")
    if session.run_outcome_id != outcome.run_outcome_id:
        _invalid("GateSession run_outcome_id does not match outcome")
    if parse_rfc3339(outcome.measured_at) < parse_rfc3339(session.updated_at):
        _invalid("run outcome measured_at precedes GateSession completion")


def verify_outcome_attribution(
    attribution: OutcomeAttribution,
    outcome: RunOutcome,
    session: GateSession,
) -> None:
    verify_run_outcome(outcome, session)
    if attribution.run_outcome_id != outcome.run_outcome_id:
        _invalid("attribution run_outcome_id does not match outcome")
    if attribution.usage_decision_id != outcome.usage_decision_id:
        _invalid("attribution usage_decision_id does not match outcome")
    if parse_rfc3339(attribution.recorded_at) < parse_rfc3339(
        outcome.measured_at
    ):
        _invalid("attribution recorded_at precedes outcome measured_at")
    if not set(attribution.memory_revision_ids).issubset(
        session.final_memory_revision_ids
    ):
        _invalid(
            "attribution memory_revision_ids were not finalized in GateSession"
        )


def dumps_run_outcome(outcome: RunOutcome) -> str:
    return _dumps(outcome.to_dict())


def dumps_outcome_attribution(attribution: OutcomeAttribution) -> str:
    return _dumps(attribution.to_dict())


def loads_run_outcome(data: str | bytes | bytearray) -> RunOutcome:
    return parse_run_outcome(_loads_object(data))


def loads_outcome_attribution(
    data: str | bytes | bytearray,
) -> OutcomeAttribution:
    return parse_outcome_attribution(_loads_object(data))


def parse_run_outcome(value: Mapping[str, object]) -> RunOutcome:
    obj = _strict_object(value, _RUN_OUTCOME_FIELDS, "RunOutcome")
    artifacts = _parse_string_array(
        obj["evidence_artifact_sha256s"],
        "evidence_artifact_sha256s",
        OUTCOME_MAX_ARTIFACTS,
    )
    return RunOutcome(
        run_outcome_id=_as_str(obj["run_outcome_id"], "run_outcome_id"),
        session_id=_as_str(obj["session_id"], "session_id"),
        trace_id=_as_str(obj["trace_id"], "trace_id"),
        run_id=_as_str(obj["run_id"], "run_id"),
        usage_decision_id=_as_str(
            obj["usage_decision_id"], "usage_decision_id"
        ),
        result=cast(RunResult, obj["result"]),
        evaluator_id=_as_str(obj["evaluator_id"], "evaluator_id"),
        evaluator_version=_as_str(
            obj["evaluator_version"], "evaluator_version"
        ),
        output_sha256=_as_optional_str(
            obj["output_sha256"], "output_sha256"
        ),
        tool_outputs_sha256=_as_optional_str(
            obj["tool_outputs_sha256"], "tool_outputs_sha256"
        ),
        evidence_artifact_sha256s=artifacts,
        latency_ms=_as_optional_int(obj["latency_ms"], "latency_ms"),
        cost_usd=_as_optional_number(obj["cost_usd"], "cost_usd"),
        error_code=_as_optional_str(obj["error_code"], "error_code"),
        measured_at=_as_str(obj["measured_at"], "measured_at"),
        contract_version=_as_str(
            obj["contract_version"], "contract_version"
        ),
    )


def parse_outcome_attribution(
    value: Mapping[str, object],
) -> OutcomeAttribution:
    obj = _strict_object(value, _ATTRIBUTION_FIELDS, "OutcomeAttribution")
    revisions = _parse_string_array(
        obj["memory_revision_ids"],
        "memory_revision_ids",
        OUTCOME_MAX_MEMORY_REVISIONS,
    )
    artifacts = _parse_string_array(
        obj["evidence_artifact_sha256s"],
        "evidence_artifact_sha256s",
        OUTCOME_MAX_ARTIFACTS,
    )
    confidence = _canonical_float(
        obj["confidence"],
        "confidence",
        minimum=0,
        maximum=1,
    )
    return OutcomeAttribution(
        attribution_id=_as_str(obj["attribution_id"], "attribution_id"),
        run_outcome_id=_as_str(obj["run_outcome_id"], "run_outcome_id"),
        usage_decision_id=_as_str(
            obj["usage_decision_id"], "usage_decision_id"
        ),
        memory_revision_ids=revisions,
        claim_strength=cast(
            AttributionClaimStrength, obj["claim_strength"]
        ),
        effect=cast(AttributionEffect, obj["effect"]),
        method=cast(AttributionMethod, obj["method"]),
        evaluator_id=_as_str(obj["evaluator_id"], "evaluator_id"),
        evaluator_version=_as_str(
            obj["evaluator_version"], "evaluator_version"
        ),
        verifier_id=_as_optional_str(obj["verifier_id"], "verifier_id"),
        evidence_artifact_sha256s=artifacts,
        confidence=confidence,
        reason=_as_str(obj["reason"], "reason"),
        recorded_at=_as_str(obj["recorded_at"], "recorded_at"),
        contract_version=_as_str(
            obj["contract_version"], "contract_version"
        ),
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
                max_bytes=OUTCOME_JSON_MAX_BYTES,
                description="outcome JSON",
            )
        elif type(data) is str:
            text = data
            if len(text.encode("utf-8")) > OUTCOME_JSON_MAX_BYTES:
                raise ValueError("outcome JSON exceeds byte limit")
        else:
            raise TypeError("outcome JSON must be str, bytes, or bytearray")
        value = parse_bounded_json(
            text,
            description="outcome",
            max_depth=OUTCOME_JSON_MAX_DEPTH,
            max_nodes=OUTCOME_JSON_MAX_NODES,
        )
    except (TypeError, ValueError) as exc:
        raise OutcomeContractError(
            "TBM_OUTCOME_INVALID_JSON", str(exc)
        ) from exc
    if not isinstance(value, Mapping):
        _invalid("outcome JSON must be an object")
    return cast(Mapping[str, object], value)


def _strict_object(
    value: Mapping[str, object],
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{label} must be an object")
    obj = dict(value)
    if set(obj) != fields:
        _invalid(f"{label} fields do not match the contract")
    if any(type(key) is not str for key in obj):
        _invalid(f"{label} keys must be strings")
    return obj


def _parse_string_array(
    value: object,
    name: str,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _invalid(f"{name} must be an array")
    if len(value) > maximum:
        _invalid(f"{name} exceeds the item limit")
    if any(type(item) is not str for item in value):
        _invalid(f"{name} items must be strings")
    return tuple(cast(list[str], value))


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _IDENTIFIER_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _invalid(f"{name} must be a bounded identifier")


def _text(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _TEXT_MAX_CHARS
        or value.strip() != value
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        _invalid(f"{name} must be bounded non-empty text")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{name} must be a canonical sha256 digest")


def _optional_digest(value: object, name: str) -> None:
    if value is not None:
        _digest(value, name)


def _digest_tuple(
    values: tuple[str, ...],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if type(values) is not tuple:
        _invalid(f"{name} must be a tuple")
    if not minimum <= len(values) <= maximum:
        _invalid(f"{name} has an invalid item count")
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        _invalid(f"{name} must be sorted and unique")
    for value in values:
        _digest(value, name)


def _revision_tuple(values: tuple[str, ...]) -> None:
    if type(values) is not tuple:
        _invalid("memory_revision_ids must be a tuple")
    if not 1 <= len(values) <= OUTCOME_MAX_MEMORY_REVISIONS:
        _invalid("memory_revision_ids has an invalid item count")
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        _invalid("memory_revision_ids must be sorted and unique")
    if any(
        type(value) is not str or not _REVISION_ID_RE.fullmatch(value)
        for value in values
    ):
        _invalid("memory_revision_ids must contain canonical revision IDs")


def _timestamp(value: object, name: str):
    if type(value) is not str:
        _invalid(f"{name} must be an RFC3339 timestamp")
    try:
        parsed = parse_rfc3339(value)
    except (TypeError, ValueError) as exc:
        _invalid(f"{name} must be an RFC3339 timestamp: {exc}")
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


def _as_optional_int(value: object, name: str) -> int | None:
    if value is not None and type(value) is not int:
        _invalid(f"{name} must be an integer or null")
    return cast(int | None, value)


def _as_optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _canonical_float(value, name, minimum=0)


def _canonical_float(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{name} must be a number")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise OutcomeContractError(
            "TBM_OUTCOME_INVALID",
            f"{name} must be a bounded finite number",
        ) from exc
    if (
        not math.isfinite(normalized)
        or normalized < minimum
        or (maximum is not None and normalized > maximum)
    ):
        _invalid(f"{name} must be a bounded finite number")
    return normalized
