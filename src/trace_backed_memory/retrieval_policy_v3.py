from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Literal, Mapping, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from .contracts_v3 import canonical_sha256


RETRIEVAL_POLICY_CONTRACT_VERSION = "tbm.retrieval-policy.v3"
RETRIEVAL_POLICY_JSON_MAX_BYTES = 64 * 1024
RETRIEVAL_POLICY_JSON_MAX_DEPTH = 16
RETRIEVAL_POLICY_JSON_MAX_NODES = 2_000
RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024

TaskMode = Literal["planning", "repair", "debug", "eval", "production"]
MemoryType = Literal["procedural", "semantic", "episodic", "policy"]
DataClassification = Literal[
    "public",
    "internal",
    "confidential",
    "restricted",
]
AncestryMode = Literal["required", "disabled"]
RankingStage = Literal[
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
]

RETRIEVAL_TASK_MODES = (
    "planning",
    "repair",
    "debug",
    "eval",
    "production",
)
_MEMORY_TYPES = ("procedural", "semantic", "episodic", "policy")
_CLASSIFICATIONS = (
    "public",
    "internal",
    "confidential",
    "restricted",
)
_ANCESTRY_MODES = frozenset({"required", "disabled"})
RETRIEVAL_RANKING_STAGES = (
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
)
_TASK_MODES = RETRIEVAL_TASK_MODES
_RANKING_STAGES = RETRIEVAL_RANKING_STAGES
_TASK_MODE_ORDER = {value: index for index, value in enumerate(_TASK_MODES)}
_MEMORY_TYPE_ORDER = {
    value: index for index, value in enumerate(_MEMORY_TYPES)
}
_CLASSIFICATION_ORDER = {
    value: index for index, value in enumerate(_CLASSIFICATIONS)
}
_STAGE_ORDER = {
    value: index for index, value in enumerate(_RANKING_STAGES)
}
_POLICY_ID_RE = re.compile(r"^retrieval_policy_sha256_[0-9a-f]{64}$")
_POLICY_FIELDS = frozenset(
    {
        "contract_version",
        "policy_id",
        "policy_version",
        "allowed_classifications",
        "mode_memory_rules",
        "ancestry_mode",
        "ancestry_bypass_reason",
        "stage_weights",
        "minimum_fused_score",
        "payload_budget_bytes",
        "block_eval_leaking",
    }
)
_MODE_RULE_FIELDS = frozenset({"task_mode", "allowed_memory_types"})


class RetrievalPolicyV3ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise RetrievalPolicyV3ContractError(
        "TBM_RETRIEVAL_POLICY_INVALID",
        message,
    )


@dataclass(frozen=True)
class ModeMemoryRule:
    task_mode: TaskMode
    allowed_memory_types: tuple[MemoryType, ...]

    def __post_init__(self) -> None:
        if type(self.task_mode) is not str or self.task_mode not in _TASK_MODES:
            _invalid("task_mode is not supported")
        if (
            type(self.allowed_memory_types) is not tuple
            or not self.allowed_memory_types
            or any(
                type(item) is not str or item not in _MEMORY_TYPES
                for item in self.allowed_memory_types
            )
            or len(set(self.allowed_memory_types))
            != len(self.allowed_memory_types)
        ):
            _invalid(
                "allowed_memory_types must be a unique non-empty tuple"
            )
        canonical = tuple(
            sorted(
                self.allowed_memory_types,
                key=_MEMORY_TYPE_ORDER.__getitem__,
            )
        )
        if self.allowed_memory_types != canonical:
            _invalid("allowed_memory_types must use canonical order")

    def to_dict(self) -> dict[str, object]:
        return {
            "task_mode": self.task_mode,
            "allowed_memory_types": list(self.allowed_memory_types),
        }


