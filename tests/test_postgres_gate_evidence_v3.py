from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_gate_evidence_v3 as gate_evidence_module
from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from trace_backed_memory.postgres import _load_psycopg
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3ConflictError,
    PostgresGateEvidenceV3NotFoundError,
    PostgresGateEvidenceV3PersistenceError,
    PostgresGateEvidenceV3Repository,
    PostgresGateEvidenceV3SchemaError,
    _GATE_EVIDENCE_CATALOG_SHA256_QUERY,
)


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-gate-evidence.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-gate-evidence-rollback.sql"
SCHEMA = "trace_backed_memory_v3_gate_evidence"


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    result = postgres_cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr


def _records() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation


def _repository(
    postgres_cluster: PostgresCluster,
) -> PostgresGateEvidenceV3Repository:
    return PostgresGateEvidenceV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )


def test_postgres_gate_evidence_catalog_fingerprint(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _GATE_EVIDENCE_CATALOG_SHA256_QUERY,
                (SCHEMA,) * 7,
            )
            fingerprint = cursor.fetchone()["catalog_sha256"]
    print(f"GATE_EVIDENCE_CATALOG_SHA256={fingerprint}")
    assert len(fingerprint) == 64


def test_postgres_gate_evidence_public_exports_are_intentional() -> None:
    assert (
        tbm.PostgresGateEvidenceV3Repository
        is PostgresGateEvidenceV3Repository
    )
    assert tbm.POSTGRES_GATE_EVIDENCE_V3_SCHEMA_VERSION == 1
    assert (
        tbm.POSTGRES_GATE_EVIDENCE_V3_CONTRACT_VERSION
        == "tbm.gate-evidence.v3"
    )
    assert "PostgresGateEvidenceV3Repository" in tbm.__all__
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-gate-evidence.sql"
    ) == INSTALL.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-gate-evidence-rollback.sql"
    ) == ROLLBACK.read_bytes()


def test_postgres_gate_evidence_round_trip_and_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    repository = _repository(postgres_cluster)
    try:
        first = repository.store_bundle(snapshot, evaluation)
        second = repository.store_bundle(snapshot, evaluation)
        assert first.snapshot_inserted is True
        assert first.evaluation_inserted is True
        assert second.snapshot_inserted is False
        assert second.evaluation_inserted is False
        assert repository.load_snapshot(snapshot.snapshot_id) == snapshot
        assert repository.load_evaluation(evaluation.evaluation_id) == evaluation
    finally:
        repository.close()


def test_postgres_gate_evidence_rejects_second_evaluation_atomically(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    conflicting = tbm.build_system_gate_evaluation(
        session_id=evaluation.session_id,
        retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
        authorization_event_id=evaluation.authorization_event_id,
        policy_bundle_sha256=evaluation.policy_bundle_sha256,
        evaluator_id=evaluation.evaluator_id,
        evaluator_version="3.1",
        decisions=evaluation.decisions,
        evaluated_at=evaluation.evaluated_at,
    )
    repository = _repository(postgres_cluster)
    try:
        repository.store_bundle(snapshot, evaluation)
        with pytest.raises(PostgresGateEvidenceV3ConflictError):
            repository.store_bundle(snapshot, conflicting)
        assert repository.load_evaluation(evaluation.evaluation_id) == evaluation
        with pytest.raises(PostgresGateEvidenceV3NotFoundError):
            repository.load_evaluation(conflicting.evaluation_id)
    finally:
        repository.close()


def test_postgres_gate_evidence_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    psycopg, dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    )
    repository = PostgresGateEvidenceV3Repository(connection)
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_work (value text)")
                cursor.execute("INSERT INTO caller_work VALUES ('before')")
            repository.store_bundle(snapshot, evaluation)
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_work")
                assert cursor.fetchall() == [{"value": "before"}]
        assert repository.load_snapshot(snapshot.snapshot_id) == snapshot
    finally:
        repository.close()
        connection.close()


