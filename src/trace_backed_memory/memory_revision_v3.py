from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Literal, Mapping, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import AuthorizationScope, canonical_sha256
from .evidence_v3 import StructuredRegressionEvidence
from .replay_v3 import ContentAddressedArtifact


MEMORY_REVISION_CONTRACT_VERSION = "tbm.memory-revision.v3"
MEMORY_REVISION_JSON_MAX_BYTES = 1024 * 1024
MEMORY_REVISION_JSON_MAX_DEPTH = 32
MEMORY_REVISION_JSON_MAX_NODES = 10_000
MEMORY_REVISION_MAX_EVIDENCE_IDS = 100

_IDENTIFIER_MAX_CHARS = 128
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_EVIDENCE_ID_RE = re.compile(r"^regression_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])$"
)
_KINDS = frozenset({"lesson", "project_policy"})
_MEMORY_TYPES = frozenset({"procedural", "semantic", "episodic", "policy"})
_FIELDS = frozenset(
    {
        "contract_version",
        "revision_id",
        "memory_id",
        "memory_kind",
        "revision_number",
        "previous_revision_id",
        "memory_type",
        "content_artifact",
        "scope",
        "confidence",
        "sensitive",
        "eval_leaking",
        "source_case_id",
        "source_case_revision_id",
        "fix_evidence_id",
        "regression_evidence_ids",
        "proposed_by",
        "proposed_via_client_id",
        "proposed_at",
        "proposal_attestation_sha256",
    }
)


class MemoryRevisionContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise MemoryRevisionContractError("TBM_MEMORY_REVISION_INVALID", message)


