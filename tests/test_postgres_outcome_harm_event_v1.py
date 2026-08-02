from tests.postgres_support import PostgresCluster
from tests.test_outcome_harm_event_v1 import (
    TRUSTED_VERIFIERS,
    _context_access,
    _records,
    _source_access,
    _source_events,
)
from tests.test_postgres_event_ledger_v1 import _install
from trace_backed_memory.outcome_harm_event_v1 import (
    append_outcome_evaluation_contexts,
    build_outcome_harm_policy,
    rebuild_outcome_harm_from_ledger,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1


def test_postgres_event_ledger_rebuilds_outcome_harm_like_sqlite(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    outcome, session, association, causal, stored = _records()
    source_events, source_idempotency = _source_events(
        outcome, session, association, causal
    )
    source_ledger = PostgresEventLedgerV1.connect(
        _source_access(), **postgres_cluster.connection_kwargs()
    )
    try:
        source_ledger.append(
            source_events[0].stream_id,
            0,
            source_events,
            source_idempotency,
        )
    finally:
        source_ledger.close()
    ledger = PostgresEventLedgerV1.connect(
        _context_access(stored.decision),
        **postgres_cluster.connection_kwargs(),
    )
    try:
        result = append_outcome_evaluation_contexts(
            ledger,
            (stored,),
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            recorded_at="2026-07-29T01:00:01Z",
        )
        snapshot = rebuild_outcome_harm_from_ledger(
            ledger,
            policy=build_outcome_harm_policy(),
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        assert snapshot == result.snapshot
        assert snapshot.source_event_count == 4
        assert len(snapshot.projection.harmful_memory_signals) == 1
        assert len(snapshot.projection.suspension_recommendations) == 1
    finally:
        ledger.close()
