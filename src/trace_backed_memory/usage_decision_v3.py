from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError, canonical_sha256
from .gate_session_v3 import GATE_SESSION_MAX_MEMORY_REVISIONS
from .policy import (
    MEMORY_DECISION_REASON_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
)
from .replay_v3 import (
    REPLAY_COMPONENT_NAMES,
    ReplayComponentName,
    StoredReplayArtifact,
    artifact_id_from_sha256,
    create_content_addressed_artifact,
)


USAGE_DECISION_CONTRACT_VERSION = "tbm.usage-decision.v3"
USAGE_DECISION_ARTIFACT_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.usage-decision+json"
)
USAGE_DECISION_JSON_MAX_BYTES = 1024 * 1024
USAGE_DECISION_JSON_MAX_DEPTH = 24
USAGE_DECISION_JSON_MAX_NODES = 4_096

_USAGE_ID_RE = re.compile(r"usage_decision_sha256_[0-9a-f]{64}")
_REVISION_ID_RE = re.compile(r"memory_revision_sha256_[0-9a-f]{64}")
_SNAPSHOT_ID_RE = re.compile(r"retrieval_snapshot_sha256_[0-9a-f]{64}")
_SYSTEM_GATE_ID_RE = re.compile(r"system_gate_sha256_[0-9a-f]{64}")
_SEMANTIC_ATTEMPT_ID_RE = re.compile(r"semantic_attempt_sha256_[0-9a-f]{64}")
_AUTHORIZATION_ID_RE = re.compile(r"authz_sha256_[0-9a-f]{64}")
_ARTIFACT_ID_RE = re.compile(r"artifact_sha256_[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(
    rf"(?=.{{1,{MEMORY_ID_MAX_CHARS}}}\Z)"
    r"(?!\s)(?!.*\s\Z)[^\x00-\x1f\x7f]+",
    re.DOTALL,
)
_RISKS = {"low", "medium", "high"}
_RECOMMENDED_INJECTIONS = {"none", "summary", "full"}
_FIELDS = frozenset(
    {
        "contract_version",
        "usage_decision_id",
        "session_id",
        "decision_id",
        "trace_id",
        "run_id",
        "authorization_event_id",
        "retrieval_snapshot_id",
        "system_gate_evaluation_id",
        "semantic_gate_attempt_id",
        "candidate_memory_revision_ids",
        "system_allowed_memory_revision_ids",
        "semantic_allowed_memory_revision_ids",
        "final_memory_revision_ids",
        "blocked_memory_revision_ids",
        "system_blocked",
        "reason",
        "risk",
        "recommended_injection",
        "renderer_id",
        "renderer_version",
        "policy_bundle_sha256",
        "injection_artifact_id",
        "replay_components",
        "created_at",
    }
)
_ARTIFACT_FIELDS = _FIELDS - {"usage_decision_id"}


class UsageDecisionV3Error(V3ContractError):
    """Stable failure for malformed content-addressed usage decisions."""