@dataclass(frozen=True)
class RetrievalPolicyBundle:
    policy_id: str
    policy_version: str
    allowed_classifications: tuple[DataClassification, ...]
    mode_memory_rules: tuple[ModeMemoryRule, ...]
    ancestry_mode: AncestryMode
    ancestry_bypass_reason: str | None
    stage_weights: tuple[tuple[RankingStage, float], ...]
    minimum_fused_score: float
    payload_budget_bytes: int
    block_eval_leaking: bool = True
    contract_version: str = RETRIEVAL_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RETRIEVAL_POLICY_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.policy_id) is not str or not _POLICY_ID_RE.fullmatch(
            self.policy_id
        ):
            _invalid(
                "policy_id must be retrieval_policy_sha256_<64 lowercase hex>"
            )
        _metadata(self.policy_version, "policy_version")
        _classifications(self.allowed_classifications)
        _mode_rules(self.mode_memory_rules)
        if (
            type(self.ancestry_mode) is not str
            or self.ancestry_mode not in _ANCESTRY_MODES
        ):
            _invalid("ancestry_mode must be required or disabled")
        if self.ancestry_mode == "required":
            if self.ancestry_bypass_reason is not None:
                _invalid(
                    "required ancestry cannot declare a bypass reason"
                )
        else:
            _metadata(
                self.ancestry_bypass_reason,
                "ancestry_bypass_reason",
            )
        object.__setattr__(
            self,
            "stage_weights",
            _weights(self.stage_weights),
        )
        object.__setattr__(
            self,
            "minimum_fused_score",
            _unit_float(
                self.minimum_fused_score,
                "minimum_fused_score",
            ),
        )
        if (
            type(self.payload_budget_bytes) is not int
            or self.payload_budget_bytes < 1
            or self.payload_budget_bytes > RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES
        ):
            _invalid("payload_budget_bytes is outside the supported bound")
        if self.block_eval_leaking is not True:
            _invalid("block_eval_leaking must remain true")
        if self.policy_id != retrieval_policy_id(self._unsigned_dict()):
            raise RetrievalPolicyV3ContractError(
                "TBM_RETRIEVAL_POLICY_HASH_MISMATCH",
                "policy_id does not match canonical retrieval policy content",
            )

    @property
    def policy_sha256(self) -> str:
        return "sha256:" + self.policy_id.removeprefix(
            "retrieval_policy_sha256_"
        )

    def allowed_types(self, task_mode: TaskMode) -> frozenset[MemoryType]:
        for rule in self.mode_memory_rules:
            if rule.task_mode == task_mode:
                return frozenset(rule.allowed_memory_types)
        return frozenset()

    def weight(self, stage: RankingStage) -> float:
        for name, value in self.stage_weights:
            if name == stage:
                return value
        raise AssertionError("validated retrieval policy is missing a stage")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "policy_version": self.policy_version,
            "allowed_classifications": list(self.allowed_classifications),
            "mode_memory_rules": [
                rule.to_dict() for rule in self.mode_memory_rules
            ],
            "ancestry_mode": self.ancestry_mode,
            "ancestry_bypass_reason": self.ancestry_bypass_reason,
            "stage_weights": dict(self.stage_weights),
            "minimum_fused_score": self.minimum_fused_score,
            "payload_budget_bytes": self.payload_budget_bytes,
            "block_eval_leaking": self.block_eval_leaking,
        }

    def to_dict(self) -> dict[str, object]:
        return {"policy_id": self.policy_id, **self._unsigned_dict()}


def retrieval_policy_id(payload: Mapping[str, object]) -> str:
    return "retrieval_policy_sha256_" + canonical_sha256(
        payload
    ).removeprefix("sha256:")


def build_retrieval_policy(
    *,
    policy_version: str,
    allowed_classifications: tuple[DataClassification, ...],
    mode_memory_rules: tuple[ModeMemoryRule, ...],
    ancestry_mode: AncestryMode,
    ancestry_bypass_reason: str | None,
    stage_weights: tuple[tuple[RankingStage, float], ...],
    minimum_fused_score: float,
    payload_budget_bytes: int,
) -> RetrievalPolicyBundle:
    if type(allowed_classifications) is not tuple:
        _invalid("allowed_classifications must be a tuple")
    if type(mode_memory_rules) is not tuple:
        _invalid("mode_memory_rules must be a tuple")
    if type(stage_weights) is not tuple:
        _invalid("stage_weights must be a tuple")
    if any(
        type(item) is not str or item not in _CLASSIFICATIONS
        for item in allowed_classifications
    ):
        _invalid("allowed_classifications contains an unsupported value")
    if any(type(item) is not ModeMemoryRule for item in mode_memory_rules):
        _invalid("mode_memory_rules must contain ModeMemoryRule records")
    canonical_classifications = tuple(
        sorted(
            allowed_classifications,
            key=_CLASSIFICATION_ORDER.__getitem__,
        )
    )
    canonical_rules = tuple(
        sorted(
            mode_memory_rules,
            key=lambda item: _TASK_MODE_ORDER[item.task_mode],
        )
    )
    canonical_weights = _normalize_weights(stage_weights)
    canonical_minimum = _unit_float(
        minimum_fused_score,
        "minimum_fused_score",
    )
    values: dict[str, object] = {
        "contract_version": RETRIEVAL_POLICY_CONTRACT_VERSION,
        "policy_version": policy_version,
        "allowed_classifications": list(canonical_classifications),
        "mode_memory_rules": [
            rule.to_dict() for rule in canonical_rules
        ],
        "ancestry_mode": ancestry_mode,
        "ancestry_bypass_reason": ancestry_bypass_reason,
        "stage_weights": dict(canonical_weights),
        "minimum_fused_score": canonical_minimum,
        "payload_budget_bytes": payload_budget_bytes,
        "block_eval_leaking": True,
    }
    return RetrievalPolicyBundle(
        policy_id=retrieval_policy_id(values),
        policy_version=policy_version,
        allowed_classifications=cast(
            tuple[DataClassification, ...],
            canonical_classifications,
        ),
        mode_memory_rules=canonical_rules,
        ancestry_mode=ancestry_mode,
        ancestry_bypass_reason=ancestry_bypass_reason,
        stage_weights=canonical_weights,
        minimum_fused_score=canonical_minimum,
        payload_budget_bytes=payload_budget_bytes,
    )


