from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.gate_session_v3 import (
    create_gate_session,
    transition_gate_session,
)
from trace_backed_memory.gate_worker_v3 import (
    GateSessionRecoveryWorker,
    GateSessionRecoveryWorkerError,
)


FINGERPRINT = "sha256:" + "a" * 64


class _Clock:
    def __init__(self, values: Iterable[str]) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


def _create(
    repository: tbm.SQLiteGateSessionRepository,
    *,
    expires_in_seconds: int = 10,
) -> tbm.GateSession:
    return repository.create_or_get(
        session_id="session_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=FINGERPRINT,
        idempotency_key="idempotency_001",
        expires_in_seconds=expires_in_seconds,
    ).session


def _prepare(
    repository: tbm.SQLiteGateSessionRepository,
    created: tbm.GateSession,
) -> tbm.GateSession:
    return repository.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=2,
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )


def test_worker_distinguishes_lease_due_from_session_expiry():
    clock = _Clock(
        (
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:01Z",
            "2026-07-28T00:00:03Z",
            "2026-07-28T00:00:04Z",
            "2026-07-28T00:00:11Z",
            "2026-07-28T00:00:12Z",
        )
    )
    with tbm.SQLiteGateSessionRepository.connect(
        initialize=True,
        clock=clock,
    ) as repository:
        prepared = _prepare(repository, _create(repository))
        worker = GateSessionRecoveryWorker(repository)

        (lease_due,) = worker.run_once()
        assert lease_due.outcome == "recovery_required"
        assert lease_due.current == prepared
        assert repository.history(prepared.session_id) == (
            repository.history(prepared.session_id)[0],
            prepared,
        )

        (expired,) = worker.run_once()
        assert expired.outcome == "expired"
        assert expired.current.status == "expired"
        assert expired.current.terminal_reason == "session_expired"
        assert [item.status for item in repository.history(prepared.session_id)] == [
            "created",
            "prepared",
            "expired",
        ]


def _decided_session() -> tbm.GateSession:
    created = create_gate_session(
        session_id="session_decided",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="client_001",
        trace_id="trace_001",
        run_id="run_001",
        request_fingerprint=FINGERPRINT,
        idempotency_key="idempotency_decided",
        created_at="2026-07-28T00:00:00Z",
        expires_at="2026-07-28T00:10:00Z",
    )
    prepared = transition_gate_session(
        created,
        "prepared",
        expected_version=1,
        updated_at="2026-07-28T00:00:01Z",
        lease_expires_at="2026-07-28T00:01:00Z",
        retrieval_snapshot_id="retrieval_001",
        system_gate_evaluation_id="system_gate_001",
    )
    awaiting = transition_gate_session(
        prepared,
        "awaiting_decision",
        expected_version=2,
        updated_at="2026-07-28T00:00:02Z",
    )
    return transition_gate_session(
        awaiting,
        "decided",
        expected_version=3,
        updated_at="2026-07-28T00:00:03Z",
        semantic_gate_attempt_ids=("attempt_001",),
        decision_id="decision_001",
    )


def test_worker_reports_decided_due_session_without_illegal_transition():
    decided = _decided_session()

    class Repository:
        transition_calls = 0

        def list_due(self, *, limit: int = 100) -> tuple[tbm.GateSession, ...]:
            assert limit == 100
            return (decided,)

        def get(self, session_id: str) -> tbm.GateSession:
            assert session_id == decided.session_id
            return decided

        def transition(self, *args: object, **kwargs: object) -> tbm.GateSession:
            self.transition_calls += 1
            raise AssertionError("decided due state has no legal worker transition")

    repository = Repository()
    (result,) = GateSessionRecoveryWorker(repository).run_once()

    assert result.outcome == "recovery_required"
    assert result.current == decided
    assert repository.transition_calls == 0


def test_worker_classifies_cas_race_as_superseded():
    candidate = _decided_session()
    current = transition_gate_session(
        candidate,
        "finalized",
        expected_version=4,
        updated_at="2026-07-28T00:00:04Z",
        final_memory_revision_ids=("revision_001",),
        injection_artifact_id="artifact_001",
        usage_decision_id="usage_001",
    )

    class Repository:
        def list_due(self, *, limit: int = 100) -> tuple[tbm.GateSession, ...]:
            return (candidate,)

        def get(self, session_id: str) -> tbm.GateSession:
            return current

        def transition(self, *args: object, **kwargs: object) -> tbm.GateSession:
            raise AssertionError("decided candidates are read-only")

    (result,) = GateSessionRecoveryWorker(Repository()).run_once()

    assert result.outcome == "superseded"
    assert result.current == current


def test_worker_rejects_invalid_current_state_with_stable_error():
    candidate = _decided_session()

    class Repository:
        def list_due(self, *, limit: int = 100) -> tuple[tbm.GateSession, ...]:
            return (candidate,)

        def get(self, session_id: str) -> object:
            return None

        def transition(self, *args: object, **kwargs: object) -> tbm.GateSession:
            raise AssertionError("decided candidates are read-only")

    with pytest.raises(GateSessionRecoveryWorkerError) as raised:
        GateSessionRecoveryWorker(Repository()).run_once()  # type: ignore[arg-type]
    assert raised.value.code == "TBM_GATE_WORKER_CURRENT_INVALID"


def test_worker_validates_entire_bounded_candidate_page_before_mutation():
    candidate = _decided_session()

    class Repository:
        transition_calls = 0

        def list_due(self, *, limit: int = 100) -> tuple[object, ...]:
            return (candidate, "invalid")

        def get(self, session_id: str) -> tbm.GateSession:
            return candidate

        def transition(self, *args: object, **kwargs: object) -> tbm.GateSession:
            self.transition_calls += 1
            raise AssertionError("invalid page must fail before mutation")

    repository = Repository()
    with pytest.raises(GateSessionRecoveryWorkerError) as raised:
        GateSessionRecoveryWorker(repository).run_once()  # type: ignore[arg-type]
    assert raised.value.code == "TBM_GATE_WORKER_CANDIDATES_INVALID"
    assert repository.transition_calls == 0

    overflow = replace(
        candidate,
        session_id="session_decided_other",
        idempotency_key="idempotency_decided_other",
    )

    class OverflowRepository(Repository):
        def list_due(self, *, limit: int = 100) -> tuple[object, ...]:
            assert limit == 1
            return (candidate, overflow)

    with pytest.raises(GateSessionRecoveryWorkerError) as overflow_error:
        GateSessionRecoveryWorker(OverflowRepository()).run_once(limit=1)  # type: ignore[arg-type]
    assert overflow_error.value.code == "TBM_GATE_WORKER_CANDIDATES_INVALID"


@pytest.mark.parametrize("limit", (0, -1, 10_001, True))
def test_worker_rejects_invalid_limits(limit: object):
    class Repository:
        def list_due(self, *, limit: int = 100) -> tuple[tbm.GateSession, ...]:
            pytest.fail("invalid limit must fail before discovery")

    with pytest.raises(GateSessionRecoveryWorkerError) as raised:
        GateSessionRecoveryWorker(Repository()).run_once(  # type: ignore[arg-type]
            limit=limit,  # type: ignore[arg-type]
        )
    assert raised.value.code == "TBM_GATE_WORKER_LIMIT_INVALID"


def test_gate_worker_public_exports_are_intentional():
    assert tbm.GateSessionRecoveryWorker is GateSessionRecoveryWorker
    assert "GateSessionRecoveryWorker" in tbm.__all__
