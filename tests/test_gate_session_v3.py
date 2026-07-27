from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import re

import pytest

import trace_backed_memory as tbm


ROOT = Path(__file__).resolve().parents[1]
HASH = "sha256:" + "a" * 64


def _created(**overrides) -> tbm.GateSession:
    values = {
        "session_id": "gate_session_001",
        "tenant_id": "tenant_001",
        "repository_id": "repository_001",
        "principal_id": "principal_001",
        "agent_client_id": "agent_client_001",
        "trace_id": "trace_001",
        "run_id": "run_001",
        "request_fingerprint": HASH,
        "idempotency_key": "request-001",
        "created_at": "2026-07-27T00:00:00Z",
        "expires_at": "2026-07-27T01:00:00Z",
    }
    values.update(overrides)
    return tbm.create_gate_session(**values)


def _prepared() -> tbm.GateSession:
    return tbm.transition_gate_session(
        _created(),
        "prepared",
        expected_version=1,
        updated_at="2026-07-27T00:01:00Z",
        lease_expires_at="2026-07-27T00:20:00Z",
        retrieval_snapshot_id="retrieval_snapshot_001",
        system_gate_evaluation_id="system_gate_001",
    )


def _awaiting() -> tbm.GateSession:
    return tbm.transition_gate_session(
        _prepared(),
        "awaiting_decision",
        expected_version=2,
        updated_at="2026-07-27T00:02:00Z",
    )


def _decided() -> tbm.GateSession:
    return tbm.transition_gate_session(
        _awaiting(),
        "decided",
        expected_version=3,
        updated_at="2026-07-27T00:03:00Z",
        semantic_gate_attempt_ids=("semantic_gate_attempt_001",),
        decision_id="decision_001",
    )


def _finalized() -> tbm.GateSession:
    return tbm.transition_gate_session(
        _decided(),
        "finalized",
        expected_version=4,
        updated_at="2026-07-27T00:04:00Z",
        final_memory_revision_ids=(
            "memory_revision_001",
            "memory_revision_002",
        ),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_decision_001",
    )


def _executing() -> tbm.GateSession:
    return tbm.transition_gate_session(
        _finalized(),
        "executing",
        expected_version=5,
        updated_at="2026-07-27T00:05:00Z",
    )


def test_gate_session_full_lifecycle_is_immutable_and_versioned():
    created = _created()
    prepared = _prepared()
    awaiting = _awaiting()
    decided = _decided()
    finalized = _finalized()
    executing = _executing()
    completed = tbm.transition_gate_session(
        executing,
        "completed",
        expected_version=6,
        updated_at="2026-07-27T00:06:00Z",
        run_outcome_id="run_outcome_001",
    )

    assert [
        item.status
        for item in (
            created,
            prepared,
            awaiting,
            decided,
            finalized,
            executing,
            completed,
        )
    ] == [
        "created",
        "prepared",
        "awaiting_decision",
        "decided",
        "finalized",
        "executing",
        "completed",
    ]
    assert [item.version for item in (
        created,
        prepared,
        awaiting,
        decided,
        finalized,
        executing,
        completed,
    )] == list(range(1, 8))
    assert completed.terminal is True
    assert completed.lease_expires_at is None
    assert completed.run_outcome_id == "run_outcome_001"
    assert created.status == "created"
    with pytest.raises(FrozenInstanceError):
        completed.status = "created"  # type: ignore[misc]


def test_gate_session_cancel_expire_and_abandon_are_terminal():
    canceled = tbm.transition_gate_session(
        _created(),
        "canceled",
        expected_version=1,
        updated_at="2026-07-27T00:01:00Z",
        terminal_reason="caller abandoned the request",
    )
    expired = tbm.transition_gate_session(
        _awaiting(),
        "expired",
        expected_version=3,
        updated_at="2026-07-27T01:00:01Z",
        terminal_reason="decision deadline elapsed",
    )
    abandoned = tbm.transition_gate_session(
        _executing(),
        "abandoned",
        expected_version=6,
        updated_at="2026-07-27T00:06:00Z",
        terminal_reason="execution worker lease was lost",
    )

    assert canceled.terminal is True
    assert expired.terminal is True
    assert expired.retrieval_snapshot_id == "retrieval_snapshot_001"
    assert abandoned.terminal is True
    assert abandoned.injection_artifact_id == "artifact_001"
    for terminal in (canceled, expired, abandoned):
        with pytest.raises(
            tbm.GateSessionContractError,
            match="cannot transition",
        ) as raised:
            tbm.transition_gate_session(
                terminal,
                "prepared",
                expected_version=terminal.version,
                updated_at="2026-07-27T01:10:00Z",
            )
        assert raised.value.code == "TBM_GATE_SESSION_INVALID_TRANSITION"


