from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Literal, Mapping, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import canonical_sha256


RETRIEVAL_SNAPSHOT_CONTRACT_VERSION = "tbm.retrieval-snapshot.v3"
RETRIEVAL_SNAPSHOT_JSON_MAX_BYTES = 1024 * 1024
RETRIEVAL_SNAPSHOT_JSON_MAX_DEPTH = 32
RETRIEVAL_SNAPSHOT_JSON_MAX_NODES = 20_000
RETRIEVAL_SNAPSHOT_MAX_HITS = 100
RETRIEVAL_SNAPSHOT_MAX_INDEX_VERSIONS = 16
RETRIEVAL_SNAPSHOT_MAX_TRUNCATION_REASONS = 16
RETRIEVAL_SNAPSHOT_MAX_TOTAL_CANDIDATES = 1_000_000

_IDENTIFIER_MAX_CHARS = 128
_SNAPSHOT_ID_RE = re.compile(r"^retrieval_snapshot_sha256_[0-9a-f]{64}$")
_REVISION_ID_RE = re.compile(r"^memory_revision_sha256_[0-9a-f]{64}$")
_AUTHORIZATION_EVENT_ID_RE = re.compile(r"^authz_sha256_[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])$"
)
_INDEX_KINDS = (
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "git_graph",
)
_INDEX_KIND_ORDER = {value: index for index, value in enumerate(_INDEX_KINDS)}
_STAGES = (
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "fusion",
)
_STAGE_ORDER = {value: index for index, value in enumerate(_STAGES)}
_MODES = frozenset(
    {
        "metadata",
        "lexical",
        "semantic",
        "evidence_graph",
        "hybrid",
    }
)
_TRUNCATION_REASONS = frozenset(
    {
        "top_k",
        "minimum_score",
        "authorization",
        "lifecycle",
        "classification",
        "applicability",
        "eval_leakage",
        "git_ancestry",
        "payload_budget",
    }
)
_INDEX_FIELDS = frozenset(
    {"index_kind", "index_id", "index_version", "content_sha256"}
)
_HIT_FIELDS = frozenset(
    {
        "memory_id",
        "memory_revision_id",
        "candidate_sha256",
        "rank",
        "metadata_score",
        "lexical_score",
        "semantic_score",
        "evidence_graph_score",
        "fused_score",
        "selected_stages",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "contract_version",
        "snapshot_id",
        "session_id",
        "request_id",
        "trace_id",
        "run_id",
        "authorization_event_id",
        "context_sha256",
        "query_sha256",
        "retrieval_mode",
        "retriever_id",
        "retriever_version",
        "index_versions",
        "hits",
        "total_candidates",
        "top_k",
        "truncated",
        "truncation_reasons",
        "created_at",
    }
)

IndexKind = Literal[
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "git_graph",
]
RetrievalMode = Literal[
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "hybrid",
]
RetrievalStage = Literal[
    "metadata",
    "lexical",
    "semantic",
    "evidence_graph",
    "fusion",
]
TruncationReason = Literal[
    "top_k",
    "minimum_score",
    "authorization",
    "lifecycle",
    "classification",
    "applicability",
    "eval_leakage",
    "git_ancestry",
    "payload_budget",
]


class RetrievalSnapshotContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> NoReturn:
    raise RetrievalSnapshotContractError(
        "TBM_RETRIEVAL_SNAPSHOT_INVALID",
        message,
    )


