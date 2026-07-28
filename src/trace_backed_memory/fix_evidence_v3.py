from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Mapping, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import CommitRelationEvidence, V3ContractError, canonical_sha256


FIX_EVIDENCE_CONTRACT_VERSION = "tbm.fix-evidence.v3"
FIX_EVIDENCE_JSON_MAX_BYTES = 1024 * 1024
FIX_EVIDENCE_JSON_MAX_DEPTH = 32
FIX_EVIDENCE_JSON_MAX_NODES = 10_000
FIX_EVIDENCE_MAX_ARTIFACT_HASHES = 1_000

_IDENTIFIER_MAX_CHARS = 128
_METADATA_MAX_CHARS = 2_000
_FIX_EVIDENCE_ID_RE = re.compile(r"^fix_evidence_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])$"
)
_FIELDS = frozenset(
    {
        "contract_version",
        "evidence_id",
        "case_id",
        "source_trace_id",
        "source_commit_sha",
        "fix_commit_sha",
        "source_to_fix",
        "artifact_hashes",
        "submitter_id",
        "submitted_at",
        "reviewer_id",
        "reviewed_at",
        "attestation_sha256",
    }
)
_RELATION_FIELDS = frozenset(
    {
        "from_commit_sha",
        "to_commit_sha",
        "relation",
        "verified_by",
        "verified_at",
    }
)


class FixEvidenceV3ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise FixEvidenceV3ContractError("TBM_FIX_EVIDENCE_INVALID", message)


