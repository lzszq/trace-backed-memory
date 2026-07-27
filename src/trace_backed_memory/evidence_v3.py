from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Literal, Mapping, cast

from ._ingestion import (
    decode_bounded_utf8,
    parse_bounded_json,
)
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import (
    CommitRelationEvidence,
    V3ContractError,
    canonical_sha256,
)


REGRESSION_EVIDENCE_CONTRACT_VERSION = "tbm.regression-evidence.v3"
EVIDENCE_JSON_MAX_BYTES = 1024 * 1024
EVIDENCE_JSON_MAX_DEPTH = 32
EVIDENCE_JSON_MAX_NODES = 10_000
EVIDENCE_MAX_ARTIFACT_HASHES = 1_000
EVIDENCE_MAX_ENVIRONMENT_ENTRIES = 100

_IDENTIFIER_MAX_CHARS = 128
_METADATA_MAX_CHARS = 2_000
_ENVIRONMENT_KEY_MAX_CHARS = 256
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_ID_RE = re.compile(r"^regression_sha256_[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])$"
)
_RESULTS = frozenset({"pass", "fail", "error"})
_FIELDS = frozenset(
    {
        "contract_version",
        "evidence_id",
        "case_id",
        "source_trace_id",
        "verification_trace_id",
        "verification_run_id",
        "evaluator_id",
        "evaluator_version",
        "evaluation_suite",
        "evaluation_case_id",
        "expected_outcome",
        "observed_outcome",
        "result",
        "environment",
        "source_commit_sha",
        "fix_commit_sha",
        "verification_commit_sha",
        "source_to_fix",
        "fix_to_verification",
        "artifact_hashes",
        "submitter_id",
        "submitted_at",
        "verifier_id",
        "verified_at",
        "attestation_sha256",
    }
)


class EvidenceV3ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> None:
    raise EvidenceV3ContractError("TBM_EVIDENCE_INVALID", message)