@dataclass(frozen=True)
class IndexVersion:
    index_kind: IndexKind
    index_id: str
    index_version: str
    content_sha256: str

    def __post_init__(self) -> None:
        if type(self.index_kind) is not str or self.index_kind not in _INDEX_KIND_ORDER:
            _invalid("index_kind is not supported")
        _identifier(self.index_id, "index_id")
        _identifier(self.index_version, "index_version")
        _digest(self.content_sha256, "index content_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "index_kind": self.index_kind,
            "index_id": self.index_id,
            "index_version": self.index_version,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class RetrievalHit:
    memory_id: str
    memory_revision_id: str
    candidate_sha256: str
    rank: int
    metadata_score: float | None
    lexical_score: float | None
    semantic_score: float | None
    evidence_graph_score: float | None
    fused_score: float
    selected_stages: tuple[RetrievalStage, ...]

    def __post_init__(self) -> None:
        _identifier(self.memory_id, "memory_id")
        if (
            type(self.memory_revision_id) is not str
            or not _REVISION_ID_RE.fullmatch(self.memory_revision_id)
        ):
            _invalid("memory_revision_id must reference a memory revision")
        _digest(self.candidate_sha256, "candidate_sha256")
        if type(self.rank) is not int or self.rank < 1:
            _invalid("rank must be a positive integer")
        for field in (
            "metadata_score",
            "lexical_score",
            "semantic_score",
            "evidence_graph_score",
        ):
            object.__setattr__(
                self,
                field,
                _optional_score(getattr(self, field), field),
            )
        object.__setattr__(self, "fused_score", _score(self.fused_score, "fused_score"))
        _selected_stages(self.selected_stages)
        score_by_stage = {
            "metadata": self.metadata_score,
            "lexical": self.lexical_score,
            "semantic": self.semantic_score,
            "evidence_graph": self.evidence_graph_score,
        }
        selected = frozenset(self.selected_stages)
        if "fusion" not in selected:
            _invalid("selected_stages must include fusion")
        for stage, score in score_by_stage.items():
            if (stage in selected) != (score is not None):
                _invalid("selected_stages must exactly match recorded stage scores")

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.memory_id,
            "memory_revision_id": self.memory_revision_id,
            "candidate_sha256": self.candidate_sha256,
            "rank": self.rank,
            "metadata_score": self.metadata_score,
            "lexical_score": self.lexical_score,
            "semantic_score": self.semantic_score,
            "evidence_graph_score": self.evidence_graph_score,
            "fused_score": self.fused_score,
            "selected_stages": list(self.selected_stages),
        }


@dataclass(frozen=True)
class RetrievalSnapshot:
    snapshot_id: str
    session_id: str
    request_id: str
    trace_id: str
    run_id: str
    authorization_event_id: str
    context_sha256: str
    query_sha256: str | None
    retrieval_mode: RetrievalMode
    retriever_id: str
    retriever_version: str
    index_versions: tuple[IndexVersion, ...]
    hits: tuple[RetrievalHit, ...]
    total_candidates: int
    top_k: int
    truncated: bool
    truncation_reasons: tuple[TruncationReason, ...]
    created_at: str
    contract_version: str = RETRIEVAL_SNAPSHOT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "created_at", canonical_rfc3339(self.created_at))
        if self.contract_version != RETRIEVAL_SNAPSHOT_CONTRACT_VERSION:
            _invalid("contract_version is not supported")
        if type(self.snapshot_id) is not str or not _SNAPSHOT_ID_RE.fullmatch(
            self.snapshot_id
        ):
            _invalid(
                "snapshot_id must be retrieval_snapshot_sha256_<64 lowercase hex>"
            )
        for field in ("session_id", "request_id", "trace_id", "run_id"):
            _identifier(getattr(self, field), field)
        if (
            type(self.authorization_event_id) is not str
            or not _AUTHORIZATION_EVENT_ID_RE.fullmatch(
                self.authorization_event_id
            )
        ):
            _invalid("authorization_event_id must reference an authorization event")
        _digest(self.context_sha256, "context_sha256")
        if self.query_sha256 is not None:
            _digest(self.query_sha256, "query_sha256")
        if type(self.retrieval_mode) is not str or self.retrieval_mode not in _MODES:
            _invalid("retrieval_mode is not supported")
        _identifier(self.retriever_id, "retriever_id")
        _identifier(self.retriever_version, "retriever_version")
        _index_versions(self.index_versions)
        _hits(self.hits)
        index_kinds = frozenset(item.index_kind for item in self.index_versions)
        ranking_index_kinds = index_kinds.intersection(
            {"metadata", "lexical", "semantic", "evidence_graph"}
        )
        if self.retrieval_mode == "hybrid":
            if len(ranking_index_kinds) < 2:
                _invalid("hybrid retrieval requires at least two index versions")
        elif self.retrieval_mode not in index_kinds:
            _invalid("retrieval_mode requires a matching index version")
        for hit in self.hits:
            ranking_stages = tuple(
                stage for stage in hit.selected_stages if stage != "fusion"
            )
            if any(stage not in index_kinds for stage in ranking_stages):
                _invalid("every selected retrieval stage requires an index version")
            if self.retrieval_mode == "hybrid":
                if len(ranking_stages) < 2:
                    _invalid("hybrid retrieval requires at least two ranking stages")
            elif self.retrieval_mode not in ranking_stages:
                _invalid("retrieval_mode must be selected for every hit")
        if (
            type(self.total_candidates) is not int
            or self.total_candidates < len(self.hits)
            or self.total_candidates > RETRIEVAL_SNAPSHOT_MAX_TOTAL_CANDIDATES
        ):
            _invalid("total_candidates must be bounded and cover every recorded hit")
        if (
            type(self.top_k) is not int
            or self.top_k < 1
            or self.top_k > RETRIEVAL_SNAPSHOT_MAX_HITS
            or len(self.hits) > self.top_k
        ):
            _invalid("top_k must be bounded and cover every recorded hit")
        if type(self.truncated) is not bool:
            _invalid("truncated must be a boolean")
        _truncation_reasons(self.truncation_reasons)
        if self.truncated != bool(self.truncation_reasons):
            _invalid("truncated and truncation_reasons must agree")
        if self.total_candidates > len(self.hits) and not self.truncated:
            _invalid("omitted candidates require an explicit truncation reason")
        expected = retrieval_snapshot_id(self._unsigned_dict())
        if self.snapshot_id != expected:
            raise RetrievalSnapshotContractError(
                "TBM_RETRIEVAL_SNAPSHOT_HASH_MISMATCH",
                "snapshot_id does not match canonical retrieval content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "authorization_event_id": self.authorization_event_id,
            "context_sha256": self.context_sha256,
            "query_sha256": self.query_sha256,
            "retrieval_mode": self.retrieval_mode,
            "retriever_id": self.retriever_id,
            "retriever_version": self.retriever_version,
            "index_versions": [item.to_dict() for item in self.index_versions],
            "hits": [item.to_dict() for item in self.hits],
            "total_candidates": self.total_candidates,
            "top_k": self.top_k,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"snapshot_id": self.snapshot_id, **self._unsigned_dict()}