@dataclass(frozen=True)
class UsageDecision:
    """Immutable final-use audit bound to one exact rendered injection."""

    usage_decision_id: str
    session_id: str
    decision_id: str
    trace_id: str
    run_id: str
    authorization_event_id: str
    retrieval_snapshot_id: str
    system_gate_evaluation_id: str
    semantic_gate_attempt_id: str
    candidate_memory_revision_ids: tuple[str, ...]
    system_allowed_memory_revision_ids: tuple[str, ...]
    semantic_allowed_memory_revision_ids: tuple[str, ...]
    final_memory_revision_ids: tuple[str, ...]
    blocked_memory_revision_ids: tuple[str, ...]
    system_blocked: tuple[tuple[str, str, str], ...]
    reason: str
    risk: str
    recommended_injection: str
    renderer_id: str
    renderer_version: str
    policy_bundle_sha256: str
    injection_artifact_id: str
    replay_components: tuple[tuple[ReplayComponentName, str], ...]
    created_at: str
    contract_version: str = USAGE_DECISION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != USAGE_DECISION_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.usage_decision_id) is not str or not _USAGE_ID_RE.fullmatch(
            self.usage_decision_id
        ):
            _invalid("usage_decision_id must be content-addressed")
        for name in (
            "session_id",
            "decision_id",
            "trace_id",
            "run_id",
            "renderer_id",
        ):
            _identifier(getattr(self, name), name)
        if type(
            self.authorization_event_id
        ) is not str or not _AUTHORIZATION_ID_RE.fullmatch(self.authorization_event_id):
            _invalid("authorization_event_id is invalid")
        if type(self.retrieval_snapshot_id) is not str or not _SNAPSHOT_ID_RE.fullmatch(
            self.retrieval_snapshot_id
        ):
            _invalid("retrieval_snapshot_id is invalid")
        if type(
            self.system_gate_evaluation_id
        ) is not str or not _SYSTEM_GATE_ID_RE.fullmatch(
            self.system_gate_evaluation_id
        ):
            _invalid("system_gate_evaluation_id is invalid")
        if type(
            self.semantic_gate_attempt_id
        ) is not str or not _SEMANTIC_ATTEMPT_ID_RE.fullmatch(
            self.semantic_gate_attempt_id
        ):
            _invalid("semantic_gate_attempt_id is invalid")
        for name in (
            "candidate_memory_revision_ids",
            "system_allowed_memory_revision_ids",
            "semantic_allowed_memory_revision_ids",
            "final_memory_revision_ids",
            "blocked_memory_revision_ids",
        ):
            _revision_ids(getattr(self, name), name)
        candidates = self.candidate_memory_revision_ids
        _ordered_subset(
            self.system_allowed_memory_revision_ids,
            candidates,
            "system_allowed_memory_revision_ids",
        )
        _ordered_subset(
            self.semantic_allowed_memory_revision_ids,
            self.system_allowed_memory_revision_ids,
            "semantic_allowed_memory_revision_ids",
        )
        _ordered_subset(
            self.final_memory_revision_ids,
            self.semantic_allowed_memory_revision_ids,
            "final_memory_revision_ids",
        )
        expected_blocked = tuple(
            item
            for item in candidates
            if item not in frozenset(self.final_memory_revision_ids)
        )
        if self.blocked_memory_revision_ids != expected_blocked:
            _invalid(
                "blocked_memory_revision_ids must be the ordered candidate "
                "complement of final_memory_revision_ids"
            )
        _system_blocks(
            self.system_blocked,
            candidates,
            self.system_allowed_memory_revision_ids,
        )
        _text(self.reason, "reason", MEMORY_DECISION_REASON_MAX_CHARS)
        if type(self.risk) is not str or self.risk not in _RISKS:
            _invalid("risk is not supported")
        if (
            type(self.recommended_injection) is not str
            or self.recommended_injection not in _RECOMMENDED_INJECTIONS
        ):
            _invalid("recommended_injection is not supported")
        if self.recommended_injection == "none" and self.final_memory_revision_ids:
            _invalid("recommended_injection none requires an empty final set")
        _text(
            self.renderer_version,
            "renderer_version",
            METADATA_VALUE_MAX_CHARS,
        )
        _digest(self.policy_bundle_sha256, "policy_bundle_sha256")
        if type(self.injection_artifact_id) is not str or not _ARTIFACT_ID_RE.fullmatch(
            self.injection_artifact_id
        ):
            _invalid("injection_artifact_id is invalid")
        _components(self.replay_components, self.injection_artifact_id)
        try:
            created_at = canonical_rfc3339(self.created_at)
            parse_rfc3339(created_at)
        except ValueError:
            _invalid("created_at must be a timezone-aware RFC 3339 timestamp")
        object.__setattr__(self, "created_at", created_at)
        expected = usage_decision_id(self._unsigned_dict())
        if self.usage_decision_id != expected:
            raise UsageDecisionV3Error(
                "TBM_USAGE_DECISION_HASH_MISMATCH",
                "usage_decision_id does not match canonical decision content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "decision_id": self.decision_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "authorization_event_id": self.authorization_event_id,
            "retrieval_snapshot_id": self.retrieval_snapshot_id,
            "system_gate_evaluation_id": self.system_gate_evaluation_id,
            "semantic_gate_attempt_id": self.semantic_gate_attempt_id,
            "candidate_memory_revision_ids": list(self.candidate_memory_revision_ids),
            "system_allowed_memory_revision_ids": list(
                self.system_allowed_memory_revision_ids
            ),
            "semantic_allowed_memory_revision_ids": list(
                self.semantic_allowed_memory_revision_ids
            ),
            "final_memory_revision_ids": list(self.final_memory_revision_ids),
            "blocked_memory_revision_ids": list(self.blocked_memory_revision_ids),
            "system_blocked": [
                {
                    "memory_revision_id": revision_id,
                    "reason_code": reason_code,
                    "rule_id": rule_id,
                }
                for revision_id, reason_code, rule_id in self.system_blocked
            ],
            "reason": self.reason,
            "risk": self.risk,
            "recommended_injection": self.recommended_injection,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "policy_bundle_sha256": self.policy_bundle_sha256,
            "injection_artifact_id": self.injection_artifact_id,
            "replay_components": dict(self.replay_components),
            "created_at": canonical_rfc3339(self.created_at),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "usage_decision_id": self.usage_decision_id,
            **self._unsigned_dict(),
        }


