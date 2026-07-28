from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hmac
import json
import re
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from .contracts_v3 import V3ContractError
from .gate_evaluation_v3 import SemanticGateAttempt
from .policy import (
    LLM_GATE_PROMPT_MAX_CHARS,
    LLM_GATE_RESPONSE_MAX_BYTES,
    MEMORY_DECISION_REASON_MAX_CHARS,
)
from .replay_v3 import (
    ContentAddressedArtifact,
    DataClassification,
    create_content_addressed_artifact,
    verify_artifact_content,
)


SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION = "tbm.semantic-gate-artifact.v3"
SEMANTIC_GATE_ARTIFACT_JSON_MAX_BYTES = 1024 * 1024
SEMANTIC_GATE_ARTIFACT_JSON_MAX_DEPTH = 16
SEMANTIC_GATE_ARTIFACT_JSON_MAX_NODES = 256
SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES = LLM_GATE_PROMPT_MAX_CHARS * 4
SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES = LLM_GATE_RESPONSE_MAX_BYTES
SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE = "text/plain; charset=utf-8"

SemanticGateArtifactRole = Literal["prompt", "response"]

_ATTEMPT_ID_RE = re.compile(r"semantic_attempt_sha256_[0-9a-f]{64}")
_BINDING_FIELDS = frozenset(
    {
        "contract_version",
        "artifact_kind",
        "artifact_role",
        "attempt_id",
        "artifact",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "content_sha256",
        "size_bytes",
        "media_type",
        "classification",
        "created_at",
        "encryption_key_id",
        "redaction_policy_id",
    }
)


class SemanticGateArtifactContractError(V3ContractError):
    """Stable failure for malformed Semantic Gate artifact bindings."""


@dataclass(frozen=True)
class SemanticGateArtifactBinding:
    """One immutable role binding for exact Semantic Gate provider bytes."""

    artifact_role: SemanticGateArtifactRole
    attempt_id: str
    artifact: ContentAddressedArtifact
    artifact_kind: Literal["semantic_gate"] = "semantic_gate"
    contract_version: str = SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != SEMANTIC_GATE_ARTIFACT_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if self.artifact_kind != "semantic_gate":
            _invalid("artifact_kind must be semantic_gate")
        if self.artifact_role not in {"prompt", "response"}:
            _invalid("artifact_role must be prompt or response")
        if (
            type(self.attempt_id) is not str
            or not _ATTEMPT_ID_RE.fullmatch(self.attempt_id)
        ):
            _invalid(
                "attempt_id must be semantic_attempt_sha256_<64 lowercase hex>"
            )
        if type(self.artifact) is not ContentAddressedArtifact:
            _invalid("artifact must be exactly ContentAddressedArtifact")
        if self.artifact.size_bytes == 0:
            _invalid("Semantic Gate artifact content must not be empty")
        if (
            self.artifact_role == "prompt"
            and self.artifact.media_type
            != SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE
        ):
            _invalid(
                "prompt artifact media_type must be "
                f"{SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE}"
            )
        maximum = _maximum_bytes(self.artifact_role)
        if self.artifact.size_bytes > maximum:
            _invalid(
                f"{self.artifact_role} artifact exceeds maximum size of "
                f"{maximum} bytes"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "artifact_kind": self.artifact_kind,
            "artifact_role": self.artifact_role,
            "attempt_id": self.attempt_id,
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True)
class StoredSemanticGateArtifact:
    """Validated binding paired with its exact stored bytes."""

    binding: SemanticGateArtifactBinding
    content: bytes

    def __post_init__(self) -> None:
        if type(self.binding) is not SemanticGateArtifactBinding:
            _invalid(
                "binding must be exactly SemanticGateArtifactBinding"
            )
        if type(self.content) is not bytes:
            _invalid("content must be bytes")
        if not _role_content_is_valid(
            self.binding.artifact_role,
            self.content,
        ):
            _invalid("content is invalid for the bound artifact role")
        if not verify_artifact_content(self.binding.artifact, self.content):
            _invalid("content does not match the bound artifact descriptor")