@dataclass(frozen=True)
class MemoryRevision:
    revision_id: str
    memory_id: str
    memory_kind: Literal["lesson", "project_policy"]
    revision_number: int
    previous_revision_id: str | None
    memory_type: Literal["procedural", "semantic", "episodic", "policy"]
    content_artifact: ContentAddressedArtifact
    scope: AuthorizationScope
    confidence: float
    sensitive: bool
    eval_leaking: bool
    source_case_id: str | None
    source_case_revision_id: str | None
    fix_evidence_id: str | None
    regression_evidence_ids: tuple[str, ...]
    proposed_by: str
    proposed_via_client_id: str
    proposed_at: str
    proposal_attestation_sha256: str
    contract_version: str = MEMORY_REVISION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.proposed_at, "proposed_at")
        object.__setattr__(
            self,
            "proposed_at",
            canonical_rfc3339(self.proposed_at),
        )
        if self.contract_version != MEMORY_REVISION_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.revision_id) is not str or not _REVISION_ID_RE.fullmatch(
            self.revision_id
        ):
            _invalid(
                "revision_id must be memory_revision_sha256_<64 lowercase hex>"
            )
        _identifier(self.memory_id, "memory_id")
        if type(self.memory_kind) is not str or self.memory_kind not in _KINDS:
            _invalid("memory_kind must be lesson or project_policy")
        if type(self.revision_number) is not int or self.revision_number < 1:
            _invalid("revision_number must be a positive integer")
        if self.revision_number == 1:
            if self.previous_revision_id is not None:
                _invalid("first revision must not have previous_revision_id")
        else:
            if (
                type(self.previous_revision_id) is not str
                or not _REVISION_ID_RE.fullmatch(self.previous_revision_id)
            ):
                _invalid("previous_revision_id must reference a memory revision")
            if self.previous_revision_id == self.revision_id:
                _invalid("revision must not reference itself as previous")
        if type(self.memory_type) is not str or self.memory_type not in _MEMORY_TYPES:
            _invalid("memory_type is not supported")
        if self.memory_kind == "project_policy" and self.memory_type != "policy":
            _invalid("project_policy revision must use policy memory_type")
        if type(self.content_artifact) is not ContentAddressedArtifact:
            _invalid("content_artifact must be exactly ContentAddressedArtifact")
        if type(self.scope) is not AuthorizationScope:
            _invalid("scope must be exactly AuthorizationScope")
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence),
        )
        if type(self.sensitive) is not bool or type(self.eval_leaking) is not bool:
            _invalid("sensitive and eval_leaking must be booleans")
        if self.sensitive and self.content_artifact.classification not in {
            "confidential",
            "restricted",
        }:
            _invalid("sensitive revision requires sensitive classification")
        if not self.sensitive and self.content_artifact.classification in {
            "confidential",
            "restricted",
        }:
            _invalid("sensitive classification requires sensitive=true")
        _evidence_ids(self.regression_evidence_ids)
        if self.memory_kind == "lesson":
            for name in (
                "source_case_id",
                "source_case_revision_id",
                "fix_evidence_id",
            ):
                _identifier(getattr(self, name), name)
            if not self.regression_evidence_ids:
                _invalid("lesson revision requires regression evidence")
        else:
            if self.regression_evidence_ids:
                _invalid("project_policy revision forbids regression evidence")
            if any(
                value is not None
                for value in (
                    self.source_case_id,
                    self.source_case_revision_id,
                    self.fix_evidence_id,
                )
            ):
                _invalid("project_policy revision forbids case/fix references")
        _identifier(self.proposed_by, "proposed_by")
        _identifier(self.proposed_via_client_id, "proposed_via_client_id")
        _digest(
            self.proposal_attestation_sha256,
            "proposal_attestation_sha256",
        )
        if parse_rfc3339(self.content_artifact.created_at) > parse_rfc3339(
            self.proposed_at
        ):
            _invalid("content artifact must not be created after proposal")
        expected = memory_revision_id(self._unsigned_dict())
        if self.revision_id != expected:
            raise MemoryRevisionContractError(
                "TBM_MEMORY_REVISION_HASH_MISMATCH",
                "revision_id does not match canonical revision content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "memory_id": self.memory_id,
            "memory_kind": self.memory_kind,
            "revision_number": self.revision_number,
            "previous_revision_id": self.previous_revision_id,
            "memory_type": self.memory_type,
            "content_artifact": self.content_artifact.to_dict(),
            "scope": self.scope.to_dict(),
            "confidence": self.confidence,
            "sensitive": self.sensitive,
            "eval_leaking": self.eval_leaking,
            "source_case_id": self.source_case_id,
            "source_case_revision_id": self.source_case_revision_id,
            "fix_evidence_id": self.fix_evidence_id,
            "regression_evidence_ids": list(self.regression_evidence_ids),
            "proposed_by": self.proposed_by,
            "proposed_via_client_id": self.proposed_via_client_id,
            "proposed_at": self.proposed_at,
            "proposal_attestation_sha256": self.proposal_attestation_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        result = self._unsigned_dict()
        result["revision_id"] = self.revision_id
        return result


def memory_revision_id(content: Mapping[str, object]) -> str:
    return "memory_revision_sha256_" + canonical_sha256(content).removeprefix(
        "sha256:"
    )


def build_memory_revision(
    *,
    memory_id: str,
    memory_kind: Literal["lesson", "project_policy"],
    revision_number: int,
    previous_revision_id: str | None,
    memory_type: Literal["procedural", "semantic", "episodic", "policy"],
    content_artifact: ContentAddressedArtifact,
    scope: AuthorizationScope,
    confidence: float,
    sensitive: bool,
    eval_leaking: bool,
    source_case_id: str | None,
    source_case_revision_id: str | None,
    fix_evidence_id: str | None,
    regression_evidence_ids: tuple[str, ...],
    proposed_by: str,
    proposed_via_client_id: str,
    proposed_at: str,
    proposal_attestation_sha256: str,
) -> MemoryRevision:
    _timestamp(proposed_at, "proposed_at")
    if type(content_artifact) is not ContentAddressedArtifact:
        _invalid("content_artifact must be exactly ContentAddressedArtifact")
    if type(scope) is not AuthorizationScope:
        _invalid("scope must be exactly AuthorizationScope")
    if type(regression_evidence_ids) is not tuple:
        _invalid("regression_evidence_ids must be a tuple")
    if any(type(item) is not str for item in regression_evidence_ids):
        _invalid("regression_evidence_ids must contain strings")
    normalized_evidence_ids = tuple(sorted(regression_evidence_ids))
    _evidence_ids(normalized_evidence_ids)
    normalized_confidence = _confidence(confidence)
    canonical_time = canonical_rfc3339(proposed_at)
    canonical_artifact = _canonical_artifact(content_artifact)
    canonical_scope = _canonical_scope(scope)
    values = {
        "contract_version": MEMORY_REVISION_CONTRACT_VERSION,
        "memory_id": memory_id,
        "memory_kind": memory_kind,
        "revision_number": revision_number,
        "previous_revision_id": previous_revision_id,
        "memory_type": memory_type,
        "content_artifact": canonical_artifact.to_dict(),
        "scope": canonical_scope.to_dict(),
        "confidence": normalized_confidence,
        "sensitive": sensitive,
        "eval_leaking": eval_leaking,
        "source_case_id": source_case_id,
        "source_case_revision_id": source_case_revision_id,
        "fix_evidence_id": fix_evidence_id,
        "regression_evidence_ids": list(normalized_evidence_ids),
        "proposed_by": proposed_by,
        "proposed_via_client_id": proposed_via_client_id,
        "proposed_at": canonical_time,
        "proposal_attestation_sha256": proposal_attestation_sha256,
    }
    try:
        revision_id = memory_revision_id(values)
    except ValueError as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            "revision content is not canonical JSON",
        ) from error
    return MemoryRevision(
        revision_id=revision_id,
        memory_id=memory_id,
        memory_kind=memory_kind,
        revision_number=revision_number,
        previous_revision_id=previous_revision_id,
        memory_type=memory_type,
        content_artifact=canonical_artifact,
        scope=canonical_scope,
        confidence=normalized_confidence,
        sensitive=sensitive,
        eval_leaking=eval_leaking,
        source_case_id=source_case_id,
        source_case_revision_id=source_case_revision_id,
        fix_evidence_id=fix_evidence_id,
        regression_evidence_ids=normalized_evidence_ids,
        proposed_by=proposed_by,
        proposed_via_client_id=proposed_via_client_id,
        proposed_at=canonical_time,
        proposal_attestation_sha256=proposal_attestation_sha256,
    )


