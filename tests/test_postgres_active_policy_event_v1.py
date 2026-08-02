from tests.postgres_support import PostgresCluster
from tests.test_active_policy_event_v1 import (
    TRUSTED_VERIFIERS,
    _access,
    _records,
)
from tests.test_postgres_event_ledger_v1 import _install
from trace_backed_memory.active_policy_event_v1 import (
    append_active_policy_records,
    rebuild_active_policy_from_ledger,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1


def test_postgres_event_ledger_rebuilds_active_policy_like_sqlite(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    registration, activation = _records()
    ledger = None
    try:
        for record in (registration, activation):
            if hasattr(record, "registration"):
                actor = record.registration.registered_by
                client = record.registration.registered_via_client_id
                authorization_id = record.registration.authorization_event_id
            else:
                actor = record.activation.activated_by
                client = record.activation.activated_via_client_id
                authorization_id = record.activation.authorization_event_id
            if ledger is not None:
                ledger.close()
            ledger = PostgresEventLedgerV1.connect(
                _access(actor, client, authorization_id),
                **postgres_cluster.connection_kwargs(),
            )
            result = append_active_policy_records(
                ledger,
                (record,),
                recorded_at="2026-07-27T00:10:00Z",
                trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
            )
        snapshot = rebuild_active_policy_from_ledger(
            ledger,
            trusted_attestation_verifier_ids=TRUSTED_VERIFIERS,
        )
        assert snapshot.projection == result.projection
        assert snapshot.source_event_count == 2
        assert snapshot.load_active_policy() == (
            registration.registration.policy_bundle
        )
    finally:
        if ledger is not None:
            ledger.close()
