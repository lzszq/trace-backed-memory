from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_semantic_gate_v3 as semantic_module
from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from trace_backed_memory.postgres import _load_psycopg
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3Repository,
)
from trace_backed_memory.postgres_semantic_gate_v3 import (
    PostgresSemanticGateV3ConflictError,
    PostgresSemanticGateV3Error,
    PostgresSemanticGateV3NotFoundError,
    PostgresSemanticGateV3PersistenceError,
    PostgresSemanticGateV3Repository,
    PostgresSemanticGateV3SchemaError,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_EVIDENCE_INSTALL = (
    ROOT / "schemas" / "postgres-v3-gate-evidence.sql"
)
INSTALL = ROOT / "schemas" / "postgres-v3-semantic-gate.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-semantic-gate-rollback.sql"
SCHEMA = "trace_backed_memory_v3_semantic_gate"


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    evidence = postgres_cluster.run_script(GATE_EVIDENCE_INSTALL)
    assert evidence.returncode == 0, evidence.stderr
    semantic = postgres_cluster.run_script(INSTALL)
    assert semantic.returncode == 0, semantic.stderr


def _records() -> tuple[
    tbm.RetrievalSnapshot,
    tbm.SystemGateEvaluation,
    tbm.SemanticGateAttempt,
]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    attempt = tbm.loads_semantic_gate_attempt(
        (
            ROOT / "examples" / "semantic_gate_attempt_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation, attempt


def _next_attempt(
    parent: tbm.SemanticGateAttempt,
    *,
    request_id: str = "provider_request_002",
) -> tbm.SemanticGateAttempt:
    values = {
        key: value
        for key, value in parent.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        sequence=parent.sequence + 1,
        previous_attempt_id=parent.attempt_id,
        provider_request_id=request_id,
        decision_id=f"decision_{parent.sequence + 1:03d}",
        started_at="2026-07-27T08:03:00Z",
        finished_at="2026-07-27T08:03:01Z",
    )
    return tbm.build_semantic_gate_attempt(**values)


def _seed_evidence(postgres_cluster: PostgresCluster) -> None:
    snapshot, evaluation, _attempt = _records()
    with PostgresGateEvidenceV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as repository:
        repository.store_bundle(snapshot, evaluation)


def _repository(
    postgres_cluster: PostgresCluster,
) -> PostgresSemanticGateV3Repository:
    return PostgresSemanticGateV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )


def test_postgres_semantic_gate_catalog_fingerprint(
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
                semantic_module
                ._POSTGRES_SEMANTIC_GATE_CATALOG_SHA256_QUERY,
                (SCHEMA,) * 7,
            )
            fingerprint = cursor.fetchone()["catalog_sha256"]
            cursor.execute(
                "SELECT array_agg(class.relname ORDER BY class.relname) "
                "AS names FROM pg_catalog.pg_class AS class "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = %s "
                "AND class.relkind IN ('r', 'i', 'p')",
                (SCHEMA,),
            )
            relations = cursor.fetchone()["names"]
            cursor.execute(
                "SELECT array_agg(procedure.proname "
                "ORDER BY procedure.proname) AS names "
                "FROM pg_catalog.pg_proc AS procedure "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = procedure.pronamespace "
                "WHERE namespace.nspname = %s",
                (SCHEMA,),
            )
            functions = cursor.fetchone()["names"]
            cursor.execute(
                "SELECT array_agg(trigger.tgname ORDER BY trigger.tgname) "
                "AS names FROM pg_catalog.pg_trigger AS trigger "
                "JOIN pg_catalog.pg_class AS class "
                "ON class.oid = trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = %s "
                "AND NOT trigger.tgisinternal",
                (SCHEMA,),
            )
            triggers = cursor.fetchone()["names"]
    print(f"POSTGRES_SEMANTIC_GATE_CATALOG_SHA256={fingerprint}")
    print(f"POSTGRES_SEMANTIC_GATE_RELATIONS={relations}")
    print(f"POSTGRES_SEMANTIC_GATE_FUNCTIONS={functions}")
    print(f"POSTGRES_SEMANTIC_GATE_TRIGGERS={triggers}")
    assert len(fingerprint) == 64


def test_postgres_semantic_gate_public_exports_are_intentional() -> None:
    assert (
        tbm.PostgresSemanticGateV3Repository
        is PostgresSemanticGateV3Repository
    )
    assert tbm.POSTGRES_SEMANTIC_GATE_V3_SCHEMA_VERSION == 1
    assert (
        tbm.POSTGRES_SEMANTIC_GATE_V3_CONTRACT_VERSION
        == "tbm.semantic-gate-attempt.v3"
    )
    assert "PostgresSemanticGateV3Repository" in tbm.__all__
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-semantic-gate.sql"
    ) == INSTALL.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-semantic-gate-rollback.sql"
    ) == ROLLBACK.read_bytes()


