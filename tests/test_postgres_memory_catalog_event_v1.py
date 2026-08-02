from __future__ import annotations

from tests.postgres_support import PostgresCluster
from tests.test_memory_catalog_event_v1 import (
    TRUSTED_VERIFIERS,
    _access,
    _records_and_publication,
)
from tests.test_postgres_event_ledger_v1 import _install
from trace_backed_memory.memory_catalog_event_v1 import (
    MemoryCatalogEvidenceRecord,
    append_memory_catalog_records,
    rebuild_memory_catalog_from_ledger,
)
from trace_backed_memory.memory_publication_v3 import (
    StoredMemoryRevisionActivationPublication,
    StoredMemoryRevisionApprovalPublication,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1


def _actor(record) -> str:
    if hasattr(record, "proposed_by"):
        return record.proposed_by
    if isinstance(record, MemoryCatalogEvidenceRecord):
        return record.actor_id
    if hasattr(record, "reviewed_by"):
        return record.reviewed_by
    if isinstance(record, StoredMemoryRevisionApprovalPublication):
        return record.approval.approved_by
    if isinstance(record, StoredMemoryRevisionActivationPublication):
        return record.activation.activated_by
    raise AssertionError("unsupported MemoryCatalog test record")


def test_postgres_event_ledger_rebuilds_memory_catalog_like_sqlite(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    records, authorization_ids = _records_and_publication()
    last_ledger = None
    try:
        result = None
        for record, authorization_id in zip(
            records, authorization_ids, strict=True
        ):
            ledger = PostgresEventLedgerV1.connect(
                _access(_actor(record), authorization_id),
                **postgres_cluster.connection_kwargs(),
            )
            if last_ledger is not None:
                last_ledger.close()
            last_ledger = ledger
            result = append_memory_catalog_records(
                ledger,
                (record,),
                recorded_at="2026-07-27T00:20:00Z",
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            )
        assert result is not None
        snapshot = rebuild_memory_catalog_from_ledger(
            last_ledger,
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        assert snapshot.source_event_count == len(records)
        assert snapshot.load_head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=records[0].memory_id,
        ) == result.projection.activated_head
    finally:
        if last_ledger is not None:
            last_ledger.close()