@dataclass(frozen=True)
class FixEvidence:
    evidence_id: str
    case_id: str
    source_trace_id: str
    source_commit_sha: str
    fix_commit_sha: str
    source_to_fix: CommitRelationEvidence
    artifact_hashes: tuple[str, ...]
    submitter_id: str
    submitted_at: str
    reviewer_id: str
    reviewed_at: str
    attestation_sha256: str
    contract_version: str = FIX_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != FIX_EVIDENCE_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.evidence_id) is not str or not _FIX_EVIDENCE_ID_RE.fullmatch(
            self.evidence_id
        ):
            _invalid(
                "evidence_id must be fix_evidence_sha256_<64 lowercase hex>"
            )
        for name in (
            "case_id",
            "source_trace_id",
            "submitter_id",
            "reviewer_id",
        ):
            _identifier(getattr(self, name), name)
        if self.submitter_id == self.reviewer_id:
            _invalid("submitter_id and reviewer_id must differ")
        _metadata(self.source_commit_sha, "source_commit_sha")
        _metadata(self.fix_commit_sha, "fix_commit_sha")
        if self.source_commit_sha == self.fix_commit_sha:
            _invalid("fix_commit_sha must differ from source_commit_sha")
        if type(self.source_to_fix) is not CommitRelationEvidence:
            _invalid("source_to_fix must be exactly CommitRelationEvidence")
        relation = _canonical_relation(self.source_to_fix)
        object.__setattr__(self, "source_to_fix", relation)
        if (
            relation.from_commit_sha != self.source_commit_sha
            or relation.to_commit_sha != self.fix_commit_sha
        ):
            _invalid("source_to_fix does not bind source and fix commits")
        _artifact_hashes(self.artifact_hashes)
        _timestamp(self.submitted_at, "submitted_at")
        _timestamp(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "submitted_at", canonical_rfc3339(self.submitted_at))
        object.__setattr__(self, "reviewed_at", canonical_rfc3339(self.reviewed_at))
        if parse_rfc3339(relation.verified_at) > parse_rfc3339(self.submitted_at):
            _invalid("source_to_fix must be verified before submission")
        if parse_rfc3339(self.reviewed_at) < parse_rfc3339(self.submitted_at):
            _invalid("reviewed_at must not precede submitted_at")
        _digest(self.attestation_sha256, "attestation_sha256")
        if self.evidence_id != fix_evidence_id(self._unsigned_dict()):
            raise FixEvidenceV3ContractError(
                "TBM_FIX_EVIDENCE_HASH_MISMATCH",
                "evidence_id does not match canonical fix evidence content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "case_id": self.case_id,
            "source_trace_id": self.source_trace_id,
            "source_commit_sha": self.source_commit_sha,
            "fix_commit_sha": self.fix_commit_sha,
            "source_to_fix": self.source_to_fix.to_dict(),
            "artifact_hashes": list(self.artifact_hashes),
            "submitter_id": self.submitter_id,
            "submitted_at": self.submitted_at,
            "reviewer_id": self.reviewer_id,
            "reviewed_at": self.reviewed_at,
            "attestation_sha256": self.attestation_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        result = self._unsigned_dict()
        result["evidence_id"] = self.evidence_id
        return result


def fix_evidence_id(content: Mapping[str, object]) -> str:
    return "fix_evidence_sha256_" + canonical_sha256(content).removeprefix(
        "sha256:"
    )


def build_fix_evidence(
    *,
    case_id: str,
    source_trace_id: str,
    source_commit_sha: str,
    fix_commit_sha: str,
    source_to_fix: CommitRelationEvidence,
    artifact_hashes: tuple[str, ...],
    submitter_id: str,
    submitted_at: str,
    reviewer_id: str,
    reviewed_at: str,
    attestation_sha256: str,
) -> FixEvidence:
    if type(source_to_fix) is not CommitRelationEvidence:
        _invalid("source_to_fix must be exactly CommitRelationEvidence")
    if type(artifact_hashes) is not tuple:
        _invalid("artifact_hashes must be a tuple")
    _timestamp(submitted_at, "submitted_at")
    _timestamp(reviewed_at, "reviewed_at")
    relation = _canonical_relation(source_to_fix)
    artifacts = _parse_artifact_hashes(list(artifact_hashes))
    canonical_submitted_at = canonical_rfc3339(submitted_at)
    canonical_reviewed_at = canonical_rfc3339(reviewed_at)
    values: dict[str, object] = {
        "contract_version": FIX_EVIDENCE_CONTRACT_VERSION,
        "case_id": case_id,
        "source_trace_id": source_trace_id,
        "source_commit_sha": source_commit_sha,
        "fix_commit_sha": fix_commit_sha,
        "source_to_fix": relation.to_dict(),
        "artifact_hashes": list(artifacts),
        "submitter_id": submitter_id,
        "submitted_at": canonical_submitted_at,
        "reviewer_id": reviewer_id,
        "reviewed_at": canonical_reviewed_at,
        "attestation_sha256": attestation_sha256,
    }
    return FixEvidence(
        evidence_id=fix_evidence_id(values),
        case_id=case_id,
        source_trace_id=source_trace_id,
        source_commit_sha=source_commit_sha,
        fix_commit_sha=fix_commit_sha,
        source_to_fix=relation,
        artifact_hashes=artifacts,
        submitter_id=submitter_id,
        submitted_at=canonical_submitted_at,
        reviewer_id=reviewer_id,
        reviewed_at=canonical_reviewed_at,
        attestation_sha256=attestation_sha256,
    )


def dumps_fix_evidence(evidence: FixEvidence) -> str:
    if type(evidence) is not FixEvidence:
        _invalid("evidence must be exactly FixEvidence")
    return json.dumps(
        evidence.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def loads_fix_evidence(document: str | bytes) -> FixEvidence:
    try:
        if type(document) is bytes:
            source = decode_bounded_utf8(
                document,
                max_bytes=FIX_EVIDENCE_JSON_MAX_BYTES,
                description="fix evidence JSON",
            )
        else:
            source = document
        if type(source) is not str:
            raise ValueError("fix evidence source must be str or bytes")
        if len(source) > FIX_EVIDENCE_JSON_MAX_BYTES:
            raise ValueError("fix evidence exceeds character limit")
        if len(source.encode("utf-8")) > FIX_EVIDENCE_JSON_MAX_BYTES:
            raise ValueError("fix evidence exceeds byte limit")
        payload = parse_bounded_json(
            source,
            description="fix evidence",
            max_nodes=FIX_EVIDENCE_JSON_MAX_NODES,
            max_depth=FIX_EVIDENCE_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise FixEvidenceV3ContractError(
            "TBM_FIX_EVIDENCE_INVALID_JSON",
            "fix evidence must be bounded strict JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("fix evidence must be an object")
    return parse_fix_evidence(cast(dict[str, object], payload))


def parse_fix_evidence(payload: Mapping[str, object]) -> FixEvidence:
    item = _strict_object(payload, "fix evidence", _FIELDS)
    return FixEvidence(
        evidence_id=cast(str, item["evidence_id"]),
        case_id=cast(str, item["case_id"]),
        source_trace_id=cast(str, item["source_trace_id"]),
        source_commit_sha=cast(str, item["source_commit_sha"]),
        fix_commit_sha=cast(str, item["fix_commit_sha"]),
        source_to_fix=_parse_relation(item["source_to_fix"]),
        artifact_hashes=_parse_artifact_hashes(item["artifact_hashes"]),
        submitter_id=cast(str, item["submitter_id"]),
        submitted_at=cast(str, item["submitted_at"]),
        reviewer_id=cast(str, item["reviewer_id"]),
        reviewed_at=cast(str, item["reviewed_at"]),
        attestation_sha256=cast(str, item["attestation_sha256"]),
        contract_version=cast(str, item["contract_version"]),
    )


def _parse_relation(value: object) -> CommitRelationEvidence:
    item = _strict_object(value, "source_to_fix", _RELATION_FIELDS)
    try:
        return _canonical_relation(
            CommitRelationEvidence(
                from_commit_sha=cast(str, item["from_commit_sha"]),
                to_commit_sha=cast(str, item["to_commit_sha"]),
                relation=cast(str, item["relation"]),
                verified_by=cast(str, item["verified_by"]),
                verified_at=cast(str, item["verified_at"]),
            )
        )
    except (TypeError, ValueError, V3ContractError) as error:
        raise FixEvidenceV3ContractError(
            "TBM_FIX_EVIDENCE_INVALID",
            "source_to_fix failed validation",
        ) from error


def _canonical_relation(
    relation: CommitRelationEvidence,
) -> CommitRelationEvidence:
    try:
        return CommitRelationEvidence(
            from_commit_sha=relation.from_commit_sha,
            to_commit_sha=relation.to_commit_sha,
            relation=relation.relation,
            verified_by=relation.verified_by,
            verified_at=canonical_rfc3339(relation.verified_at),
        )
    except (AttributeError, TypeError, ValueError, V3ContractError) as error:
        raise FixEvidenceV3ContractError(
            "TBM_FIX_EVIDENCE_INVALID",
            "source_to_fix failed validation",
        ) from error


def _parse_artifact_hashes(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        _invalid("artifact_hashes must be an array of strings")
    if len(value) > FIX_EVIDENCE_MAX_ARTIFACT_HASHES:
        _invalid("artifact_hashes must be a bounded array")
    if any(type(item) is not str for item in value):
        _invalid("artifact_hashes must be an array of strings")
    result = tuple(sorted(cast(list[str], value)))
    _artifact_hashes(result)
    return result


def _artifact_hashes(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > FIX_EVIDENCE_MAX_ARTIFACT_HASHES
    ):
        _invalid("artifact_hashes must be a bounded tuple")
    hashes = cast(tuple[object, ...], value)
    for digest in hashes:
        _digest(digest, "artifact_hash")
    if len(set(hashes)) != len(hashes):
        _invalid("artifact_hashes must not contain duplicates")
    if tuple(sorted(cast(tuple[str, ...], hashes))) != hashes:
        _invalid("artifact_hashes must be sorted")


def _strict_object(
    value: object,
    label: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _invalid(f"{label} must be an object")
    item = cast(dict[object, object], value)
    if len(item) != len(fields):
        _invalid(f"{label} fields do not match the contract")
    if any(type(key) is not str for key in item):
        _invalid(f"{label} keys must be strings")
    if frozenset(cast(dict[str, object], item)) != fields:
        _invalid(f"{label} fields do not match the contract")
    return cast(dict[str, object], item)


def _identifier(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _IDENTIFIER_MAX_CHARS
    ):
        _invalid(f"{label} must be a nonblank bounded identifier")


def _metadata(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _METADATA_MAX_CHARS
    ):
        _invalid(f"{label} must be nonblank bounded text")


def _timestamp(value: object, label: str) -> None:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        _invalid(f"{label} must be an RFC 3339 timestamp")
    try:
        parse_rfc3339(value)
    except ValueError as error:
        raise FixEvidenceV3ContractError(
            "TBM_FIX_EVIDENCE_INVALID",
            f"{label} must be an RFC 3339 timestamp",
        ) from error


def _digest(value: object, label: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{label} must be sha256:<64 lowercase hex>")


__all__ = [
    "FIX_EVIDENCE_CONTRACT_VERSION",
    "FIX_EVIDENCE_JSON_MAX_BYTES",
    "FIX_EVIDENCE_JSON_MAX_DEPTH",
    "FIX_EVIDENCE_JSON_MAX_NODES",
    "FIX_EVIDENCE_MAX_ARTIFACT_HASHES",
    "FixEvidence",
    "FixEvidenceV3ContractError",
    "build_fix_evidence",
    "dumps_fix_evidence",
    "fix_evidence_id",
    "loads_fix_evidence",
    "parse_fix_evidence",
]