def dumps_retrieval_policy(policy: RetrievalPolicyBundle) -> str:
    if type(policy) is not RetrievalPolicyBundle:
        _invalid("policy must be exactly RetrievalPolicyBundle")
    return json.dumps(
        policy.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def loads_retrieval_policy(
    raw: str | bytes | bytearray,
) -> RetrievalPolicyBundle:
    try:
        if isinstance(raw, (bytes, bytearray)):
            text = decode_bounded_utf8(
                raw,
                max_bytes=RETRIEVAL_POLICY_JSON_MAX_BYTES,
                description="retrieval policy JSON",
            )
        elif type(raw) is str:
            if (
                len(raw) > RETRIEVAL_POLICY_JSON_MAX_BYTES
                or len(raw.encode("utf-8"))
                > RETRIEVAL_POLICY_JSON_MAX_BYTES
            ):
                _invalid("retrieval policy JSON exceeds the size bound")
            text = raw
        else:
            _invalid("retrieval policy JSON must be text or bytes")
        value = parse_bounded_json(
            text,
            max_depth=RETRIEVAL_POLICY_JSON_MAX_DEPTH,
            max_nodes=RETRIEVAL_POLICY_JSON_MAX_NODES,
            description="retrieval policy JSON",
        )
    except RetrievalPolicyV3ContractError:
        raise
    except (TypeError, ValueError) as error:
        raise RetrievalPolicyV3ContractError(
            "TBM_RETRIEVAL_POLICY_JSON_INVALID",
            "retrieval policy JSON is invalid",
        ) from error
    return parse_retrieval_policy(value)


def parse_retrieval_policy(
    value: Mapping[str, object] | object,
) -> RetrievalPolicyBundle:
    item = _object(value, "retrieval policy")
    _fields(item, _POLICY_FIELDS, "retrieval policy")
    classifications = _string_tuple(
        item["allowed_classifications"],
        "allowed_classifications",
        maximum=len(_CLASSIFICATIONS),
    )
    raw_rules = _array(
        item["mode_memory_rules"],
        "mode_memory_rules",
        maximum=len(_TASK_MODES),
    )
    rules: list[ModeMemoryRule] = []
    for index, raw_rule in enumerate(raw_rules):
        rule = _object(raw_rule, f"mode_memory_rules[{index}]")
        _fields(rule, _MODE_RULE_FIELDS, f"mode_memory_rules[{index}]")
        rules.append(
            ModeMemoryRule(
                task_mode=cast(
                    TaskMode,
                    _string(rule["task_mode"], "task_mode"),
                ),
                allowed_memory_types=cast(
                    tuple[MemoryType, ...],
                    _string_tuple(
                        rule["allowed_memory_types"],
                        "allowed_memory_types",
                        maximum=len(_MEMORY_TYPES),
                    ),
                ),
            )
        )
    raw_weights = _object(item["stage_weights"], "stage_weights")
    if set(raw_weights) != set(_RANKING_STAGES):
        _invalid("stage_weights must contain every ranking stage")
    weights = tuple(
        (
            cast(RankingStage, stage),
            _number(raw_weights[stage], f"stage_weights.{stage}"),
        )
        for stage in _RANKING_STAGES
    )
    bypass = item["ancestry_bypass_reason"]
    if bypass is not None and type(bypass) is not str:
        _invalid("ancestry_bypass_reason must be a string or null")
    block_eval = item["block_eval_leaking"]
    if type(block_eval) is not bool:
        _invalid("block_eval_leaking must be a boolean")
    return RetrievalPolicyBundle(
        policy_id=_string(item["policy_id"], "policy_id"),
        policy_version=_string(
            item["policy_version"],
            "policy_version",
        ),
        allowed_classifications=cast(
            tuple[DataClassification, ...],
            classifications,
        ),
        mode_memory_rules=tuple(rules),
        ancestry_mode=cast(
            AncestryMode,
            _string(item["ancestry_mode"], "ancestry_mode"),
        ),
        ancestry_bypass_reason=cast(str | None, bypass),
        stage_weights=cast(
            tuple[tuple[RankingStage, float], ...],
            weights,
        ),
        minimum_fused_score=_number(
            item["minimum_fused_score"],
            "minimum_fused_score",
        ),
        payload_budget_bytes=_integer(
            item["payload_budget_bytes"],
            "payload_budget_bytes",
        ),
        block_eval_leaking=block_eval,
        contract_version=_string(
            item["contract_version"],
            "contract_version",
        ),
    )


def _classifications(values: tuple[DataClassification, ...]) -> None:
    if (
        type(values) is not tuple
        or not values
        or any(
            type(item) is not str or item not in _CLASSIFICATIONS
            for item in values
        )
        or len(set(values)) != len(values)
    ):
        _invalid("allowed_classifications must be a unique non-empty tuple")
    if values != tuple(
        sorted(values, key=_CLASSIFICATION_ORDER.__getitem__)
    ):
        _invalid("allowed_classifications must use canonical order")


def _mode_rules(values: tuple[ModeMemoryRule, ...]) -> None:
    if (
        type(values) is not tuple
        or len(values) != len(_TASK_MODES)
        or any(type(item) is not ModeMemoryRule for item in values)
        or {item.task_mode for item in values} != set(_TASK_MODES)
    ):
        _invalid("mode_memory_rules must define every task mode exactly once")
    if values != tuple(
        sorted(values, key=lambda item: _TASK_MODE_ORDER[item.task_mode])
    ):
        _invalid("mode_memory_rules must use canonical order")


def _weights(
    values: tuple[tuple[RankingStage, float], ...],
) -> tuple[tuple[RankingStage, float], ...]:
    if (
        type(values) is not tuple
        or len(values) != len(_RANKING_STAGES)
    ):
        _invalid("stage_weights must contain every ranking stage")
    expected = _normalize_weights(values)
    if values != expected:
        _invalid("stage_weights must use canonical order and float values")
    return expected


def _normalize_weights(
    values: tuple[tuple[RankingStage, float], ...],
) -> tuple[tuple[RankingStage, float], ...]:
    normalized: list[tuple[RankingStage, float]] = []
    for value in values:
        if (
            type(value) is not tuple
            or len(value) != 2
            or type(value[0]) is not str
            or value[0] not in _RANKING_STAGES
        ):
            _invalid("stage_weights contains an invalid stage")
        normalized.append(
            (
                cast(RankingStage, value[0]),
                _positive_unit_float(
                    value[1],
                    f"{value[0]} weight",
                ),
            )
        )
    if (
        len(normalized) != len(_RANKING_STAGES)
        or len({stage for stage, _weight in normalized})
        != len(_RANKING_STAGES)
    ):
        _invalid("stage_weights must contain every stage exactly once")
    return tuple(
        sorted(normalized, key=lambda item: _STAGE_ORDER[item[0]])
    )


def _unit_float(value: object, name: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        _invalid(f"{name} must be between zero and one")
    return result


def _positive_unit_float(value: object, name: str) -> float:
    result = _unit_float(value, name)
    if result == 0.0:
        _invalid(f"{name} must be greater than zero")
    return result


def _metadata(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 128
    ):
        _invalid(f"{name} must be a bounded non-empty string")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeError as error:
        raise RetrievalPolicyV3ContractError(
            "TBM_RETRIEVAL_POLICY_INVALID",
            f"{name} must be valid UTF-8",
        ) from error


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        type(key) is not str for key in value
    ):
        _invalid(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _array(value: object, name: str, *, maximum: int) -> list[object]:
    if type(value) is not list or not value or len(value) > maximum:
        _invalid(f"{name} must be a bounded non-empty array")
    return value


def _fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    name: str,
) -> None:
    if set(value) != set(expected):
        _invalid(f"{name} fields do not match the contract")


def _string(value: object, name: str) -> str:
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return value


def _string_tuple(
    value: object,
    name: str,
    *,
    maximum: int,
) -> tuple[str, ...]:
    items = _array(value, name, maximum=maximum)
    if any(type(item) is not str for item in items):
        _invalid(f"{name} must contain strings")
    return tuple(cast(list[str], items))


def _number(value: object, name: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        _invalid(f"{name} must be finite")
    return result


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        _invalid(f"{name} must be an integer")
    return value


__all__ = [
    "AncestryMode",
    "DataClassification",
    "MemoryType",
    "ModeMemoryRule",
    "RETRIEVAL_POLICY_CONTRACT_VERSION",
    "RETRIEVAL_POLICY_JSON_MAX_BYTES",
    "RETRIEVAL_POLICY_JSON_MAX_DEPTH",
    "RETRIEVAL_POLICY_JSON_MAX_NODES",
    "RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES",
    "RETRIEVAL_RANKING_STAGES",
    "RETRIEVAL_TASK_MODES",
    "RankingStage",
    "RetrievalPolicyBundle",
    "RetrievalPolicyV3ContractError",
    "TaskMode",
    "build_retrieval_policy",
    "dumps_retrieval_policy",
    "loads_retrieval_policy",
    "parse_retrieval_policy",
    "retrieval_policy_id",
]