def usage_decision_id(content: Mapping[str, object]) -> str:
    return "usage_decision_sha256_" + canonical_sha256(content).removeprefix("sha256:")


def usage_decision_artifact_id(value: str) -> str:
    if type(value) is not str or not _USAGE_ID_RE.fullmatch(value):
        _invalid("usage decision ID is invalid")
    return artifact_id_from_sha256(
        "sha256:" + value.removeprefix("usage_decision_sha256_")
    )


def build_usage_decision(
    *,
    session_id: str,
    decision_id: str,
    trace_id: str,
    run_id: str,
    authorization_event_id: str,
    retrieval_snapshot_id: str,
    system_gate_evaluation_id: str,
    semantic_gate_attempt_id: str,
    candidate_memory_revision_ids: tuple[str, ...],
    system_allowed_memory_revision_ids: tuple[str, ...],
    semantic_allowed_memory_revision_ids: tuple[str, ...],
    final_memory_revision_ids: tuple[str, ...],
    blocked_memory_revision_ids: tuple[str, ...],
    system_blocked: tuple[tuple[str, str, str], ...],
    reason: str,
    risk: str,
    recommended_injection: str,
    renderer_id: str,
    renderer_version: str,
    policy_bundle_sha256: str,
    injection_artifact_id: str,
    replay_components: tuple[tuple[ReplayComponentName, str], ...],
    created_at: str,
) -> UsageDecision:
    unsigned: dict[str, object] = {
        "contract_version": USAGE_DECISION_CONTRACT_VERSION,
        "session_id": session_id,
        "decision_id": decision_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "authorization_event_id": authorization_event_id,
        "retrieval_snapshot_id": retrieval_snapshot_id,
        "system_gate_evaluation_id": system_gate_evaluation_id,
        "semantic_gate_attempt_id": semantic_gate_attempt_id,
        "candidate_memory_revision_ids": list(candidate_memory_revision_ids),
        "system_allowed_memory_revision_ids": list(system_allowed_memory_revision_ids),
        "semantic_allowed_memory_revision_ids": list(
            semantic_allowed_memory_revision_ids
        ),
        "final_memory_revision_ids": list(final_memory_revision_ids),
        "blocked_memory_revision_ids": list(blocked_memory_revision_ids),
        "system_blocked": [
            {
                "memory_revision_id": revision_id,
                "reason_code": reason_code,
                "rule_id": rule_id,
            }
            for revision_id, reason_code, rule_id in system_blocked
        ],
        "reason": reason,
        "risk": risk,
        "recommended_injection": recommended_injection,
        "renderer_id": renderer_id,
        "renderer_version": renderer_version,
        "policy_bundle_sha256": policy_bundle_sha256,
        "injection_artifact_id": injection_artifact_id,
        "replay_components": dict(replay_components),
        "created_at": _canonical_timestamp(created_at),
    }
    return UsageDecision(
        usage_decision_id=usage_decision_id(unsigned),
        session_id=session_id,
        decision_id=decision_id,
        trace_id=trace_id,
        run_id=run_id,
        authorization_event_id=authorization_event_id,
        retrieval_snapshot_id=retrieval_snapshot_id,
        system_gate_evaluation_id=system_gate_evaluation_id,
        semantic_gate_attempt_id=semantic_gate_attempt_id,
        candidate_memory_revision_ids=candidate_memory_revision_ids,
        system_allowed_memory_revision_ids=system_allowed_memory_revision_ids,
        semantic_allowed_memory_revision_ids=(semantic_allowed_memory_revision_ids),
        final_memory_revision_ids=final_memory_revision_ids,
        blocked_memory_revision_ids=blocked_memory_revision_ids,
        system_blocked=system_blocked,
        reason=reason,
        risk=risk,
        recommended_injection=recommended_injection,
        renderer_id=renderer_id,
        renderer_version=renderer_version,
        policy_bundle_sha256=policy_bundle_sha256,
        injection_artifact_id=injection_artifact_id,
        replay_components=replay_components,
        created_at=created_at,
    )