@dataclass(frozen=True)
class StoredSemanticGateAttemptArtifacts:
    """One attempt paired with its exact prompt and optional response bytes."""

    attempt: SemanticGateAttempt
    prompt: StoredSemanticGateArtifact
    response: StoredSemanticGateArtifact | None

    def __post_init__(self) -> None:
        if type(self.attempt) is not SemanticGateAttempt:
            _invalid("attempt must be exactly SemanticGateAttempt")
        if type(self.prompt) is not StoredSemanticGateArtifact:
            _invalid("prompt must be exactly StoredSemanticGateArtifact")
        if (
            self.response is not None
            and type(self.response) is not StoredSemanticGateArtifact
        ):
            _invalid(
                "response must be exactly StoredSemanticGateArtifact or null"
            )
        if not verify_semantic_gate_artifact_binding(
            self.prompt.binding,
            self.attempt,
            self.prompt.content,
        ):
            _invalid("prompt does not match Semantic Gate attempt")
        if self.attempt.status == "succeeded":
            if self.response is None or not verify_semantic_gate_artifact_binding(
                self.response.binding,
                self.attempt,
                self.response.content,
            ):
                _invalid(
                    "succeeded attempt requires its exact response artifact"
                )
        elif self.response is not None:
            _invalid("failed attempt forbids a response artifact")


def create_semantic_gate_artifact_binding(
    attempt: SemanticGateAttempt,
    content: bytes,
    *,
    artifact_role: SemanticGateArtifactRole,
    media_type: str,
    classification: DataClassification,
    created_at: str,
    encryption_key_id: str | None = None,
    redaction_policy_id: str | None = None,
) -> SemanticGateArtifactBinding:
    """Bind exact prompt or response bytes to one Semantic Gate attempt."""

    expected_sha256 = _expected_sha256(attempt, artifact_role)
    if not _role_content_is_valid(artifact_role, content):
        _invalid(f"{artifact_role} content is invalid or exceeds its limit")
    artifact = create_content_addressed_artifact(
        content,
        media_type=media_type,
        classification=classification,
        created_at=created_at,
        encryption_key_id=encryption_key_id,
        redaction_policy_id=redaction_policy_id,
    )
    binding = SemanticGateArtifactBinding(
        artifact_role=artifact_role,
        attempt_id=attempt.attempt_id,
        artifact=artifact,
    )
    if not hmac.compare_digest(
        artifact.content_sha256,
        expected_sha256,
    ):
        _invalid(
            f"{artifact_role} content digest does not match "
            "Semantic Gate attempt"
        )
    return binding


def verify_semantic_gate_artifact_binding(
    binding: SemanticGateArtifactBinding,
    attempt: SemanticGateAttempt,
    content: bytes,
) -> bool:
    """Verify role, attempt linkage, digest, and exact bytes together."""

    if type(binding) is not SemanticGateArtifactBinding:
        _invalid("binding must be exactly SemanticGateArtifactBinding")
    expected_sha256 = _expected_sha256(attempt, binding.artifact_role)
    if not _role_content_is_valid(binding.artifact_role, content):
        return False
    if not hmac.compare_digest(binding.attempt_id, attempt.attempt_id):
        return False
    if not hmac.compare_digest(
        binding.artifact.content_sha256,
        expected_sha256,
    ):
        return False
    return verify_artifact_content(binding.artifact, content)


def parse_semantic_gate_artifact_binding(
    payload: Mapping[str, object],
) -> SemanticGateArtifactBinding:
    """Parse one strict external Semantic Gate artifact descriptor."""

    data = _strict_object(
        payload,
        _BINDING_FIELDS,
        "Semantic Gate artifact binding",
    )
    artifact_data = _strict_object(
        data["artifact"],
        _ARTIFACT_FIELDS,
        "content-addressed artifact",
    )
    artifact = ContentAddressedArtifact(
        artifact_id=_string(artifact_data, "artifact_id"),
        content_sha256=_string(artifact_data, "content_sha256"),
        size_bytes=_integer(artifact_data, "size_bytes"),
        media_type=_string(artifact_data, "media_type"),
        classification=cast(
            DataClassification,
            _string(artifact_data, "classification"),
        ),
        created_at=_string(artifact_data, "created_at"),
        encryption_key_id=_optional_string(
            artifact_data,
            "encryption_key_id",
        ),
        redaction_policy_id=_optional_string(
            artifact_data,
            "redaction_policy_id",
        ),
    )
    return SemanticGateArtifactBinding(
        contract_version=_string(data, "contract_version"),
        artifact_kind=cast(
            Literal["semantic_gate"],
            _string(data, "artifact_kind"),
        ),
        artifact_role=cast(
            SemanticGateArtifactRole,
            _string(data, "artifact_role"),
        ),
        attempt_id=_string(data, "attempt_id"),
        artifact=artifact,
    )