def test_gate_session_lease_renewal_requires_active_fresh_revision():
    prepared = _prepared()
    renewed = tbm.renew_gate_session_lease(
        prepared,
        expected_version=2,
        updated_at="2026-07-27T00:10:00Z",
        lease_expires_at="2026-07-27T00:30:00Z",
    )

    assert renewed.version == 3
    assert renewed.status == "prepared"
    assert renewed.lease_expires_at == "2026-07-27T00:30:00Z"
    with pytest.raises(tbm.GateSessionContractError) as stale:
        tbm.renew_gate_session_lease(
            renewed,
            expected_version=2,
            updated_at="2026-07-27T00:11:00Z",
            lease_expires_at="2026-07-27T00:40:00Z",
        )
    assert stale.value.code == "TBM_GATE_SESSION_STALE_VERSION"
    with pytest.raises(
        tbm.GateSessionContractError,
        match="cannot renew",
    ):
        tbm.renew_gate_session_lease(
            _created(),
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
            lease_expires_at="2026-07-27T00:10:00Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="before the current lease expires",
    ):
        tbm.renew_gate_session_lease(
            prepared,
            expected_version=2,
            updated_at="2026-07-27T00:20:00Z",
            lease_expires_at="2026-07-27T00:30:00Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="must extend",
    ):
        tbm.renew_gate_session_lease(
            prepared,
            expected_version=2,
            updated_at="2026-07-27T00:10:00Z",
            lease_expires_at="2026-07-27T00:15:00Z",
        )


def test_gate_session_rejects_stale_and_invalid_transitions():
    created = _created()
    with pytest.raises(tbm.GateSessionContractError) as stale:
        tbm.transition_gate_session(
            created,
            "prepared",
            expected_version=2,
            updated_at="2026-07-27T00:01:00Z",
        )
    assert stale.value.code == "TBM_GATE_SESSION_STALE_VERSION"

    with pytest.raises(tbm.GateSessionContractError) as invalid:
        tbm.transition_gate_session(
            created,
            "decided",
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
        )
    assert invalid.value.code == "TBM_GATE_SESSION_INVALID_TRANSITION"

    with pytest.raises(
        tbm.GateSessionContractError,
        match="later than current",
    ):
        tbm.transition_gate_session(
            created,
            "canceled",
            expected_version=1,
            updated_at=created.updated_at,
            terminal_reason="caller canceled",
        )

    with pytest.raises(
        tbm.GateSessionContractError,
        match="canceled transition cannot set decision_id",
    ):
        tbm.transition_gate_session(
            created,
            "canceled",
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
            decision_id="forged_decision",
            terminal_reason="caller canceled",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="prepared transition requires lease_expires_at",
    ):
        tbm.transition_gate_session(
            created,
            "prepared",
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
            retrieval_snapshot_id="snapshot",
            system_gate_evaluation_id="system_gate",
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contract_version": "wrong"}, "contract_version"),
        ({"request_fingerprint": "not-a-digest"}, "SHA-256"),
        ({"idempotency_key": " "}, "idempotency_key"),
        ({"version": True}, "positive integer"),
        ({"status": "unknown"}, "supported GateSession status"),
        ({"expires_at": "2026-07-27T00:00:00Z"}, "later than created"),
        ({"lease_expires_at": "2026-07-27T00:10:00Z"}, "created session"),
        ({"terminal_reason": "not terminal"}, "cannot contain"),
        ({"retrieval_snapshot_id": "snapshot"}, "recorded together"),
        ({"decision_id": "decision"}, "prepared retrieval"),
        (
            {"semantic_gate_attempt_ids": ("attempt",)},
            "require decision_id",
        ),
        ({"injection_artifact_id": "artifact"}, "recorded together"),
        ({"run_outcome_id": "outcome"}, "finalized artifacts"),
    ],
)
def test_gate_session_direct_construction_is_strict(changes, message):
    with pytest.raises(tbm.GateSessionContractError, match=message):
        replace(_created(), **changes)