def test_postgres_semantic_gate_round_trip_and_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, evaluation, first = _records()
    second = _next_attempt(first)
    with _repository(postgres_cluster) as repository:
        first_result = repository.store_attempt(first)
        replay = repository.store_attempt(first)
        second_result = repository.store_attempt(second)

        assert first_result.inserted is True
        assert replay.inserted is False
        assert second_result.inserted is True
        assert repository.load_attempt(first.attempt_id) == first
        assert repository.load_attempt(second.attempt_id) == second
        assert repository.load_chain(evaluation.evaluation_id) == (
            first,
            second,
        )


def test_postgres_semantic_gate_rejects_forked_sequence(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, evaluation, first = _records()
    accepted = _next_attempt(first, request_id="provider_request_accepted")
    fork = _next_attempt(first, request_id="provider_request_fork")
    with _repository(postgres_cluster) as repository:
        repository.store_attempt(first)
        repository.store_attempt(accepted)
        with pytest.raises(
            PostgresSemanticGateV3ConflictError,
            match="extend",
        ):
            repository.store_attempt(fork)
        assert repository.load_chain(evaluation.evaluation_id) == (
            first,
            accepted,
        )


def test_postgres_semantic_gate_concurrent_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()

    def append() -> bool:
        with _repository(postgres_cluster) as repository:
            return repository.store_attempt(attempt).inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: append(), range(2)))
    assert sorted(results) == [False, True]


def test_postgres_semantic_gate_concurrent_first_fork_has_one_head(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, evaluation, first = _records()
    values = {
        key: value
        for key, value in first.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        provider_request_id="provider_request_first_fork",
        decision_id="decision_first_fork",
    )
    fork = tbm.build_semantic_gate_attempt(**values)
    barrier = Barrier(2)

    def append(attempt: tbm.SemanticGateAttempt) -> str:
        barrier.wait()
        try:
            with _repository(postgres_cluster) as repository:
                repository.store_attempt(attempt)
            return "stored"
        except PostgresSemanticGateV3ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, (first, fork)))
    assert sorted(results) == ["conflict", "stored"]
    with _repository(postgres_cluster) as repository:
        chain = repository.load_chain(evaluation.evaluation_id)
        assert len(chain) == 1
        assert chain[0] in (first, fork)


def test_postgres_semantic_gate_concurrent_later_fork_has_one_head(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, evaluation, first = _records()
    with _repository(postgres_cluster) as repository:
        repository.store_attempt(first)
    attempts = (
        _next_attempt(first, request_id="provider_request_a"),
        _next_attempt(first, request_id="provider_request_b"),
    )
    barrier = Barrier(2)

    def append(attempt: tbm.SemanticGateAttempt) -> str:
        barrier.wait()
        try:
            with _repository(postgres_cluster) as repository:
                repository.store_attempt(attempt)
            return "stored"
        except PostgresSemanticGateV3ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, attempts))
    assert sorted(results) == ["conflict", "stored"]
    with _repository(postgres_cluster) as repository:
        chain = repository.load_chain(evaluation.evaluation_id)
        assert len(chain) == 2
        assert chain[0] == first
        assert chain[1] in attempts


def test_postgres_semantic_gate_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    psycopg, dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    )
    repository = PostgresSemanticGateV3Repository(connection)
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_work (value text)")
                cursor.execute("INSERT INTO caller_work VALUES ('before')")
            repository.store_attempt(attempt)
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_work")
                assert cursor.fetchall() == [{"value": "before"}]
        assert repository.load_attempt(attempt.attempt_id) == attempt
    finally:
        repository.close()
        connection.close()