def loads_semantic_gate_artifact_binding(
    source: str | bytes,
) -> SemanticGateArtifactBinding:
    """Decode bounded strict JSON into one artifact binding."""

    if type(source) is bytes:
        try:
            source_text = decode_bounded_utf8(
                source,
                max_bytes=SEMANTIC_GATE_ARTIFACT_JSON_MAX_BYTES,
                description="Semantic Gate artifact binding JSON",
            )
        except (UnicodeError, ValueError) as error:
            raise SemanticGateArtifactContractError(
                "TBM_SEMANTIC_GATE_ARTIFACT_INVALID_JSON",
                "Semantic Gate artifact binding must be bounded strict JSON",
            ) from error
    else:
        source_text = source
    try:
        if type(source_text) is not str:
            raise ValueError("source must be str or bytes")
        if (
            len(source_text.encode("utf-8"))
            > SEMANTIC_GATE_ARTIFACT_JSON_MAX_BYTES
        ):
            raise ValueError("Semantic Gate artifact binding JSON is too large")
        payload = parse_bounded_json(
            source_text,
            description="Semantic Gate artifact binding",
            max_nodes=SEMANTIC_GATE_ARTIFACT_JSON_MAX_NODES,
            max_depth=SEMANTIC_GATE_ARTIFACT_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise SemanticGateArtifactContractError(
            "TBM_SEMANTIC_GATE_ARTIFACT_INVALID_JSON",
            str(error)[:MEMORY_DECISION_REASON_MAX_CHARS],
        ) from error
    if type(payload) is not dict:
        _invalid("Semantic Gate artifact binding JSON must contain one object")
    return parse_semantic_gate_artifact_binding(
        cast(dict[str, object], payload)
    )


def dumps_semantic_gate_artifact_binding(
    binding: SemanticGateArtifactBinding,
) -> str:
    """Serialize one artifact binding as canonical JSON without its bytes."""

    if type(binding) is not SemanticGateArtifactBinding:
        _invalid("binding must be exactly SemanticGateArtifactBinding")
    return json.dumps(
        binding.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _expected_sha256(
    attempt: SemanticGateAttempt,
    artifact_role: object,
) -> str:
    if type(attempt) is not SemanticGateAttempt:
        _invalid("attempt must be exactly SemanticGateAttempt")
    if artifact_role == "prompt":
        return attempt.prompt_artifact_sha256
    if artifact_role == "response":
        if attempt.status != "succeeded":
            _invalid("failed Semantic Gate attempt forbids response artifact")
        if attempt.response_artifact_sha256 is None:
            _invalid("succeeded Semantic Gate attempt requires response digest")
        return cast(str, attempt.response_artifact_sha256)
    _invalid("artifact_role must be prompt or response")


def _maximum_bytes(artifact_role: SemanticGateArtifactRole) -> int:
    if artifact_role == "prompt":
        return SEMANTIC_GATE_PROMPT_ARTIFACT_MAX_BYTES
    return SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES


def _role_content_is_valid(
    artifact_role: SemanticGateArtifactRole,
    content: object,
) -> bool:
    if type(content) is not bytes or not content:
        return False
    if artifact_role == "response":
        return len(content) <= SEMANTIC_GATE_RESPONSE_ARTIFACT_MAX_BYTES
    try:
        prompt = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return len(prompt) <= LLM_GATE_PROMPT_MAX_CHARS


def _strict_object(
    payload: object,
    expected_fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(payload) is not dict:
        _invalid(f"{label} must be a JSON object")
    data = cast(dict[str, object], payload)
    if any(type(key) is not str for key in data):
        _invalid(f"{label} object keys must be strings")
    fields = frozenset(data)
    unknown = sorted(fields - expected_fields)
    missing = sorted(expected_fields - fields)
    if unknown:
        _invalid(f"{label} has unknown field: {unknown[0]}")
    if missing:
        _invalid(f"{label} is missing field: {missing[0]}")
    return data


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


def _invalid(message: str) -> NoReturn:
    raise SemanticGateArtifactContractError(
        "TBM_SEMANTIC_GATE_ARTIFACT_INVALID",
        message[:MEMORY_DECISION_REASON_MAX_CHARS],
    )