@pytest.mark.parametrize(
    ("session_factory", "changes", "message"),
    [
        (
            _created,
            {"updated_at": "2026-07-26T23:59:00Z"},
            "must not precede",
        ),
        (
            _prepared,
            {"updated_at": "2026-07-27T01:01:00Z"},
            "nonterminal updated_at",
        ),
        (
            _prepared,
            {"lease_expires_at": "2026-07-27T00:01:00Z"},
            "lease_expires_at must be later",
        ),
        (
            _prepared,
            {"lease_expires_at": None},
            "requires an active lease",
        ),
        (
            _created,
            {"final_memory_revision_ids": ("memory_revision",)},
            "require finalized artifacts",
        ),
        (
            _created,
            {
                "retrieval_snapshot_id": "snapshot",
                "system_gate_evaluation_id": "system_gate",
                "injection_artifact_id": "artifact",
                "usage_decision_id": "usage",
            },
            "finalized artifacts require decision_id",
        ),
        (
            _created,
            {"session_id": "x" * 129},
            "at most 128",
        ),
        (
            _created,
            {"session_id": "\ud800"},
            "valid Unicode",
        ),
        (
            _created,
            {"semantic_gate_attempt_ids": ("duplicate", "duplicate")},
            "must not contain duplicates",
        ),
        (
            _created,
            {"semantic_gate_attempt_ids": ["not", "tuple"]},
            "must be a tuple",
        ),
    ],
)
def test_gate_session_rejects_invalid_time_identity_and_evidence_shapes(
    session_factory,
    changes,
    message,
):
    with pytest.raises(tbm.GateSessionContractError, match=message):
        replace(session_factory(), **changes)


def test_gate_session_rejects_inconsistent_lifecycle_shapes():
    with pytest.raises(
        tbm.GateSessionContractError,
        match="prepared session",
    ):
        replace(
            _prepared(),
            retrieval_snapshot_id=None,
            system_gate_evaluation_id=None,
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="require decision_id",
    ):
        replace(_decided(), decision_id=None)
    with pytest.raises(
        tbm.GateSessionContractError,
        match="finalized session",
    ):
        replace(
            _finalized(),
            injection_artifact_id=None,
            usage_decision_id=None,
            final_memory_revision_ids=(),
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="completed session",
    ):
        replace(
            tbm.transition_gate_session(
                _executing(),
                "completed",
                expected_version=6,
                updated_at="2026-07-27T00:06:00Z",
                run_outcome_id="outcome",
            ),
            run_outcome_id=None,
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="requires terminal_reason",
    ):
        replace(
            tbm.transition_gate_session(
                _created(),
                "canceled",
                expected_version=1,
                updated_at="2026-07-27T00:01:00Z",
                terminal_reason="canceled",
            ),
            terminal_reason=None,
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="canceled session cannot contain",
    ):
        replace(
            tbm.transition_gate_session(
                _created(),
                "canceled",
                expected_version=1,
                updated_at="2026-07-27T00:01:00Z",
                terminal_reason="canceled",
            ),
            retrieval_snapshot_id="snapshot",
            system_gate_evaluation_id="system_gate",
            decision_id="decision",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="expired session requires prepared retrieval evidence",
    ):
        replace(
            tbm.transition_gate_session(
                _awaiting(),
                "expired",
                expected_version=3,
                updated_at="2026-07-27T01:00:00Z",
                terminal_reason="expired",
            ),
            retrieval_snapshot_id=None,
            system_gate_evaluation_id=None,
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="expired session updated_at cannot precede expires_at",
    ):
        replace(
            _awaiting(),
            status="expired",
            version=4,
            updated_at="2026-07-27T00:30:00Z",
            lease_expires_at=None,
            terminal_reason="premature expiry",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="decided session requires",
    ):
        replace(
            _decided(),
            decision_id=None,
            semantic_gate_attempt_ids=(),
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="abandoned session requires finalized",
    ):
        replace(
            tbm.transition_gate_session(
                _executing(),
                "abandoned",
                expected_version=6,
                updated_at="2026-07-27T00:06:00Z",
                terminal_reason="abandoned",
            ),
            final_memory_revision_ids=(),
            injection_artifact_id=None,
            usage_decision_id=None,
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="cannot retain an active lease",
    ):
        replace(
            tbm.transition_gate_session(
                _created(),
                "canceled",
                expected_version=1,
                updated_at="2026-07-27T00:01:00Z",
                terminal_reason="canceled",
            ),
            lease_expires_at="2026-07-27T00:20:00Z",
        )