def verify_memory_revision_evidence(
    revision: MemoryRevision,
    evidence_by_id: Mapping[str, StructuredRegressionEvidence],
) -> None:
    if type(revision) is not MemoryRevision:
        _invalid("revision must be exactly MemoryRevision")
    if revision.memory_kind != "lesson":
        if revision.regression_evidence_ids:
            _invalid("project_policy evidence requires a separate policy review")
        return
    for evidence_id in revision.regression_evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if type(evidence) is not StructuredRegressionEvidence:
            _invalid(f"missing structured evidence: {evidence_id}")
        if evidence.evidence_id != evidence_id:
            _invalid("structured evidence identity does not match reference")
        if evidence.result != "pass":
            _invalid("lesson revision requires passing structured evidence")
        if evidence.case_id != revision.source_case_id:
            _invalid("structured evidence does not match source_case_id")
        if revision.proposed_by in {
            evidence.submitter_id,
            evidence.verifier_id,
        }:
            _invalid("revision proposer must be independent of evidence actors")


def dumps_memory_revision(revision: MemoryRevision) -> str:
    if type(revision) is not MemoryRevision:
        _invalid("revision must be exactly MemoryRevision")
    return json.dumps(
        revision.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def loads_memory_revision(document: str | bytes) -> MemoryRevision:
    if type(document) is bytes:
        try:
            source = decode_bounded_utf8(
                document,
                max_bytes=MEMORY_REVISION_JSON_MAX_BYTES,
                description="memory revision JSON",
            )
        except (UnicodeError, ValueError) as error:
            raise MemoryRevisionContractError(
                "TBM_MEMORY_REVISION_INVALID_JSON",
                "memory revision must be bounded strict JSON",
            ) from error
    else:
        source = document
    try:
        if type(source) is not str:
            raise ValueError("memory revision source must be str or bytes")
        if len(source.encode("utf-8")) > MEMORY_REVISION_JSON_MAX_BYTES:
            raise ValueError("memory revision exceeds byte limit")
        payload = parse_bounded_json(
            source,
            description="memory revision",
            max_nodes=MEMORY_REVISION_JSON_MAX_NODES,
            max_depth=MEMORY_REVISION_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID_JSON",
            "memory revision must be bounded strict JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("memory revision must be an object")
    return parse_memory_revision(cast(dict[str, object], payload))


def parse_memory_revision(payload: Mapping[str, object]) -> MemoryRevision:
    item = _strict_object(payload, "memory revision", _FIELDS)
    return MemoryRevision(
        revision_id=cast(str, item["revision_id"]),
        memory_id=cast(str, item["memory_id"]),
        memory_kind=cast(Literal["lesson", "project_policy"], item["memory_kind"]),
        revision_number=cast(int, item["revision_number"]),
        previous_revision_id=cast(str | None, item["previous_revision_id"]),
        memory_type=cast(
            Literal["procedural", "semantic", "episodic", "policy"],
            item["memory_type"],
        ),
        content_artifact=_parse_artifact(item["content_artifact"]),
        scope=_parse_scope(item["scope"]),
        confidence=cast(float, item["confidence"]),
        sensitive=cast(bool, item["sensitive"]),
        eval_leaking=cast(bool, item["eval_leaking"]),
        source_case_id=cast(str | None, item["source_case_id"]),
        source_case_revision_id=cast(
            str | None,
            item["source_case_revision_id"],
        ),
        fix_evidence_id=cast(str | None, item["fix_evidence_id"]),
        regression_evidence_ids=_parse_evidence_ids(
            item["regression_evidence_ids"]
        ),
        proposed_by=cast(str, item["proposed_by"]),
        proposed_via_client_id=cast(str, item["proposed_via_client_id"]),
        proposed_at=cast(str, item["proposed_at"]),
        proposal_attestation_sha256=cast(
            str,
            item["proposal_attestation_sha256"],
        ),
        contract_version=cast(str, item["contract_version"]),
    )


def _parse_artifact(value: object) -> ContentAddressedArtifact:
    fields = frozenset(
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
    item = _strict_object(value, "content_artifact", fields)
    try:
        artifact = ContentAddressedArtifact(
            artifact_id=cast(str, item["artifact_id"]),
            content_sha256=cast(str, item["content_sha256"]),
            size_bytes=cast(int, item["size_bytes"]),
            media_type=cast(str, item["media_type"]),
            classification=cast(
                Literal["public", "internal", "confidential", "restricted"],
                item["classification"],
            ),
            created_at=cast(str, item["created_at"]),
            encryption_key_id=cast(str | None, item["encryption_key_id"]),
            redaction_policy_id=cast(str | None, item["redaction_policy_id"]),
        )
    except ValueError as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            "content_artifact failed validation",
        ) from error
    return _canonical_artifact(artifact)


def _parse_scope(value: object) -> AuthorizationScope:
    fields = frozenset(
        {"kind", "tenant_id", "repository_id", "attributes"}
    )
    item = _strict_object(value, "scope", fields)
    attributes = item["attributes"]
    if type(attributes) is not dict or any(
        type(key) is not str or type(entry) is not str
        for key, entry in cast(dict[object, object], attributes).items()
    ):
        _invalid("scope attributes must be a string mapping")
    try:
        return AuthorizationScope(
            kind=cast(Literal["global", "tenant", "repository"], item["kind"]),
            tenant_id=cast(str | None, item["tenant_id"]),
            repository_id=cast(str | None, item["repository_id"]),
            attributes=tuple(
                sorted(cast(dict[str, str], attributes).items())
            ),
        )
    except ValueError as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            "scope failed validation",
        ) from error


def _canonical_artifact(
    artifact: ContentAddressedArtifact,
) -> ContentAddressedArtifact:
    _timestamp(artifact.created_at, "content_artifact.created_at")
    try:
        return ContentAddressedArtifact(
            artifact_id=artifact.artifact_id,
            content_sha256=artifact.content_sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
            classification=artifact.classification,
            created_at=canonical_rfc3339(artifact.created_at),
            encryption_key_id=artifact.encryption_key_id,
            redaction_policy_id=artifact.redaction_policy_id,
        )
    except ValueError as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            "content_artifact failed validation",
        ) from error