@dataclass(frozen=True)
class StructuredRegressionEvidence:
    evidence_id: str
    case_id: str
    source_trace_id: str
    verification_trace_id: str
    verification_run_id: str
    evaluator_id: str
    evaluator_version: str
    evaluation_suite: str
    evaluation_case_id: str
    expected_outcome: str
    observed_outcome: str
    result: Literal["pass", "fail", "error"]
    environment: tuple[tuple[str, str], ...]
    source_commit_sha: str
    fix_commit_sha: str
    verification_commit_sha: str
    source_to_fix: CommitRelationEvidence
    fix_to_verification: CommitRelationEvidence
    artifact_hashes: tuple[str, ...]
    submitter_id: str
    submitted_at: str
    verifier_id: str
    verified_at: str
    attestation_sha256: str
    contract_version: str = REGRESSION_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.submitted_at, "submitted_at")
        _timestamp(self.verified_at, "verified_at")
        object.__setattr__(
            self,
            "submitted_at",
            canonical_rfc3339(self.submitted_at),
        )
        object.__setattr__(
            self,
            "verified_at",
            canonical_rfc3339(self.verified_at),
        )
        if self.contract_version != REGRESSION_EVIDENCE_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.evidence_id) is not str or not _EVIDENCE_ID_RE.fullmatch(
            self.evidence_id
        ):
            _invalid("evidence_id must be regression_sha256_<64 lowercase hex>")
        for name in (
            "case_id",
            "source_trace_id",
            "verification_trace_id",
            "verification_run_id",
            "evaluator_id",
            "submitter_id",
            "verifier_id",
        ):
            _identifier(getattr(self, name), name)
        if self.source_trace_id == self.verification_trace_id:
            _invalid("verification_trace_id must differ from source_trace_id")
        if self.submitter_id == self.verifier_id:
            _invalid("submitter_id and verifier_id must differ")
        for name in (
            "evaluator_version",
            "evaluation_suite",
            "evaluation_case_id",
            "expected_outcome",
            "observed_outcome",
            "source_commit_sha",
            "fix_commit_sha",
            "verification_commit_sha",
        ):
            _metadata(getattr(self, name), name)
        if type(self.result) is not str or self.result not in _RESULTS:
            _invalid("result must be pass, fail, or error")
        _environment(self.environment)
        if type(self.source_to_fix) is not CommitRelationEvidence:
            _invalid("source_to_fix must be exactly CommitRelationEvidence")
        if type(self.fix_to_verification) is not CommitRelationEvidence:
            _invalid(
                "fix_to_verification must be exactly CommitRelationEvidence"
            )
        _timestamp(self.source_to_fix.verified_at, "source_to_fix.verified_at")
        _timestamp(
            self.fix_to_verification.verified_at,
            "fix_to_verification.verified_at",
        )
        object.__setattr__(
            self,
            "source_to_fix",
            _canonical_relation(self.source_to_fix),
        )
        object.__setattr__(
            self,
            "fix_to_verification",
            _canonical_relation(self.fix_to_verification),
        )
        if (
            self.source_to_fix.from_commit_sha != self.source_commit_sha
            or self.source_to_fix.to_commit_sha != self.fix_commit_sha
        ):
            _invalid("source_to_fix does not bind source and fix commits")
        if (
            self.fix_to_verification.from_commit_sha != self.fix_commit_sha
            or self.fix_to_verification.to_commit_sha
            != self.verification_commit_sha
        ):
            _invalid(
                "fix_to_verification does not bind fix and verification commits"
            )
        _artifact_hashes(self.artifact_hashes)
        submitted_at = parse_rfc3339(self.submitted_at)
        verified_at = parse_rfc3339(self.verified_at)
        source_relation_at = parse_rfc3339(self.source_to_fix.verified_at)
        fix_relation_at = parse_rfc3339(self.fix_to_verification.verified_at)
        if verified_at < submitted_at:
            _invalid("verified_at must not precede submitted_at")
        if source_relation_at > fix_relation_at:
            _invalid(
                "source_to_fix verification must not follow "
                "fix_to_verification"
            )
        if fix_relation_at > submitted_at:
            _invalid("commit relationships must be verified before submission")
        _digest(self.attestation_sha256, "attestation_sha256")
        expected_id = regression_evidence_id(self._unsigned_dict())
        if self.evidence_id != expected_id:
            raise EvidenceV3ContractError(
                "TBM_EVIDENCE_HASH_MISMATCH",
                "evidence_id does not match canonical evidence content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "case_id": self.case_id,
            "source_trace_id": self.source_trace_id,
            "verification_trace_id": self.verification_trace_id,
            "verification_run_id": self.verification_run_id,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "evaluation_suite": self.evaluation_suite,
            "evaluation_case_id": self.evaluation_case_id,
            "expected_outcome": self.expected_outcome,
            "observed_outcome": self.observed_outcome,
            "result": self.result,
            "environment": dict(self.environment),
            "source_commit_sha": self.source_commit_sha,
            "fix_commit_sha": self.fix_commit_sha,
            "verification_commit_sha": self.verification_commit_sha,
            "source_to_fix": self.source_to_fix.to_dict(),
            "fix_to_verification": self.fix_to_verification.to_dict(),
            "artifact_hashes": list(self.artifact_hashes),
            "submitter_id": self.submitter_id,
            "submitted_at": canonical_rfc3339(self.submitted_at),
            "verifier_id": self.verifier_id,
            "verified_at": canonical_rfc3339(self.verified_at),
            "attestation_sha256": self.attestation_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        result = self._unsigned_dict()
        result["evidence_id"] = self.evidence_id
        return result


def regression_evidence_id(content: Mapping[str, object]) -> str:
    return "regression_sha256_" + canonical_sha256(content).removeprefix(
        "sha256:"
    )


def build_structured_regression_evidence(
    *,
    case_id: str,
    source_trace_id: str,
    verification_trace_id: str,
    verification_run_id: str,
    evaluator_id: str,
    evaluator_version: str,
    evaluation_suite: str,
    evaluation_case_id: str,
    expected_outcome: str,
    observed_outcome: str,
    result: Literal["pass", "fail", "error"],
    environment: Mapping[str, str],
    source_commit_sha: str,
    fix_commit_sha: str,
    verification_commit_sha: str,
    source_to_fix: CommitRelationEvidence,
    fix_to_verification: CommitRelationEvidence,
    artifact_hashes: tuple[str, ...],
    submitter_id: str,
    submitted_at: str,
    verifier_id: str,
    verified_at: str,
    attestation_sha256: str,
) -> StructuredRegressionEvidence:
    if not isinstance(environment, Mapping):
        _invalid("environment must be a mapping")
    try:
        environment_payload = dict(environment)
    except (TypeError, ValueError) as error:
        raise EvidenceV3ContractError(
            "TBM_EVIDENCE_INVALID",
            "environment must be a bounded string mapping",
        ) from error
    if type(artifact_hashes) is not tuple:
        _invalid("artifact_hashes must be a tuple")
    if type(source_to_fix) is not CommitRelationEvidence:
        _invalid("source_to_fix must be exactly CommitRelationEvidence")
    if type(fix_to_verification) is not CommitRelationEvidence:
        _invalid(
            "fix_to_verification must be exactly CommitRelationEvidence"
        )
    _timestamp(submitted_at, "submitted_at")
    _timestamp(verified_at, "verified_at")
    canonical_source_to_fix = _validated_canonical_relation(
        source_to_fix,
        "source_to_fix",
    )
    canonical_fix_to_verification = _validated_canonical_relation(
        fix_to_verification,
        "fix_to_verification",
    )
    environment_entries = _parse_environment(environment_payload)
    artifact_entries = _parse_artifact_hashes(list(artifact_hashes))
    values: dict[str, object] = {
        "contract_version": REGRESSION_EVIDENCE_CONTRACT_VERSION,
        "case_id": case_id,
        "source_trace_id": source_trace_id,
        "verification_trace_id": verification_trace_id,
        "verification_run_id": verification_run_id,
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
        "evaluation_suite": evaluation_suite,
        "evaluation_case_id": evaluation_case_id,
        "expected_outcome": expected_outcome,
        "observed_outcome": observed_outcome,
        "result": result,
        "environment": dict(environment_entries),
        "source_commit_sha": source_commit_sha,
        "fix_commit_sha": fix_commit_sha,
        "verification_commit_sha": verification_commit_sha,
        "source_to_fix": canonical_source_to_fix.to_dict(),
        "fix_to_verification": canonical_fix_to_verification.to_dict(),
        "artifact_hashes": list(artifact_entries),
        "submitter_id": submitter_id,
        "submitted_at": canonical_rfc3339(submitted_at),
        "verifier_id": verifier_id,
        "verified_at": canonical_rfc3339(verified_at),
        "attestation_sha256": attestation_sha256,
    }
    return StructuredRegressionEvidence(
        evidence_id=regression_evidence_id(values),
        case_id=case_id,
        source_trace_id=source_trace_id,
        verification_trace_id=verification_trace_id,
        verification_run_id=verification_run_id,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        evaluation_suite=evaluation_suite,
        evaluation_case_id=evaluation_case_id,
        expected_outcome=expected_outcome,
        observed_outcome=observed_outcome,
        result=result,
        environment=environment_entries,
        source_commit_sha=source_commit_sha,
        fix_commit_sha=fix_commit_sha,
        verification_commit_sha=verification_commit_sha,
        source_to_fix=canonical_source_to_fix,
        fix_to_verification=canonical_fix_to_verification,
        artifact_hashes=artifact_entries,
        submitter_id=submitter_id,
        submitted_at=canonical_rfc3339(submitted_at),
        verifier_id=verifier_id,
        verified_at=canonical_rfc3339(verified_at),
        attestation_sha256=attestation_sha256,
    )


def parse_structured_regression_evidence(
    payload: Mapping[str, object],
) -> StructuredRegressionEvidence:
    item = _strict_object(payload, "structured regression evidence", _FIELDS)
    source_to_fix = _parse_relation(item["source_to_fix"], "source_to_fix")
    fix_to_verification = _parse_relation(
        item["fix_to_verification"],
        "fix_to_verification",
    )
    environment = _parse_environment(item["environment"])
    artifact_hashes = _parse_artifact_hashes(item["artifact_hashes"])
    return StructuredRegressionEvidence(
        evidence_id=cast(str, item["evidence_id"]),
        case_id=cast(str, item["case_id"]),
        source_trace_id=cast(str, item["source_trace_id"]),
        verification_trace_id=cast(str, item["verification_trace_id"]),
        verification_run_id=cast(str, item["verification_run_id"]),
        evaluator_id=cast(str, item["evaluator_id"]),
        evaluator_version=cast(str, item["evaluator_version"]),
        evaluation_suite=cast(str, item["evaluation_suite"]),
        evaluation_case_id=cast(str, item["evaluation_case_id"]),
        expected_outcome=cast(str, item["expected_outcome"]),
        observed_outcome=cast(str, item["observed_outcome"]),
        result=cast(Literal["pass", "fail", "error"], item["result"]),
        environment=environment,
        source_commit_sha=cast(str, item["source_commit_sha"]),
        fix_commit_sha=cast(str, item["fix_commit_sha"]),
        verification_commit_sha=cast(str, item["verification_commit_sha"]),
        source_to_fix=source_to_fix,
        fix_to_verification=fix_to_verification,
        artifact_hashes=artifact_hashes,
        submitter_id=cast(str, item["submitter_id"]),
        submitted_at=cast(str, item["submitted_at"]),
        verifier_id=cast(str, item["verifier_id"]),
        verified_at=cast(str, item["verified_at"]),
        attestation_sha256=cast(str, item["attestation_sha256"]),
        contract_version=cast(str, item["contract_version"]),
    )


def loads_structured_regression_evidence(
    document: str | bytes,
) -> StructuredRegressionEvidence:
    try:
        raw = (
            decode_bounded_utf8(
                document,
                max_bytes=EVIDENCE_JSON_MAX_BYTES,
                description="structured regression evidence",
            )
            if type(document) is bytes
            else document
        )
        if type(raw) is not str or len(raw.encode("utf-8")) > EVIDENCE_JSON_MAX_BYTES:
            raise ValueError(
                "structured regression evidence exceeds byte limit"
            )
        payload = parse_bounded_json(
            raw,
            description="structured regression evidence",
            max_nodes=EVIDENCE_JSON_MAX_NODES,
            max_depth=EVIDENCE_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise EvidenceV3ContractError(
            "TBM_EVIDENCE_INVALID_JSON",
            "structured regression evidence must be bounded strict JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("structured regression evidence must be an object")
    return parse_structured_regression_evidence(cast(dict[str, object], payload))


def dumps_structured_regression_evidence(
    evidence: StructuredRegressionEvidence,
) -> str:
    if type(evidence) is not StructuredRegressionEvidence:
        _invalid("evidence must be exactly StructuredRegressionEvidence")
    return json.dumps(
        evidence.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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


def _parse_relation(value: object, label: str) -> CommitRelationEvidence:
    fields = frozenset(
        {
            "from_commit_sha",
            "to_commit_sha",
            "relation",
            "verified_by",
            "verified_at",
        }
    )
    item = _strict_object(value, label, fields)
    try:
        relation = CommitRelationEvidence(
            from_commit_sha=cast(str, item["from_commit_sha"]),
            to_commit_sha=cast(str, item["to_commit_sha"]),
            relation=cast(Literal["ancestor"], item["relation"]),
            verified_by=cast(str, item["verified_by"]),
            verified_at=cast(str, item["verified_at"]),
        )
    except V3ContractError as error:
        raise EvidenceV3ContractError(
            "TBM_EVIDENCE_INVALID",
            f"{label} failed validation",
        ) from error
    _timestamp(relation.verified_at, f"{label}.verified_at")
    return _canonical_relation(relation)


def _parse_environment(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not dict:
        _invalid("environment must be an object")
    item = cast(dict[object, object], value)
    if any(type(key) is not str or type(entry) is not str for key, entry in item.items()):
        _invalid("environment keys and values must be strings")
    result = tuple(sorted(cast(dict[str, str], item).items()))
    _environment(result)
    return result


def _parse_artifact_hashes(value: object) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid("artifact_hashes must be an array of strings")
    result = tuple(sorted(cast(list[str], value)))
    _artifact_hashes(result)
    return result


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
    if (
        type(value) is not str
        or not _TIMESTAMP_RE.fullmatch(cast(str, value))
    ):
        _invalid(f"{label} must be an RFC 3339 timestamp")
    try:
        parse_rfc3339(cast(str, value))
    except ValueError as error:
        raise EvidenceV3ContractError(
            "TBM_EVIDENCE_INVALID",
            f"{label} must be an RFC 3339 timestamp",
        ) from error


def _canonical_relation(
    relation: CommitRelationEvidence,
) -> CommitRelationEvidence:
    return CommitRelationEvidence(
        from_commit_sha=relation.from_commit_sha,
        to_commit_sha=relation.to_commit_sha,
        relation=relation.relation,
        verified_by=relation.verified_by,
        verified_at=canonical_rfc3339(relation.verified_at),
    )


def _validated_canonical_relation(
    relation: CommitRelationEvidence,
    label: str,
) -> CommitRelationEvidence:
    try:
        _timestamp(relation.verified_at, f"{label}.verified_at")
        return _canonical_relation(relation)
    except EvidenceV3ContractError:
        raise
    except (AttributeError, TypeError, V3ContractError, ValueError) as error:
        raise EvidenceV3ContractError(
            "TBM_EVIDENCE_INVALID",
            f"{label} failed validation",
        ) from error


def _digest(value: object, label: str) -> None:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _invalid(f"{label} must be sha256:<64 lowercase hex>")


def _environment(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > EVIDENCE_MAX_ENVIRONMENT_ENTRIES
    ):
        _invalid("environment must be a bounded tuple")
    pairs = cast(tuple[object, ...], value)
    keys: list[str] = []
    normalized: list[tuple[str, str]] = []
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            _invalid("environment entries must be key/value pairs")
        pair_items = cast(tuple[object, object], pair)
        key, entry = pair_items
        if (
            type(key) is not str
            or not key.strip()
            or len(key) > _ENVIRONMENT_KEY_MAX_CHARS
        ):
            _invalid("environment keys must be nonblank bounded strings")
        _metadata(entry, "environment value")
        normalized_key = cast(str, key)
        normalized_entry = cast(str, entry)
        keys.append(normalized_key)
        normalized.append((normalized_key, normalized_entry))
    if tuple(sorted(normalized)) != tuple(normalized):
        _invalid("environment entries must be sorted")
    if len(set(keys)) != len(keys):
        _invalid("environment keys must be unique")


def _artifact_hashes(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > EVIDENCE_MAX_ARTIFACT_HASHES
    ):
        _invalid("artifact_hashes must be a bounded tuple")
    hashes = cast(tuple[object, ...], value)
    for digest in hashes:
        _digest(digest, "artifact_hash")
    if len(set(hashes)) != len(hashes):
        _invalid("artifact_hashes must not contain duplicates")
    if tuple(sorted(cast(tuple[str, ...], hashes))) != hashes:
        _invalid("artifact_hashes must be sorted")