def usage_decision_artifact_payload(decision: UsageDecision) -> bytes:
    if type(decision) is not UsageDecision:
        _invalid("decision must be exactly UsageDecision")
    try:
        return json.dumps(
            decision._unsigned_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _invalid("usage decision cannot be encoded as canonical JSON")


def create_usage_decision_artifact(
    decision: UsageDecision,
) -> StoredReplayArtifact:
    payload = usage_decision_artifact_payload(decision)
    artifact = create_content_addressed_artifact(
        payload,
        media_type=USAGE_DECISION_ARTIFACT_MEDIA_TYPE,
        classification="internal",
        created_at=decision.created_at,
    )
    if artifact.artifact_id != usage_decision_artifact_id(decision.usage_decision_id):
        raise UsageDecisionV3Error(
            "TBM_USAGE_DECISION_ARTIFACT_MISMATCH",
            "usage decision artifact does not match its content-derived ID",
        )
    return StoredReplayArtifact(artifact, payload)


def parse_usage_decision(
    payload: Mapping[str, object],
) -> UsageDecision:
    item = _strict_object(payload, "usage decision", _FIELDS)
    return _from_payload(item, includes_id=True)


def parse_usage_decision_artifact(
    payload: Mapping[str, object],
) -> UsageDecision:
    item = _strict_object(payload, "usage decision artifact", _ARTIFACT_FIELDS)
    return _from_payload(item, includes_id=False)


def loads_usage_decision(source: str | bytes) -> UsageDecision:
    return _loads(source, includes_id=True)


def loads_usage_decision_artifact(source: str | bytes) -> UsageDecision:
    return _loads(source, includes_id=False)


def dumps_usage_decision(decision: UsageDecision) -> str:
    if type(decision) is not UsageDecision:
        _invalid("decision must be exactly UsageDecision")
    return json.dumps(
        decision.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _loads(source: str | bytes, *, includes_id: bool) -> UsageDecision:
    try:
        if type(source) is bytes:
            text = decode_bounded_utf8(
                source,
                max_bytes=USAGE_DECISION_JSON_MAX_BYTES,
                description="usage decision",
            )
        elif type(source) is str:
            encoded = source.encode("utf-8")
            if len(encoded) > USAGE_DECISION_JSON_MAX_BYTES:
                _invalid("usage decision exceeds its JSON byte limit")
            text = source
        else:
            _invalid("usage decision source must be text or bytes")
        payload = parse_bounded_json(
            text,
            description="usage decision",
            max_nodes=USAGE_DECISION_JSON_MAX_NODES,
            max_depth=USAGE_DECISION_JSON_MAX_DEPTH,
        )
        if not isinstance(payload, Mapping):
            _invalid("usage decision must be a JSON object")
        return (
            parse_usage_decision(cast(Mapping[str, object], payload))
            if includes_id
            else parse_usage_decision_artifact(cast(Mapping[str, object], payload))
        )
    except UsageDecisionV3Error:
        raise
    except Exception:
        _invalid("usage decision JSON is invalid")


def _from_payload(
    item: Mapping[str, object],
    *,
    includes_id: bool,
) -> UsageDecision:
    unsigned = dict(item)
    supplied_id = unsigned.pop("usage_decision_id", None)
    computed_id = usage_decision_id(unsigned)
    if includes_id and supplied_id != computed_id:
        raise UsageDecisionV3Error(
            "TBM_USAGE_DECISION_HASH_MISMATCH",
            "usage_decision_id does not match canonical decision content",
        )
    return UsageDecision(
        usage_decision_id=computed_id,
        session_id=_string(item, "session_id"),
        decision_id=_string(item, "decision_id"),
        trace_id=_string(item, "trace_id"),
        run_id=_string(item, "run_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        retrieval_snapshot_id=_string(item, "retrieval_snapshot_id"),
        system_gate_evaluation_id=_string(
            item,
            "system_gate_evaluation_id",
        ),
        semantic_gate_attempt_id=_string(item, "semantic_gate_attempt_id"),
        candidate_memory_revision_ids=_strings(
            item,
            "candidate_memory_revision_ids",
        ),
        system_allowed_memory_revision_ids=_strings(
            item,
            "system_allowed_memory_revision_ids",
        ),
        semantic_allowed_memory_revision_ids=_strings(
            item,
            "semantic_allowed_memory_revision_ids",
        ),
        final_memory_revision_ids=_strings(
            item,
            "final_memory_revision_ids",
        ),
        blocked_memory_revision_ids=_strings(
            item,
            "blocked_memory_revision_ids",
        ),
        system_blocked=_parse_system_blocks(item["system_blocked"]),
        reason=_string(item, "reason"),
        risk=_string(item, "risk"),
        recommended_injection=_string(item, "recommended_injection"),
        renderer_id=_string(item, "renderer_id"),
        renderer_version=_string(item, "renderer_version"),
        policy_bundle_sha256=_string(item, "policy_bundle_sha256"),
        injection_artifact_id=_string(item, "injection_artifact_id"),
        replay_components=_parse_components(item["replay_components"]),
        created_at=_string(item, "created_at"),
        contract_version=_string(item, "contract_version"),
    )


def _strict_object(
    value: object,
    name: str,
    fields: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        _invalid(f"{name} must be an object")
    if frozenset(value) != fields:
        _invalid(f"{name} fields are invalid")
    return cast(Mapping[str, object], value)


def _string(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        _invalid(f"{name} must be a string")
    return cast(str, value)


def _strings(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = payload[name]
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid(f"{name} must be an array of strings")
    return tuple(cast(list[str], value))


def _parse_system_blocks(
    value: object,
) -> tuple[tuple[str, str, str], ...]:
    if type(value) is not list:
        _invalid("system_blocked must be an array")
    result: list[tuple[str, str, str]] = []
    for raw in cast(list[object], value):
        item = _strict_object(
            raw,
            "system_blocked entry",
            frozenset({"memory_revision_id", "reason_code", "rule_id"}),
        )
        result.append(
            (
                _string(item, "memory_revision_id"),
                _string(item, "reason_code"),
                _string(item, "rule_id"),
            )
        )
    return tuple(result)


def _parse_components(
    value: object,
) -> tuple[tuple[ReplayComponentName, str], ...]:
    if not isinstance(value, Mapping) or any(
        type(key) is not str or type(item) is not str for key, item in value.items()
    ):
        _invalid("replay_components must be an object of digests")
    if frozenset(value) != frozenset(REPLAY_COMPONENT_NAMES):
        _invalid("replay_components must contain the fixed complete set")
    return cast(
        tuple[tuple[ReplayComponentName, str], ...],
        tuple(
            (name, cast(str, value[name]))
            for name in REPLAY_COMPONENT_NAMES
            if name in value
        ),
    )


def _revision_ids(value: object, name: str) -> None:
    if (
        type(value) is not tuple
        or len(value) > GATE_SESSION_MAX_MEMORY_REVISIONS
        or len(set(value)) != len(value)
        or any(
            type(item) is not str or not _REVISION_ID_RE.fullmatch(item)
            for item in value
        )
    ):
        _invalid(f"{name} must be a unique bounded tuple of revision IDs")


def _ordered_subset(
    values: tuple[str, ...],
    parent: tuple[str, ...],
    name: str,
) -> None:
    parent_set = frozenset(parent)
    if any(item not in parent_set for item in values):
        _invalid(f"{name} must be a subset of its parent set")
    expected = tuple(item for item in parent if item in frozenset(values))
    if values != expected:
        _invalid(f"{name} must preserve candidate order")


def _system_blocks(
    value: object,
    candidates: tuple[str, ...],
    system_allowed: tuple[str, ...],
) -> None:
    if type(value) is not tuple or any(
        type(item) is not tuple or len(item) != 3 for item in value
    ):
        _invalid("system_blocked must be a tuple of block records")
    blocked_ids: list[str] = []
    for revision_id, reason_code, rule_id in cast(
        tuple[tuple[object, object, object], ...],
        value,
    ):
        if type(revision_id) is not str or not _REVISION_ID_RE.fullmatch(revision_id):
            _invalid("system_blocked revision ID is invalid")
        _identifier(reason_code, "system_blocked reason_code")
        _identifier(rule_id, "system_blocked rule_id")
        blocked_ids.append(revision_id)
    expected = tuple(
        item for item in candidates if item not in frozenset(system_allowed)
    )
    if tuple(blocked_ids) != expected:
        _invalid("system_blocked must exactly cover System Gate blocks")


def _components(
    value: object,
    injection_artifact_id: str,
) -> None:
    if type(value) is not tuple or any(
        type(item) is not tuple or len(item) != 2
        for item in cast(tuple[object, ...], value)
    ):
        _invalid("replay_components must contain the fixed complete set")
    entries = cast(tuple[tuple[str, object], ...], value)
    if tuple(name for name, _ in entries) != REPLAY_COMPONENT_NAMES:
        _invalid("replay_components must contain the fixed complete set")
    for _, digest in entries:
        _digest(digest, "replay component")
    component_map = dict(cast(tuple[tuple[str, str], ...], entries))
    if (
        artifact_id_from_sha256(component_map["injection_artifact"])
        != injection_artifact_id
    ):
        _invalid("injection replay component does not match artifact ID")


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(
            f"{name} must be a bounded identifier without surrounding "
            "whitespace or control characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeError:
        _invalid(f"{name} must contain valid Unicode")


def _text(value: object, name: str, maximum: int) -> None:
    if type(value) is not str or len(value) > maximum:
        _invalid(f"{name} must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _invalid(f"{name} must contain valid Unicode")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{name} must be an algorithm-tagged SHA-256 digest")


def _canonical_timestamp(value: object) -> str:
    try:
        result = canonical_rfc3339(value)
        parse_rfc3339(result)
        return result
    except ValueError:
        _invalid("created_at must be a timezone-aware RFC 3339 timestamp")


def _invalid(message: str) -> NoReturn:
    raise UsageDecisionV3Error(
        "TBM_USAGE_DECISION_INVALID",
        message,
    )


__all__ = [
    "USAGE_DECISION_ARTIFACT_MEDIA_TYPE",
    "USAGE_DECISION_CONTRACT_VERSION",
    "USAGE_DECISION_JSON_MAX_BYTES",
    "USAGE_DECISION_JSON_MAX_DEPTH",
    "USAGE_DECISION_JSON_MAX_NODES",
    "UsageDecision",
    "UsageDecisionV3Error",
    "build_usage_decision",
    "create_usage_decision_artifact",
    "dumps_usage_decision",
    "loads_usage_decision",
    "loads_usage_decision_artifact",
    "parse_usage_decision",
    "parse_usage_decision_artifact",
    "usage_decision_artifact_id",
    "usage_decision_artifact_payload",
    "usage_decision_id",
]