def test_gate_session_transition_rejects_invalid_operational_inputs():
    with pytest.raises(
        tbm.GateSessionContractError,
        match="session must be exactly",
    ):
        tbm.transition_gate_session(  # type: ignore[arg-type]
            object(),
            "prepared",
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="supported GateSession status",
    ):
        tbm.transition_gate_session(
            _created(),
            "unknown",  # type: ignore[arg-type]
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="updated_at must be",
    ):
        tbm.transition_gate_session(
            _created(),
            "canceled",
            expected_version=1,
            updated_at="not-a-time",
            terminal_reason="canceled",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="not be later than expires_at",
    ):
        tbm.transition_gate_session(
            _prepared(),
            "awaiting_decision",
            expected_version=2,
            updated_at="2026-07-27T01:00:01Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="cannot precede expires_at",
    ):
        tbm.transition_gate_session(
            _awaiting(),
            "expired",
            expected_version=3,
            updated_at="2026-07-27T00:30:00Z",
            terminal_reason="premature expiry",
        )


def test_gate_session_lease_renewal_rejects_invalid_operational_inputs():
    with pytest.raises(
        tbm.GateSessionContractError,
        match="session must be exactly",
    ):
        tbm.renew_gate_session_lease(  # type: ignore[arg-type]
            object(),
            expected_version=1,
            updated_at="2026-07-27T00:01:00Z",
            lease_expires_at="2026-07-27T00:10:00Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="timestamps must be",
    ):
        tbm.renew_gate_session_lease(
            _prepared(),
            expected_version=2,
            updated_at="not-a-time",
            lease_expires_at="2026-07-27T00:30:00Z",
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="updated_at must advance",
    ):
        tbm.renew_gate_session_lease(
            _prepared(),
            expected_version=2,
            updated_at="2026-07-27T00:01:00Z",
            lease_expires_at="2026-07-27T00:30:00Z",
        )


def test_gate_session_serialization_is_canonical_and_strict():
    session = _awaiting()
    serialized = tbm.dumps_gate_session(session)
    payload = json.loads(serialized)

    assert tbm.loads_gate_session(serialized) == session
    assert tbm.loads_gate_session(serialized.encode("utf-8")) == session
    assert tbm.parse_gate_session(payload) == session
    assert serialized == json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    offset = replace(
        _created(),
        created_at="2026-07-27T08:00:00+08:00",
        updated_at="2026-07-27T08:00:00+08:00",
        expires_at="2026-07-27T09:00:00+08:00",
    )
    assert offset.to_dict()["created_at"] == "2026-07-27T00:00:00Z"
    assert tbm.loads_gate_session(tbm.dumps_gate_session(offset)).to_dict() == (
        offset.to_dict()
    )

    maximum_key = _created(
        idempotency_key="k" * tbm.METADATA_VALUE_MAX_CHARS
    )
    assert tbm.loads_gate_session(
        tbm.dumps_gate_session(maximum_key)
    ) == maximum_key
    with pytest.raises(
        tbm.GateSessionContractError,
        match="idempotency_key",
    ):
        _created(
            idempotency_key=(
                "k" * (tbm.METADATA_VALUE_MAX_CHARS + 1)
            )
        )