def test_postgres_gate_evidence_accepts_injected_default_tuple_connection(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    psycopg, _dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(**postgres_cluster.connection_kwargs())
    repository = PostgresGateEvidenceV3Repository(connection)
    try:
        repository.store_bundle(snapshot, evaluation)
        assert repository.load_snapshot(snapshot.snapshot_id) == snapshot
    finally:
        repository.close()
        connection.close()


def test_postgres_gate_evidence_immutability_and_parent_binding(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    repository = _repository(postgres_cluster)
    try:
        repository.store_bundle(snapshot, evaluation)
        for sql in (
            f"UPDATE {SCHEMA}.v3_retrieval_snapshots "
            "SET session_id = session_id",
            f"DELETE FROM {SCHEMA}.v3_system_gate_evaluations",
            f"TRUNCATE {SCHEMA}.v3_retrieval_snapshots CASCADE",
        ):
            rejected = postgres_cluster.run(sql)
            assert rejected.returncode != 0
            assert "immutable" in rejected.stderr
    finally:
        repository.close()


def test_postgres_gate_evidence_catalog_drift_fails_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        f"GRANT SELECT ON {SCHEMA}.v3_retrieval_snapshots TO PUBLIC",
    )
    snapshot, evaluation = _records()
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(PostgresGateEvidenceV3SchemaError):
            repository.store_bundle(snapshot, evaluation)
    finally:
        repository.close()
    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert "catalog fingerprint mismatch" in rollback.stderr


def test_postgres_gate_evidence_rejects_policy_and_view_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        f"CREATE POLICY unexpected_policy ON "
        f"{SCHEMA}.v3_retrieval_snapshots USING (true)",
    )
    snapshot, evaluation = _records()
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(
            PostgresGateEvidenceV3SchemaError,
            match="unsupported policies, rules, or relation kinds",
        ):
            repository.store_bundle(snapshot, evaluation)
    finally:
        repository.close()


def test_postgres_gate_evidence_rollback_is_fail_closed_and_restrictive(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        f"CREATE VIEW public.external_gate_evidence_dependency AS "
        f"SELECT snapshot_id FROM {SCHEMA}.v3_retrieval_snapshots",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "external_gate_evidence_dependency" in rejected.stderr
    assert "CASCADE" in rejected.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "DROP VIEW public.external_gate_evidence_dependency",
    )
    removed = postgres_cluster.run_script(ROLLBACK)
    assert removed.returncode == 0, removed.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            f"SELECT to_regnamespace('{SCHEMA}') IS NULL",
        )
        == "t"
    )


def test_postgres_gate_evidence_rollback_rejects_function_body_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        f"CREATE OR REPLACE FUNCTION {SCHEMA}.reject_immutable_change() "
        "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog "
        "AS $$ BEGIN RETURN NEW; END $$",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog fingerprint mismatch" in rejected.stderr


def test_postgres_gate_evidence_detects_descriptor_column_mismatch(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    repository = _repository(postgres_cluster)
    try:
        repository.store_bundle(snapshot, evaluation)
        assert_sql_succeeds(
            postgres_cluster,
            f"ALTER TABLE {SCHEMA}.v3_retrieval_snapshots "
            "DISABLE TRIGGER gate_evidence_snapshot_immutable",
        )
        assert_sql_succeeds(
            postgres_cluster,
            f"UPDATE {SCHEMA}.v3_retrieval_snapshots "
            "SET session_id = 'tampered'",
        )
        assert_sql_succeeds(
            postgres_cluster,
            f"ALTER TABLE {SCHEMA}.v3_retrieval_snapshots "
            "ENABLE TRIGGER gate_evidence_snapshot_immutable",
        )
        with pytest.raises(
            PostgresGateEvidenceV3PersistenceError,
            match="columns do not match descriptor",
        ):
            repository.load_snapshot(snapshot.snapshot_id)
    finally:
        repository.close()


def test_postgres_gate_evidence_concurrent_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()

    def store_once():
        repository = _repository(postgres_cluster)
        try:
            return repository.store_bundle(snapshot, evaluation)
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: store_once(), range(2)))

    assert sorted(result.snapshot_inserted for result in results) == [
        False,
        True,
    ]
    assert sorted(result.evaluation_inserted for result in results) == [
        False,
        True,
    ]