def test_postgres_semantic_gate_restores_caller_search_path_on_failure(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, first = _records()
    accepted = _next_attempt(first, request_id="provider_request_accepted")
    fork = _next_attempt(first, request_id="provider_request_fork")
    psycopg, dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    )
    repository = PostgresSemanticGateV3Repository(connection)
    try:
        repository.store_attempt(first)
        repository.store_attempt(accepted)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config("
                    "'search_path', 'public, pg_catalog', true)"
                )
            with pytest.raises(PostgresSemanticGateV3ConflictError):
                repository.store_attempt(fork)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.current_setting('search_path') "
                    "AS search_path"
                )
                assert cursor.fetchone() == {
                    "search_path": "public, pg_catalog"
                }
    finally:
        repository.close()
        connection.close()


def test_postgres_semantic_gate_direct_writes_are_guarded(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        repository.store_attempt(attempt)
    for sql in (
        f"UPDATE {SCHEMA}.v3_semantic_gate_attempts "
        "SET status = status",
        f"DELETE FROM {SCHEMA}.v3_semantic_gate_attempt_heads",
        f"TRUNCATE {SCHEMA}.v3_semantic_gate_attempts CASCADE",
    ):
        rejected = postgres_cluster.run(sql)
        assert rejected.returncode != 0
        assert "immutable" in rejected.stderr


def test_postgres_semantic_gate_rejects_unadvanced_direct_attempt(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    snapshot, evaluation, attempt = _records()
    descriptor = tbm.dumps_semantic_gate_attempt(attempt)
    escaped_descriptor = descriptor.replace("'", "''")
    sql = (
        "BEGIN; "
        f"INSERT INTO {SCHEMA}.v3_semantic_gate_attempt_heads "
        "(system_gate_evaluation_id, session_id, retrieval_snapshot_id, "
        "current_sequence, current_attempt_id) VALUES "
        f"('{evaluation.evaluation_id}', '{evaluation.session_id}', "
        f"'{snapshot.snapshot_id}', 0, NULL); "
        f"INSERT INTO {SCHEMA}.v3_semantic_gate_attempts "
        "(attempt_id, session_id, retrieval_snapshot_id, "
        "system_gate_evaluation_id, sequence, previous_attempt_id, status, "
        "started_at, finished_at, descriptor) VALUES "
        f"('{attempt.attempt_id}', '{attempt.session_id}', "
        f"'{attempt.retrieval_snapshot_id}', "
        f"'{attempt.system_gate_evaluation_id}', 1, NULL, "
        f"'{attempt.status}', '{attempt.started_at}', "
        f"'{attempt.finished_at}', "
        f"'{escaped_descriptor}'); COMMIT;"
    )
    rejected = postgres_cluster.run(sql)
    assert rejected.returncode != 0
    assert "chain consistency mismatch" in rejected.stderr
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresSemanticGateV3NotFoundError):
            repository.load_chain(evaluation.evaluation_id)


def test_postgres_semantic_gate_detects_catalog_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        f"GRANT SELECT ON {SCHEMA}.v3_semantic_gate_attempts TO PUBLIC",
    )
    _snapshot, _evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresSemanticGateV3SchemaError):
            repository.store_attempt(attempt)


def test_postgres_semantic_gate_detects_function_owner_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE ROLE semantic_gate_attacker",
    )
    assert_sql_succeeds(
        postgres_cluster,
        f"ALTER FUNCTION {SCHEMA}.reject_immutable_change() "
        "OWNER TO semantic_gate_attacker",
    )
    _snapshot, _evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresSemanticGateV3SchemaError):
            repository.store_attempt(attempt)
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog fingerprint mismatch" in rejected.stderr


def test_postgres_semantic_gate_detects_coordinated_owner_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE ROLE semantic_gate_attacker",
    )
    for table in (
        "schema_metadata",
        "v3_semantic_gate_attempt_heads",
        "v3_semantic_gate_attempts",
    ):
        assert_sql_succeeds(
            postgres_cluster,
            f"ALTER TABLE {SCHEMA}.{table} OWNER TO semantic_gate_attacker",
        )
    for function in (
        "protect_head_update",
        "reject_immutable_change",
        "validate_attempt_insert",
        "validate_chain_consistency",
        "validate_head_insert",
    ):
        assert_sql_succeeds(
            postgres_cluster,
            f"ALTER FUNCTION {SCHEMA}.{function}() "
            "OWNER TO semantic_gate_attacker",
        )
    assert_sql_succeeds(
        postgres_cluster,
        f"ALTER SCHEMA {SCHEMA} OWNER TO semantic_gate_attacker",
    )
    _snapshot, _evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresSemanticGateV3SchemaError):
            repository.store_attempt(attempt)
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog fingerprint mismatch" in rejected.stderr