def retrieval_snapshot_id(payload: Mapping[str, object]) -> str:
    return "retrieval_snapshot_sha256_" + canonical_sha256(payload).removeprefix(
        "sha256:"
    )


def build_retrieval_snapshot(
    *,
    session_id: str,
    request_id: str,
    trace_id: str,
    run_id: str,
    authorization_event_id: str,
    context_sha256: str,
    query_sha256: str | None,
    retrieval_mode: RetrievalMode,
    retriever_id: str,
    retriever_version: str,
    index_versions: tuple[IndexVersion, ...],
    hits: tuple[RetrievalHit, ...],
    total_candidates: int,
    top_k: int,
    truncated: bool,
    truncation_reasons: tuple[TruncationReason, ...],
    created_at: str,
) -> RetrievalSnapshot:
    if type(index_versions) is not tuple or any(
        type(item) is not IndexVersion for item in index_versions
    ):
        _invalid("index_versions must be a tuple of IndexVersion records")
    if type(hits) is not tuple or any(type(item) is not RetrievalHit for item in hits):
        _invalid("hits must be a tuple of RetrievalHit records")
    if type(truncation_reasons) is not tuple or any(
        type(item) is not str for item in truncation_reasons
    ):
        _invalid("truncation_reasons must be a tuple of strings")
    canonical_indexes = tuple(
        sorted(
            index_versions,
            key=lambda item: (_INDEX_KIND_ORDER[item.index_kind], item.index_id),
        )
    )
    canonical_hits = tuple(sorted(hits, key=lambda item: item.rank))
    canonical_reasons = tuple(sorted(truncation_reasons))
    canonical_time = _canonical_timestamp(created_at, "created_at")
    values: dict[str, object] = {
        "contract_version": RETRIEVAL_SNAPSHOT_CONTRACT_VERSION,
        "session_id": session_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "authorization_event_id": authorization_event_id,
        "context_sha256": context_sha256,
        "query_sha256": query_sha256,
        "retrieval_mode": retrieval_mode,
        "retriever_id": retriever_id,
        "retriever_version": retriever_version,
        "index_versions": [item.to_dict() for item in canonical_indexes],
        "hits": [item.to_dict() for item in canonical_hits],
        "total_candidates": total_candidates,
        "top_k": top_k,
        "truncated": truncated,
        "truncation_reasons": list(canonical_reasons),
        "created_at": canonical_time,
    }
    try:
        snapshot_id = retrieval_snapshot_id(values)
    except ValueError as error:
        raise RetrievalSnapshotContractError(
            "TBM_RETRIEVAL_SNAPSHOT_INVALID",
            "retrieval snapshot content is not canonical JSON",
        ) from error
    return RetrievalSnapshot(
        snapshot_id=snapshot_id,
        session_id=session_id,
        request_id=request_id,
        trace_id=trace_id,
        run_id=run_id,
        authorization_event_id=authorization_event_id,
        context_sha256=context_sha256,
        query_sha256=query_sha256,
        retrieval_mode=retrieval_mode,
        retriever_id=retriever_id,
        retriever_version=retriever_version,
        index_versions=canonical_indexes,
        hits=canonical_hits,
        total_candidates=total_candidates,
        top_k=top_k,
        truncated=truncated,
        truncation_reasons=cast(
            tuple[TruncationReason, ...],
            canonical_reasons,
        ),
        created_at=canonical_time,
    )


