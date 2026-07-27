from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.retrieval_v3 import (
    RETRIEVAL_SNAPSHOT_JSON_MAX_BYTES,
    IndexVersion,
    RetrievalHit,
    RetrievalSnapshot,
    RetrievalSnapshotContractError,
    build_retrieval_snapshot,
    dumps_retrieval_snapshot,
    loads_retrieval_snapshot,
    parse_retrieval_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


def _hit(
    *,
    rank: int = 1,
    memory_id: str = "memory_001",
    suffix: str = "a",
) -> RetrievalHit:
    return RetrievalHit(
        memory_id=memory_id,
        memory_revision_id="memory_revision_sha256_" + suffix * 64,
        candidate_sha256="sha256:" + suffix * 64,
        rank=rank,
        metadata_score=1.0,
        lexical_score=0.75,
        semantic_score=0.9,
        evidence_graph_score=None,
        fused_score=0.84,
        selected_stages=("metadata", "lexical", "semantic", "fusion"),
    )


def _snapshot(
    *,
    hits: tuple[RetrievalHit, ...] | None = None,
    total_candidates: int | None = None,
    truncated: bool = True,
) -> RetrievalSnapshot:
    hits = (_hit(),) if hits is None else hits
    total_candidates = total_candidates if total_candidates is not None else 3
    return build_retrieval_snapshot(
        session_id="session_001",
        request_id="request_001",
        trace_id="trace_001",
        run_id="run_001",
        authorization_event_id="authz_sha256_" + "d" * 64,
        context_sha256="sha256:" + "e" * 64,
        query_sha256="sha256:" + "f" * 64,
        retrieval_mode="hybrid",
        retriever_id="hybrid_retriever",
        retriever_version="1.0",
        index_versions=(
            IndexVersion(
                "semantic",
                "semantic_main",
                "embeddings_v7",
                "sha256:" + "2" * 64,
            ),
            IndexVersion(
                "metadata",
                "memory_catalog",
                "catalog_v11",
                "sha256:" + "1" * 64,
            ),
            IndexVersion(
                "lexical",
                "lexical_main",
                "fts_v4",
                "sha256:" + "3" * 64,
            ),
        ),
        hits=hits,
        total_candidates=total_candidates,
        top_k=10,
        truncated=truncated,
        truncation_reasons=("minimum_score", "top_k") if truncated else (),
        created_at="2026-07-27T08:00:00Z",
    )


def test_retrieval_snapshot_round_trips_canonical_json():
    snapshot = _snapshot()
    document = dumps_retrieval_snapshot(snapshot)

    assert loads_retrieval_snapshot(document) == snapshot
    assert loads_retrieval_snapshot(document.encode()) == snapshot
    assert document == json.dumps(
        snapshot.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert tuple(item.index_kind for item in snapshot.index_versions) == (
        "metadata",
        "lexical",
        "semantic",
    )


def test_snapshot_hash_detects_immutable_content_change():
    with pytest.raises(
        RetrievalSnapshotContractError,
        match="canonical retrieval content",
    ) as captured:
        replace(_snapshot(), retriever_version="2.0")

    assert captured.value.code == "TBM_RETRIEVAL_SNAPSHOT_HASH_MISMATCH"


def test_builder_canonicalizes_index_hit_reason_and_time_order():
    second = _hit(rank=2, memory_id="memory_002", suffix="b")
    first = _hit()
    snapshot = _snapshot(hits=(second, first))
    rebuilt = build_retrieval_snapshot(
        **{
            **{
                key: value
                for key, value in snapshot.__dict__.items()
                if key not in {"snapshot_id", "contract_version"}
            },
            "index_versions": tuple(reversed(snapshot.index_versions)),
            "hits": tuple(reversed(snapshot.hits)),
            "truncation_reasons": ("top_k", "minimum_score"),
            "created_at": "2026-07-27T16:00:00+08:00",
        }
    )

    assert tuple(hit.rank for hit in rebuilt.hits) == (1, 2)
    assert rebuilt.truncation_reasons == ("minimum_score", "top_k")
    assert rebuilt.created_at == "2026-07-27T08:00:00Z"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "tbm.retrieval-snapshot.v4"}, "contract_version"),
        ({"snapshot_id": "bad"}, "snapshot_id"),
        ({"session_id": " "}, "session_id"),
        ({"authorization_event_id": "bad"}, "authorization_event_id"),
        ({"context_sha256": "bad"}, "context_sha256"),
        ({"query_sha256": "bad"}, "query_sha256"),
        ({"retrieval_mode": "vector"}, "retrieval_mode"),
        ({"index_versions": ()}, "index_versions"),
        ({"total_candidates": 0}, "total_candidates"),
        ({"top_k": 0}, "top_k"),
        ({"truncated": "yes"}, "boolean"),
        ({"truncation_reasons": ()}, "agree"),
    ],
)
def test_snapshot_rejects_invalid_fields_before_hash_check(
    changes: dict[str, object],
    message: str,
):
    with pytest.raises(RetrievalSnapshotContractError, match=message):
        replace(_snapshot(), **changes)