def test_postgres_semantic_gate_detects_descriptor_column_tamper(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        repository.store_attempt(attempt)
        assert_sql_succeeds(
            postgres_cluster,
            f"ALTER TABLE {SCHEMA}.v3_semantic_gate_attempts "
            "DISABLE TRIGGER semantic_gate_attempt_immutable",
        )
        assert_sql_succeeds(
            postgres_cluster,
            f"UPDATE {SCHEMA}.v3_semantic_gate_attempts "
            "SET status = 'failed'",
        )
        assert_sql_succeeds(
            postgres_cluster,
            f"ALTER TABLE {SCHEMA}.v3_semantic_gate_attempts "
            "ENABLE TRIGGER semantic_gate_attempt_immutable",
        )
        with pytest.raises(
            PostgresSemanticGateV3PersistenceError,
            match="columns do not match descriptor",
        ):
            repository.load_attempt(attempt.attempt_id)


def test_postgres_semantic_gate_rollback_is_restrictive(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        f"CREATE VIEW public.external_semantic_gate_dependency AS "
        f"SELECT attempt_id FROM {SCHEMA}.v3_semantic_gate_attempts",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "external_semantic_gate_dependency" in rejected.stderr
    assert "CASCADE" in rejected.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "DROP VIEW public.external_semantic_gate_dependency",
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


def test_postgres_semantic_gate_rollback_rejects_function_drift(
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


def test_postgres_semantic_gate_requires_gate_evidence_schema(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    rejected = postgres_cluster.run_script(INSTALL)
    assert rejected.returncode != 0
    assert "gate_evidence" in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            f"SELECT to_regnamespace('{SCHEMA}') IS NULL",
        )
        == "t"
    )


def test_postgres_semantic_gate_runtime_and_rollback_reject_active_v1(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 1 WHERE singleton",
    )
    _snapshot, _evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresSemanticGateV3SchemaError):
            repository.store_attempt(attempt)
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "metadata mismatch" in rejected.stderr


def test_postgres_semantic_gate_rejects_invalid_and_missing_inputs(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, evaluation, attempt = _records()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(ValueError, match="exactly SemanticGateAttempt"):
            repository.store_attempt(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="attempt_id"):
            repository.load_attempt("invalid")
        with pytest.raises(ValueError, match="evaluation_id"):
            repository.load_chain("invalid")
        with pytest.raises(PostgresSemanticGateV3NotFoundError):
            repository.load_attempt(attempt.attempt_id)
        with pytest.raises(PostgresSemanticGateV3NotFoundError):
            repository.load_chain(evaluation.evaluation_id)
    with pytest.raises(PostgresSemanticGateV3Error, match="closed"):
        repository.load_attempt(attempt.attempt_id)
    repository.close()

    with pytest.raises(ValueError, match="connection is required"):
        PostgresSemanticGateV3Repository(None)


def test_postgres_semantic_gate_maps_database_errors_without_details() -> None:
    repository = PostgresSemanticGateV3Repository(object())

    class _DatabaseError(Exception):
        def __init__(self, sqlstate: str | None) -> None:
            super().__init__("secret database details")
            self.sqlstate = sqlstate

    with pytest.raises(PostgresSemanticGateV3SchemaError, match="missing"):
        repository._raise_database(_DatabaseError("42P01"))
    with pytest.raises(PostgresSemanticGateV3ConflictError, match="conflicts"):
        repository._raise_database(_DatabaseError("23505"))
    with pytest.raises(PostgresSemanticGateV3ConflictError, match="conflicts"):
        repository._raise_database(_DatabaseError("P0001"))
    with pytest.raises(
        PostgresSemanticGateV3PersistenceError,
        match="operation failed",
    ) as failure:
        repository._raise_database(_DatabaseError(None))
    assert "secret" not in str(failure.value)


def test_postgres_semantic_gate_connect_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Psycopg:
        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret connection failure")

    monkeypatch.setattr(
        semantic_module,
        "_load_psycopg",
        lambda: (_Psycopg(), object(), object()),
    )
    with pytest.raises(
        PostgresSemanticGateV3PersistenceError,
        match="failed to connect",
    ) as failure:
        PostgresSemanticGateV3Repository.connect("secret")
    assert "secret" not in str(failure.value)