def test_gate_session_external_json_is_bounded_and_duplicate_rejecting():
    with pytest.raises(
        tbm.GateSessionContractError,
        match="duplicate object key",
    ) as duplicate:
        tbm.loads_gate_session(
            '{"session_id":"one","session_id":"two"}'
        )
    assert duplicate.value.code == "TBM_GATE_SESSION_INVALID_JSON"

    with pytest.raises(
        tbm.GateSessionContractError,
        match="non-finite",
    ):
        tbm.loads_gate_session('{"value": NaN}')
    with pytest.raises(
        tbm.GateSessionContractError,
        match="invalid UTF-8",
    ):
        tbm.loads_gate_session(b"\xff")
    with pytest.raises(
        tbm.GateSessionContractError,
        match="maximum depth",
    ):
        tbm.loads_gate_session(
            '{"value":' + "[" * 34 + "0" + "]" * 34 + "}"
        )
    with pytest.raises(
        tbm.GateSessionContractError,
        match="maximum size",
    ):
        tbm.loads_gate_session(
            '{"value":"' + "x" * tbm.GATE_SESSION_MAX_BYTES + '"}'
        )


def test_gate_session_parser_rejects_unknown_missing_and_wrong_types():
    payload = _created().to_dict()
    payload["unknown"] = True
    with pytest.raises(
        tbm.GateSessionContractError,
        match="unknown field",
    ):
        tbm.parse_gate_session(payload)

    payload = _created().to_dict()
    del payload["run_id"]
    with pytest.raises(
        tbm.GateSessionContractError,
        match="missing field",
    ):
        tbm.parse_gate_session(payload)

    payload = _created().to_dict()
    payload["version"] = False
    with pytest.raises(
        tbm.GateSessionContractError,
        match="must be an integer",
    ):
        tbm.parse_gate_session(payload)

    payload = _created().to_dict()
    payload["semantic_gate_attempt_ids"] = "attempt"
    with pytest.raises(
        tbm.GateSessionContractError,
        match="array of strings",
    ):
        tbm.parse_gate_session(payload)

    payload = _created().to_dict()
    payload["terminal_reason"] = 7
    with pytest.raises(
        tbm.GateSessionContractError,
        match="string or null",
    ):
        tbm.parse_gate_session(payload)

    payload = _created().to_dict()
    payload["session_id"] = 7
    with pytest.raises(
        tbm.GateSessionContractError,
        match="must be a string",
    ):
        tbm.parse_gate_session(payload)

    with pytest.raises(
        tbm.GateSessionContractError,
        match="must be a JSON object",
    ):
        tbm.parse_gate_session([])  # type: ignore[arg-type]
    with pytest.raises(
        tbm.GateSessionContractError,
        match="must contain one object",
    ):
        tbm.loads_gate_session("[]")
    with pytest.raises(
        tbm.GateSessionContractError,
        match="must be str or bytes",
    ):
        tbm.loads_gate_session(7)  # type: ignore[arg-type]
    with pytest.raises(
        tbm.GateSessionContractError,
        match="session must be exactly",
    ):
        tbm.dumps_gate_session(object())  # type: ignore[arg-type]


def test_gate_session_schema_example_and_public_exports():
    schema = json.loads(
        (ROOT / "schemas" / "gate_session_v3.schema.json").read_text(
            encoding="utf-8"
        )
    )
    example = json.loads(
        (ROOT / "examples" / "gate_session_v3.example.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["additionalProperties"] is False
    assert (
        schema["properties"]["contract_version"]["const"]
        == tbm.GATE_SESSION_CONTRACT_VERSION
    )
    assert set(schema["required"]) == set(example)
    assert (
        schema["$defs"]["metadata"]["maxLength"]
        == tbm.METADATA_VALUE_MAX_CHARS
    )
    assert re.fullmatch(
        schema["$defs"]["timestamp"]["pattern"].removeprefix(
            "^"
        ).removesuffix("$"),
        "2026-07-27T00:00:00+99:99",
    ) is None
    assert len(schema["allOf"]) == 9
    assert tbm.parse_gate_session(example).to_dict() == example
    for name in (
        "GATE_SESSION_CONTRACT_VERSION",
        "GateSession",
        "GateSessionContractError",
        "GateSessionStatus",
        "create_gate_session",
        "dumps_gate_session",
        "loads_gate_session",
        "parse_gate_session",
        "renew_gate_session_lease",
        "transition_gate_session",
    ):
        assert name in tbm.__all__