def test_unreported_omissions_are_rejected():
    with pytest.raises(
        RetrievalSnapshotContractError,
        match="omitted candidates",
    ):
        _snapshot(truncated=False)


def test_hit_requires_contiguous_unique_order_and_candidate_identity():
    first = _hit()
    second = _hit(rank=2, memory_id="memory_002", suffix="b")

    with pytest.raises(RetrievalSnapshotContractError, match="contiguous"):
        replace(_snapshot(), hits=(second, first))
    with pytest.raises(RetrievalSnapshotContractError, match="memory revision"):
        replace(_snapshot(), hits=(first, replace(second, memory_revision_id=first.memory_revision_id)))
    with pytest.raises(RetrievalSnapshotContractError, match="candidate hash"):
        replace(_snapshot(), hits=(first, replace(second, candidate_sha256=first.candidate_sha256)))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"memory_id": " "}, "memory_id"),
        ({"memory_revision_id": "bad"}, "memory_revision_id"),
        ({"candidate_sha256": "bad"}, "candidate_sha256"),
        ({"rank": 0}, "rank"),
        ({"semantic_score": float("nan")}, "semantic_score"),
        ({"fused_score": "high"}, "fused_score"),
        ({"selected_stages": ()}, "nonempty"),
        ({"selected_stages": ("fusion", "metadata")}, "pipeline order"),
        ({"selected_stages": ("metadata", "metadata")}, "duplicates"),
    ],
)
def test_retrieval_hit_rejects_invalid_fields(
    changes: dict[str, object],
    message: str,
):
    with pytest.raises(RetrievalSnapshotContractError, match=message):
        replace(_hit(), **changes)


def test_index_version_rejects_invalid_identity_and_digest():
    with pytest.raises(RetrievalSnapshotContractError, match="index_kind"):
        IndexVersion("vector", "index", "v1", "sha256:" + "a" * 64)  # type: ignore[arg-type]
    with pytest.raises(RetrievalSnapshotContractError, match="index_id"):
        IndexVersion("metadata", " ", "v1", "sha256:" + "a" * 64)
    with pytest.raises(RetrievalSnapshotContractError, match="content_sha256"):
        IndexVersion("metadata", "index", "v1", "bad")


def test_scores_have_one_canonical_numeric_identity():
    integer_hit = replace(_hit(), metadata_score=1, semantic_score=-0.0)
    float_hit = replace(_hit(), metadata_score=1.0, semantic_score=0.0)

    assert integer_hit == float_hit
    assert integer_hit.to_dict() == float_hit.to_dict()
    assert _snapshot(hits=(integer_hit,)).snapshot_id == _snapshot(
        hits=(float_hit,)
    ).snapshot_id


def test_selected_stages_scores_mode_and_indexes_must_agree():
    with pytest.raises(RetrievalSnapshotContractError, match="recorded stage"):
        replace(_hit(), semantic_score=None)
    with pytest.raises(RetrievalSnapshotContractError, match="include fusion"):
        replace(
            _hit(),
            selected_stages=("metadata", "lexical", "semantic"),
        )

    snapshot = _snapshot()
    with pytest.raises(RetrievalSnapshotContractError, match="index version"):
        replace(snapshot, index_versions=(snapshot.index_versions[0],))
    with pytest.raises(RetrievalSnapshotContractError, match="two ranking"):
        replace(
            snapshot,
            hits=(
                replace(
                    _hit(),
                    lexical_score=None,
                    semantic_score=None,
                    selected_stages=("metadata", "fusion"),
                ),
            ),
        )
    with pytest.raises(RetrievalSnapshotContractError, match="retrieval_mode"):
        replace(snapshot, retrieval_mode="evidence_graph")