def dumps_retrieval_snapshot(snapshot: RetrievalSnapshot) -> str:
    if type(snapshot) is not RetrievalSnapshot:
        _invalid("snapshot must be exactly RetrievalSnapshot")
    return json.dumps(
        snapshot.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def loads_retrieval_snapshot(document: str | bytes) -> RetrievalSnapshot:
    if type(document) is bytes:
        try:
            source = decode_bounded_utf8(
                document,
                max_bytes=RETRIEVAL_SNAPSHOT_JSON_MAX_BYTES,
                description="retrieval snapshot JSON",
            )
        except (UnicodeError, ValueError) as error:
            raise RetrievalSnapshotContractError(
                "TBM_RETRIEVAL_SNAPSHOT_INVALID_JSON",
                "retrieval snapshot must be bounded strict JSON",
            ) from error
    else:
        source = document
    try:
        if type(source) is not str:
            raise ValueError("retrieval snapshot source must be str or bytes")
        if len(source.encode("utf-8")) > RETRIEVAL_SNAPSHOT_JSON_MAX_BYTES:
            raise ValueError("retrieval snapshot exceeds byte limit")
        payload = parse_bounded_json(
            source,
            description="retrieval snapshot",
            max_nodes=RETRIEVAL_SNAPSHOT_JSON_MAX_NODES,
            max_depth=RETRIEVAL_SNAPSHOT_JSON_MAX_DEPTH,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise RetrievalSnapshotContractError(
            "TBM_RETRIEVAL_SNAPSHOT_INVALID_JSON",
            "retrieval snapshot must be bounded strict JSON",
        ) from error
    if type(payload) is not dict:
        _invalid("retrieval snapshot must be an object")
    return parse_retrieval_snapshot(cast(dict[str, object], payload))


def parse_retrieval_snapshot(
    payload: Mapping[str, object],
) -> RetrievalSnapshot:
    item = _strict_object(payload, "retrieval snapshot", _SNAPSHOT_FIELDS)
    index_values = item["index_versions"]
    hit_values = item["hits"]
    if type(index_values) is not list:
        _invalid("index_versions must be an array")
    if type(hit_values) is not list:
        _invalid("hits must be an array")
    indexes = tuple(_parse_index(value) for value in index_values)
    hits = tuple(_parse_hit(value) for value in hit_values)
    reasons = _parse_truncation_reasons(item["truncation_reasons"])
    return RetrievalSnapshot(
        snapshot_id=cast(str, item["snapshot_id"]),
        session_id=cast(str, item["session_id"]),
        request_id=cast(str, item["request_id"]),
        trace_id=cast(str, item["trace_id"]),
        run_id=cast(str, item["run_id"]),
        authorization_event_id=cast(str, item["authorization_event_id"]),
        context_sha256=cast(str, item["context_sha256"]),
        query_sha256=cast(str | None, item["query_sha256"]),
        retrieval_mode=cast(RetrievalMode, item["retrieval_mode"]),
        retriever_id=cast(str, item["retriever_id"]),
        retriever_version=cast(str, item["retriever_version"]),
        index_versions=indexes,
        hits=hits,
        total_candidates=cast(int, item["total_candidates"]),
        top_k=cast(int, item["top_k"]),
        truncated=cast(bool, item["truncated"]),
        truncation_reasons=reasons,
        created_at=cast(str, item["created_at"]),
        contract_version=cast(str, item["contract_version"]),
    )


def _parse_index(value: object) -> IndexVersion:
    item = _strict_object(value, "index version", _INDEX_FIELDS)
    return IndexVersion(
        index_kind=cast(IndexKind, item["index_kind"]),
        index_id=cast(str, item["index_id"]),
        index_version=cast(str, item["index_version"]),
        content_sha256=cast(str, item["content_sha256"]),
    )


def _parse_hit(value: object) -> RetrievalHit:
    item = _strict_object(value, "retrieval hit", _HIT_FIELDS)
    stages = item["selected_stages"]
    if type(stages) is not list or any(type(stage) is not str for stage in stages):
        _invalid("selected_stages must be an array of strings")
    return RetrievalHit(
        memory_id=cast(str, item["memory_id"]),
        memory_revision_id=cast(str, item["memory_revision_id"]),
        candidate_sha256=cast(str, item["candidate_sha256"]),
        rank=cast(int, item["rank"]),
        metadata_score=cast(float | None, item["metadata_score"]),
        lexical_score=cast(float | None, item["lexical_score"]),
        semantic_score=cast(float | None, item["semantic_score"]),
        evidence_graph_score=cast(float | None, item["evidence_graph_score"]),
        fused_score=cast(float, item["fused_score"]),
        selected_stages=cast(tuple[RetrievalStage, ...], tuple(stages)),
    )


def _parse_truncation_reasons(value: object) -> tuple[TruncationReason, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _invalid("truncation_reasons must be an array of strings")
    return cast(tuple[TruncationReason, ...], tuple(value))


def _index_versions(value: object) -> None:
    if (
        type(value) is not tuple
        or not value
        or len(value) > RETRIEVAL_SNAPSHOT_MAX_INDEX_VERSIONS
        or any(type(item) is not IndexVersion for item in value)
    ):
        _invalid("index_versions must be a nonempty bounded tuple")
    indexes = cast(tuple[IndexVersion, ...], value)
    identities = tuple((item.index_kind, item.index_id) for item in indexes)
    if len(set(identities)) != len(identities):
        _invalid("index_versions must not contain duplicate identities")
    expected = tuple(
        sorted(
            indexes,
            key=lambda item: (_INDEX_KIND_ORDER[item.index_kind], item.index_id),
        )
    )
    if indexes != expected:
        _invalid("index_versions must use canonical order")


def _hits(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > RETRIEVAL_SNAPSHOT_MAX_HITS
        or any(type(item) is not RetrievalHit for item in value)
    ):
        _invalid("hits must be a bounded tuple")
    hits = cast(tuple[RetrievalHit, ...], value)
    if tuple(item.rank for item in hits) != tuple(range(1, len(hits) + 1)):
        _invalid("hit ranks must be unique, contiguous, and ordered")
    if len({item.memory_revision_id for item in hits}) != len(hits):
        _invalid("hits must not repeat a memory revision")
    if len({item.candidate_sha256 for item in hits}) != len(hits):
        _invalid("hits must not repeat a candidate hash")


def _selected_stages(value: object) -> None:
    if type(value) is not tuple or not value:
        _invalid("selected_stages must be a nonempty tuple")
    stages = cast(tuple[object, ...], value)
    if any(type(item) is not str or item not in _STAGE_ORDER for item in stages):
        _invalid("selected_stages contains an unsupported stage")
    if len(set(stages)) != len(stages):
        _invalid("selected_stages must not contain duplicates")
    expected = tuple(sorted(cast(tuple[str, ...], stages), key=_STAGE_ORDER.__getitem__))
    if stages != expected:
        _invalid("selected_stages must use pipeline order")


def _truncation_reasons(value: object) -> None:
    if (
        type(value) is not tuple
        or len(value) > RETRIEVAL_SNAPSHOT_MAX_TRUNCATION_REASONS
    ):
        _invalid("truncation_reasons must be a bounded tuple")
    reasons = cast(tuple[object, ...], value)
    if any(
        type(reason) is not str or reason not in _TRUNCATION_REASONS
        for reason in reasons
    ):
        _invalid("truncation_reasons contains an unsupported reason")
    if len(set(reasons)) != len(reasons):
        _invalid("truncation_reasons must not contain duplicates")
    if tuple(sorted(cast(tuple[str, ...], reasons))) != reasons:
        _invalid("truncation_reasons must use canonical order")


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


def _optional_score(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _score(value, label)


def _score(value: object, label: str) -> float:
    if type(value) not in {int, float}:
        _invalid(f"{label} must be a finite number")
    try:
        normalized = float(cast(int | float, value))
    except (OverflowError, ValueError) as error:
        raise RetrievalSnapshotContractError(
            "TBM_RETRIEVAL_SNAPSHOT_INVALID",
            f"{label} must be a finite number",
        ) from error
    if not math.isfinite(normalized):
        _invalid(f"{label} must be a finite number")
    return 0.0 if normalized == 0.0 else normalized


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
        raise RetrievalSnapshotContractError(
            "TBM_RETRIEVAL_SNAPSHOT_INVALID",
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
        raise RetrievalSnapshotContractError(
            "TBM_RETRIEVAL_SNAPSHOT_INVALID",
            f"{label} must be an RFC 3339 timestamp",
        ) from error