def _canonical_scope(scope: AuthorizationScope) -> AuthorizationScope:
    try:
        return AuthorizationScope(
            kind=scope.kind,
            tenant_id=scope.tenant_id,
            repository_id=scope.repository_id,
            attributes=tuple(sorted(scope.attributes)),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            "scope failed validation",
        ) from error


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
    actual = frozenset(cast(dict[str, object], item))
    if actual != fields:
        _invalid(f"{label} fields do not match the contract")
    return cast(dict[str, object], item)


def _parse_evidence_ids(value: object) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid("regression_evidence_ids must be an array of strings")
    result = tuple(sorted(cast(list[str], value)))
    _evidence_ids(result)
    return result


def _evidence_ids(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > MEMORY_REVISION_MAX_EVIDENCE_IDS
    ):
        _invalid("regression_evidence_ids must be a bounded tuple")
    identifiers = cast(tuple[object, ...], value)
    if any(
        type(item) is not str or not _EVIDENCE_ID_RE.fullmatch(item)
        for item in identifiers
    ):
        _invalid("regression_evidence_ids contain an invalid evidence ID")
    if len(set(identifiers)) != len(identifiers):
        _invalid("regression_evidence_ids must not contain duplicates")
    if tuple(sorted(cast(tuple[str, ...], identifiers))) != identifiers:
        _invalid("regression_evidence_ids must be sorted")


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
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            f"{label} must be valid UTF-8",
        ) from error


def _digest(value: object, label: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{label} must be sha256:<64 lowercase hex>")


def _confidence(value: object) -> float:
    if type(value) not in {int, float}:
        _invalid("confidence must be a finite number from 0.0 to 1.0")
    try:
        normalized = float(cast(int | float, value))
    except (OverflowError, ValueError) as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            "confidence must be a finite number from 0.0 to 1.0",
        ) from error
    if not math.isfinite(normalized) or normalized < 0.0 or normalized > 1.0:
        _invalid("confidence must be a finite number from 0.0 to 1.0")
    return normalized


def _timestamp(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not _TIMESTAMP_RE.fullmatch(cast(str, value))
    ):
        _invalid(f"{label} must be an RFC 3339 timestamp")
    try:
        parse_rfc3339(cast(str, value))
    except ValueError as error:
        raise MemoryRevisionContractError(
            "TBM_MEMORY_REVISION_INVALID",
            f"{label} must be an RFC 3339 timestamp",
        ) from error
