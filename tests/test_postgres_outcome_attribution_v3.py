from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from trace_backed_memory._timestamps import (
    aware_datetime_to_rfc3339,
    parse_rfc3339,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_INSTALL = ROOT / "schemas" / "postgres-v3-gate-session.sql"
GATE_ROLLBACK = ROOT / "schemas" / "postgres-v3-gate-session-rollback.sql"
OUTCOME_INSTALL = ROOT / "schemas" / "postgres-v3-outcome.sql"
OUTCOME_ROLLBACK = ROOT / "schemas" / "postgres-v3-outcome-rollback.sql"
INSTALL = ROOT / "schemas" / "postgres-v3-outcome-attribution.sql"
ROLLBACK = (
    ROOT / "schemas" / "postgres-v3-outcome-attribution-rollback.sql"
)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64
REVISION_B = "memory_revision_sha256_" + "2" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    for script in (GATE_INSTALL, OUTCOME_INSTALL, INSTALL):
        result = cluster.run_script(script)
        assert result.returncode == 0, result.stderr


def _complete(repository, *, suffix: str = "001"):
    import trace_backed_memory as tbm

    sessions = repository.outcomes.gate_sessions
    created = sessions.create_or_get(
        session_id=f"gate_session_{suffix}",
        tenant_id="tenant_001",
        repository_id="repository_001",
        principal_id="principal_001",
        agent_client_id="agent_001",
        trace_id=f"trace_{suffix}",
        run_id=f"run_{suffix}",
        request_fingerprint=DIGEST_A,
        idempotency_key=f"request-{suffix}",
        expires_in_seconds=3600,
    ).session
    prepared = sessions.transition(
        created.session_id,
        "prepared",
        expected_version=created.version,
        lease_seconds=1200,
        retrieval_snapshot_id=f"retrieval_{suffix}",
        system_gate_evaluation_id=f"system_gate_{suffix}",
    )
    awaiting = sessions.transition(
        prepared.session_id,
        "awaiting_decision",
        expected_version=prepared.version,
    )
    decided = sessions.transition(
        awaiting.session_id,
        "decided",
        expected_version=awaiting.version,
        semantic_gate_attempt_ids=(f"semantic_attempt_{suffix}",),
        decision_id=f"decision_{suffix}",
    )
    finalized = sessions.transition(
        decided.session_id,
        "finalized",
        expected_version=decided.version,
        final_memory_revision_ids=(REVISION_A, REVISION_B),
        injection_artifact_id=f"injection_{suffix}",
        usage_decision_id=f"usage_{suffix}",
    )
    executing = sessions.transition(
        finalized.session_id,
        "executing",
        expected_version=finalized.version,
    )
    return repository.outcomes.complete_session(
        tbm.GateCompletionRequest(
            session_id=executing.session_id,
            expected_version=executing.version,
            result="pass",
            evaluator_id="run_evaluator",
            evaluator_version="1.0",
            output_sha256=DIGEST_A,
            evidence_artifact_sha256s=(DIGEST_B,),
            latency_ms=200,
            cost_usd=0.5,
        )
    )


def _after(timestamp: str, seconds: int = 1) -> str:
    return aware_datetime_to_rfc3339(
        parse_rfc3339(timestamp) + timedelta(seconds=seconds)
    )


def _attribution(outcome, **changes):
    import trace_backed_memory as tbm

    values = {
        "run_outcome_id_value": outcome.run_outcome_id,
        "usage_decision_id": outcome.usage_decision_id,
        "memory_revision_ids": (REVISION_A,),
        "claim_strength": "association",
        "effect": "unknown",
        "method": "runtime_observation",
        "evaluator_id": "attribution_evaluator",
        "evaluator_version": "2.0",
        "evidence_artifact_sha256s": (DIGEST_A,),
        "confidence": 0.5,
        "reason": "Observed association only.",
        "recorded_at": _after(outcome.measured_at),
    }
    values.update(changes)
    return tbm.build_outcome_attribution(**values)


def _insert_sql() -> str:
    return """
        INSERT INTO
            trace_backed_memory_v3_outcome_attribution.
                outcome_attributions (
            attribution_id,
            run_outcome_id,
            usage_decision_id,
            memory_revision_ids_json,
            claim_strength,
            effect,
            method,
            evaluator_id,
            evaluator_version,
            verifier_id,
            evidence_artifact_sha256s_json,
            confidence_json,
            reason,
            recorded_at,
            descriptor
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s
        )
    """


def test_postgres_outcome_attribution_install_roundtrip_and_rollback(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version, contract_version "
        "FROM trace_backed_memory_v3_outcome_attribution.schema_metadata",
    ) == "1|tbm.outcome-attribution.v3"

    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        completed = _complete(repository)
        association = _attribution(completed.outcome)
        assert repository.put_attribution(association) == (
            tbm.PostgresOutcomeAttributionWrite(association, True)
        )
        assert repository.put_attribution(association) == (
            tbm.PostgresOutcomeAttributionWrite(association, False)
        )
        assert repository.get_attribution(
            association.attribution_id
        ) == association

        causal = _attribution(
            completed.outcome,
            memory_revision_ids=(REVISION_A, REVISION_B),
            claim_strength="causal",
            effect="helped",
            method="controlled_experiment",
            evaluator_id="experiment_evaluator",
            verifier_id="independent_verifier",
            confidence=0.9,
            reason="Controlled comparison with independent verification.",
            recorded_at=_after(completed.outcome.measured_at, 2),
        )
        assert repository.put_attribution(causal).inserted is True
        assert repository.list_attributions(
            completed.outcome.run_outcome_id
        ) == (association, causal)

    result = postgres_cluster.run_script(ROLLBACK)
    assert result.returncode == 0, result.stderr
    result = postgres_cluster.run_script(OUTCOME_ROLLBACK)
    assert result.returncode == 0, result.stderr
    result = postgres_cluster.run_script(GATE_ROLLBACK)
    assert result.returncode == 0, result.stderr


def test_postgres_outcome_attribution_linkage_and_savepoint_fail_closed(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        completed = _complete(repository)
        invalid = _attribution(
            completed.outcome,
            memory_revision_ids=("memory_revision_sha256_" + "f" * 64,),
        )
        with connection.transaction():
            connection.execute(
                "CREATE TEMP TABLE caller_attribution_state (value text)"
            )
            connection.execute(
                "INSERT INTO caller_attribution_state VALUES ('before')"
            )
            with pytest.raises(
                tbm.PostgresOutcomeAttributionV3PersistenceError
            ) as rejected:
                repository.put_attribution(invalid)
            assert rejected.value.code == (
                "TBM_POSTGRES_OUTCOME_ATTRIBUTION_LINKAGE"
            )
            connection.execute(
                "INSERT INTO caller_attribution_state VALUES ('after')"
            )
            assert connection.execute(
                "SELECT value FROM caller_attribution_state ORDER BY value"
            ).fetchall() == [("after",), ("before",)]
        assert repository.list_attributions(
            completed.outcome.run_outcome_id
        ) == ()


def test_postgres_outcome_attribution_direct_sql_guards(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        completed = _complete(repository)
        attribution = _attribution(completed.outcome)
        repository.put_attribution(attribution)

        for statement in (
            "UPDATE trace_backed_memory_v3_outcome_attribution."
            "outcome_attributions SET reason = 'changed'",
            "DELETE FROM trace_backed_memory_v3_outcome_attribution."
            "outcome_attributions",
            "TRUNCATE trace_backed_memory_v3_outcome_attribution."
            "outcome_attributions",
        ):
            with pytest.raises(psycopg.Error, match="immutable"):
                with connection.transaction():
                    connection.execute(statement)

        whitespace = _attribution(
            completed.outcome,
            reason="Non-canonical array serialization.",
            recorded_at=_after(completed.outcome.measured_at, 3),
        )
        invalid = list(repository._attribution_values(whitespace))
        canonical = invalid[3]
        invalid[3] = f'[ "{REVISION_A}" ]'
        invalid[-1] = invalid[-1].replace(canonical, invalid[3])
        with pytest.raises(psycopg.Error, match="not canonical"):
            with connection.transaction():
                connection.execute(_insert_sql(), invalid)

        evidence_whitespace = _attribution(
            completed.outcome,
            reason="Non-canonical evidence serialization.",
            recorded_at=_after(completed.outcome.measured_at, 4),
        )
        invalid = list(
            repository._attribution_values(evidence_whitespace)
        )
        canonical = invalid[10]
        invalid[10] = f'[ "{DIGEST_A}" ]'
        invalid[-1] = invalid[-1].replace(canonical, invalid[10])
        with pytest.raises(psycopg.Error, match="not canonical"):
            with connection.transaction():
                connection.execute(_insert_sql(), invalid)

        exponent = _attribution(
            completed.outcome,
            reason="Non-canonical confidence.",
            recorded_at=_after(completed.outcome.measured_at, 5),
        )
        invalid = list(repository._attribution_values(exponent))
        invalid[11] = "5e-1"
        invalid[-1] = invalid[-1].replace(
            '"confidence":0.5',
            '"confidence":5e-1',
        )
        with pytest.raises(psycopg.Error, match="not canonical"):
            with connection.transaction():
                connection.execute(_insert_sql(), invalid)

        false_causal = _attribution(
            completed.outcome,
            reason="Causal claim lacks independent verification.",
            recorded_at=_after(completed.outcome.measured_at, 6),
        )
        invalid = list(repository._attribution_values(false_causal))
        invalid[4] = "causal"
        with pytest.raises(psycopg.Error, match="causal"):
            with connection.transaction():
                connection.execute(_insert_sql(), invalid)

        reversed_time = _attribution(
            completed.outcome,
            reason="Microsecond time reversal.",
            recorded_at=aware_datetime_to_rfc3339(
                parse_rfc3339(completed.outcome.measured_at)
                - timedelta(microseconds=1)
            ),
        )
        with pytest.raises(psycopg.Error, match="linkage"):
            with connection.transaction():
                connection.execute(
                    _insert_sql(),
                    repository._attribution_values(reversed_time),
                )


def test_postgres_outcome_attribution_schema_drift_and_errors(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        completed = _complete(repository)
        with pytest.raises(tbm.PostgresOutcomeAttributionV3NotFoundError):
            repository.get_attribution(
                "outcome_attribution_sha256_" + "f" * 64
            )
        with pytest.raises(tbm.PostgresOutcomeAttributionV3NotFoundError):
            repository.list_attributions(
                "run_outcome_sha256_" + "f" * 64
            )
        with pytest.raises(TypeError):
            repository.put_attribution(object())
        with pytest.raises(ValueError):
            repository.get_attribution(1)
        with pytest.raises(ValueError):
            repository.list_attributions(1)
        connection.execute(
            "CREATE INDEX unexpected_attribution_index "
            "ON trace_backed_memory_v3_outcome_attribution."
            "outcome_attributions (effect)"
        )
        connection.commit()
        with pytest.raises(tbm.PostgresOutcomeAttributionV3SchemaError):
            repository.list_attributions(
                completed.outcome.run_outcome_id
            )


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER TABLE trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions DISABLE TRIGGER "
        "outcome_attributions_validate_insert",
        "ALTER TABLE trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions DROP CONSTRAINT "
        "outcome_attributions_reason_check, ADD CONSTRAINT "
        "outcome_attributions_reason_check CHECK (true)",
        "DROP INDEX trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions_by_outcome; CREATE INDEX "
        "outcome_attributions_by_outcome ON "
        "trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions (effect)",
        "ALTER FUNCTION trace_backed_memory_v3_outcome_attribution."
        "validate_attribution_insert() SECURITY DEFINER",
        "CREATE OR REPLACE FUNCTION "
        "trace_backed_memory_v3_outcome_attribution."
        "validate_attribution_insert() RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS "
        "$body$ BEGIN RETURN NEW; END $body$",
        "GRANT EXECUTE ON FUNCTION "
        "trace_backed_memory_v3_outcome_attribution."
        "validate_attribution_insert() TO PUBLIC",
        "GRANT INSERT ON TABLE "
        "trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions TO PUBLIC",
    ),
)
def test_postgres_outcome_attribution_exact_catalog_rejects_tampering(
    postgres_cluster: PostgresCluster,
    tamper_sql: str,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        completed = _complete(repository)
        with pytest.raises(tbm.PostgresOutcomeAttributionV3SchemaError):
            with connection.transaction():
                connection.execute(tamper_sql)
                repository.list_attributions(
                    completed.outcome.run_outcome_id
                )
        assert repository.list_attributions(
            completed.outcome.run_outcome_id
        ) == ()


def test_postgres_outcome_attribution_rollback_rejects_catalog_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    tamper = postgres_cluster.run(
        "ALTER TABLE trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions DISABLE TRIGGER "
        "outcome_attributions_validate_insert"
    )
    assert tamper.returncode == 0, tamper.stderr

    result = postgres_cluster.run_script(ROLLBACK)
    assert result.returncode != 0
    assert "rollback catalog mismatch" in result.stderr
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT to_regclass("
        "'trace_backed_memory_v3_outcome_attribution."
        "outcome_attributions') IS NOT NULL",
    ) == "t"


def test_postgres_outcome_attribution_concurrent_exact_replay(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        completed = _complete(repository)
        attribution = _attribution(completed.outcome)

    def write_once():
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            repository = tbm.PostgresOutcomeAttributionV3Repository(
                connection
            )
            return repository.put_attribution(attribution)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: write_once(), range(2)))
    assert sorted(result.inserted for result in results) == [False, True]


def test_postgres_outcome_attribution_resources_exports_and_close(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    import trace_backed_memory as tbm

    _install(postgres_cluster)
    assert tbm.POSTGRES_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION == 1
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-outcome-attribution.sql"
    ) == INSTALL.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-outcome-attribution-rollback.sql"
    ) == ROLLBACK.read_bytes()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs()
    ) as connection:
        repository = tbm.PostgresOutcomeAttributionV3Repository(connection)
        with repository as opened:
            assert opened.outcomes is repository.outcomes
        repository.close()
        with pytest.raises(
            tbm.PostgresOutcomeAttributionV3PersistenceError
        ):
            repository.__enter__()
