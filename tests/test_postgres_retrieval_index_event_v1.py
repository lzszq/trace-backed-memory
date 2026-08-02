from tests.postgres_support import PostgresCluster
from tests.test_postgres_event_ledger_v1 import _install
from tests.test_retrieval_index_event_v1 import (
    TRUSTED_EMBEDDINGS,
    TRUSTED_VERIFIERS,
    _record_access,
    _records,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.retrieval_index_event_v1 import (
    append_retrieval_index_records,
    rebuild_retrieval_index_from_ledger,
)


def test_postgres_event_ledger_rebuilds_retrieval_index_like_sqlite(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _, records = _records()
    ledger = None
    try:
        for stored in records:
            if ledger is not None:
                ledger.close()
            ledger = PostgresEventLedgerV1.connect(
                _record_access(stored),
                **postgres_cluster.connection_kwargs(),
            )
            result = append_retrieval_index_records(
                ledger,
                (stored,),
                recorded_at="2026-07-27T00:10:00Z",
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
                trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
            )
        snapshot = rebuild_retrieval_index_from_ledger(
            ledger,
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            trusted_embedding_provider_models=TRUSTED_EMBEDDINGS,
        )
        assert snapshot.projection == result.projection
        assert snapshot.source_event_count == 4
        assert snapshot.load_active_head().stale
    finally:
        if ledger is not None:
            ledger.close()