def test_postgres_gate_evidence_rejects_invalid_and_missing_inputs(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    snapshot, evaluation = _records()
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(ValueError, match="exact v3 records"):
            repository.store_bundle(object(), evaluation)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="snapshot_id"):
            repository.load_snapshot("invalid")
        with pytest.raises(ValueError, match="evaluation_id"):
            repository.load_evaluation("invalid")
        with pytest.raises(PostgresGateEvidenceV3NotFoundError):
            repository.load_snapshot(snapshot.snapshot_id)
        with pytest.raises(PostgresGateEvidenceV3NotFoundError):
            repository.load_evaluation(evaluation.evaluation_id)
    finally:
        repository.close()

    with pytest.raises(tbm.PostgresGateEvidenceV3Error, match="closed"):
        repository.load_snapshot(snapshot.snapshot_id)
    repository.close()


@pytest.mark.parametrize(
    ("loader", "row"),
    (
        (
            PostgresGateEvidenceV3Repository._stored_snapshot,
            {
                "snapshot_id": "wrong",
                "session_id": "session",
                "authorization_event_id": "authz",
                "descriptor": object(),
            },
        ),
        (
            PostgresGateEvidenceV3Repository._stored_snapshot,
            {
                "snapshot_id": "wrong",
                "session_id": "session",
                "authorization_event_id": "authz",
                "descriptor": "{}",
            },
        ),
        (
            PostgresGateEvidenceV3Repository._stored_evaluation,
            {
                "evaluation_id": "wrong",
                "session_id": "session",
                "retrieval_snapshot_id": "snapshot",
                "authorization_event_id": "authz",
                "descriptor": object(),
            },
        ),
        (
            PostgresGateEvidenceV3Repository._stored_evaluation,
            {
                "evaluation_id": "wrong",
                "session_id": "session",
                "retrieval_snapshot_id": "snapshot",
                "authorization_event_id": "authz",
                "descriptor": "{}",
            },
        ),
    ),
)
def test_postgres_gate_evidence_rejects_corrupt_rows(loader, row) -> None:
    with pytest.raises(PostgresGateEvidenceV3PersistenceError):
        loader(row)


def test_postgres_gate_evidence_maps_database_errors_without_details() -> None:
    repository = PostgresGateEvidenceV3Repository(object())

    class _DatabaseError(Exception):
        def __init__(self, sqlstate: str | None) -> None:
            super().__init__("secret database details")
            self.sqlstate = sqlstate

    with pytest.raises(PostgresGateEvidenceV3SchemaError, match="missing"):
        repository._raise_database(_DatabaseError("42P01"))
    with pytest.raises(PostgresGateEvidenceV3ConflictError, match="conflicts"):
        repository._raise_database(_DatabaseError("23505"))
    with pytest.raises(
        PostgresGateEvidenceV3ConflictError,
        match="conflicts",
    ):
        repository._raise_database(_DatabaseError("P0001"))
    with pytest.raises(
        PostgresGateEvidenceV3PersistenceError,
        match="operation failed",
    ) as failure:
        repository._raise_database(_DatabaseError(None))
    assert "secret" not in str(failure.value)


def test_postgres_gate_evidence_rejects_missing_connection_and_connect_failure(
    monkeypatch,
) -> None:
    with pytest.raises(ValueError, match="connection is required"):
        PostgresGateEvidenceV3Repository(None)

    class _Psycopg:
        @staticmethod
        def connect(*_args, **_kwargs):
            raise RuntimeError("secret connection failure")

    monkeypatch.setattr(
        gate_evidence_module,
        "_load_psycopg",
        lambda: (_Psycopg(), object(), object()),
    )
    with pytest.raises(
        PostgresGateEvidenceV3PersistenceError,
        match="failed to connect",
    ) as failure:
        PostgresGateEvidenceV3Repository.connect("secret")
    assert "secret" not in str(failure.value)