def test_empty_result_still_binds_mode_to_index_versions():
    snapshot = _snapshot(hits=(), total_candidates=0, truncated=False)

    with pytest.raises(RetrievalSnapshotContractError, match="matching index"):
        replace(
            snapshot,
            retrieval_mode="semantic",
            index_versions=(snapshot.index_versions[0],),
        )
    with pytest.raises(RetrievalSnapshotContractError, match="two index"):
        replace(
            snapshot,
            index_versions=(snapshot.index_versions[0],),
        )


def test_total_candidate_count_is_bounded():
    with pytest.raises(RetrievalSnapshotContractError, match="bounded"):
        replace(_snapshot(), total_candidates=1_000_001)


def test_parser_rejects_duplicate_indexes_and_malformed_nested_records():
    payload = _snapshot().to_dict()
    payload["index_versions"] = [
        payload["index_versions"][0],  # type: ignore[index]
        payload["index_versions"][0],  # type: ignore[index]
    ]
    with pytest.raises(RetrievalSnapshotContractError, match="duplicate"):
        parse_retrieval_snapshot(payload)

    for field in ("index_versions", "hits"):
        payload = _snapshot().to_dict()
        payload[field] = {}
        with pytest.raises(RetrievalSnapshotContractError, match="array"):
            parse_retrieval_snapshot(payload)


def test_parser_rejects_malformed_stages_reasons_and_object_shape():
    payload = _snapshot().to_dict()
    hit = dict(payload["hits"][0])  # type: ignore[index]
    hit["selected_stages"] = "semantic"
    payload["hits"] = [hit]
    with pytest.raises(RetrievalSnapshotContractError, match="selected_stages"):
        parse_retrieval_snapshot(payload)

    payload = _snapshot().to_dict()
    payload["truncation_reasons"] = ["unknown"]
    with pytest.raises(RetrievalSnapshotContractError, match="unsupported reason"):
        parse_retrieval_snapshot(payload)

    payload = _snapshot().to_dict()
    payload["extra"] = True
    with pytest.raises(RetrievalSnapshotContractError, match="fields"):
        parse_retrieval_snapshot(payload)


@pytest.mark.parametrize(
    "document",
    [
        '{"snapshot_id":"first","snapshot_id":"second"}',
        '{"score":NaN}',
        '{"nested":' * 33 + "null" + "}" * 33,
    ],
)
def test_snapshot_json_rejects_duplicate_nonfinite_and_deep_input(
    document: str,
):
    with pytest.raises(RetrievalSnapshotContractError) as captured:
        loads_retrieval_snapshot(document)
    assert captured.value.code == "TBM_RETRIEVAL_SNAPSHOT_INVALID_JSON"


def test_snapshot_json_rejects_invalid_utf8_size_type_and_shape():
    for document in (
        b"\xff",
        "x" * (RETRIEVAL_SNAPSHOT_JSON_MAX_BYTES + 1),
        1,
    ):
        with pytest.raises(RetrievalSnapshotContractError) as captured:
            loads_retrieval_snapshot(document)  # type: ignore[arg-type]
        assert captured.value.code == "TBM_RETRIEVAL_SNAPSHOT_INVALID_JSON"

    with pytest.raises(RetrievalSnapshotContractError, match="object"):
        loads_retrieval_snapshot("[]")


def test_serializer_and_builder_reject_wrong_container_types():
    with pytest.raises(RetrievalSnapshotContractError, match="snapshot"):
        dumps_retrieval_snapshot(None)  # type: ignore[arg-type]

    kwargs = {
        key: value
        for key, value in _snapshot().__dict__.items()
        if key not in {"snapshot_id", "contract_version"}
    }
    for field, value, message in (
        ("index_versions", [], "IndexVersion"),
        ("hits", [], "RetrievalHit"),
        ("truncation_reasons", [], "tuple"),
    ):
        invalid = dict(kwargs)
        invalid[field] = value
        with pytest.raises(RetrievalSnapshotContractError, match=message):
            build_retrieval_snapshot(**invalid)  # type: ignore[arg-type]


def test_schema_example_and_public_exports_match_runtime_contract():
    example = json.loads(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT / "schemas" / "retrieval_snapshot_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert parse_retrieval_snapshot(example).to_dict() == example
    assert set(schema["required"]) == set(example)
    assert schema["additionalProperties"] is False
    assert tbm.RetrievalSnapshot is RetrievalSnapshot
    assert "RetrievalSnapshot" in tbm.__all__
