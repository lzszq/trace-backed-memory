from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory.sqlite_outcome_attribution_v3 as attribution_module
from trace_backed_memory._timestamps import (
    aware_datetime_to_rfc3339,
    parse_rfc3339,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION_A = "memory_revision_sha256_" + "1" * 64
REVISION_B = "memory_revision_sha256_" + "2" * 64
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "sqlite-v3-outcome-attribution.sql"


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
    return (
        "INSERT INTO v3_outcome_attributions ("
        "attribution_id, run_outcome_id, usage_decision_id, "
        "memory_revision_ids_json, claim_strength, effect, method, "
        "evaluator_id, evaluator_version, verifier_id, "
        "evidence_artifact_sha256s_json, confidence_json, reason, "
        "recorded_at, descriptor"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )


def test_sqlite_outcome_attribution_store_replay_list_and_multiple_claims():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    association = _attribution(completed.outcome)
    first = repository.put_attribution(association)
    assert first == tbm.SQLiteOutcomeAttributionWrite(association, True)
    assert repository.put_attribution(association) == (
        tbm.SQLiteOutcomeAttributionWrite(association, False)
    )
    assert repository.get_attribution(association.attribution_id) == association

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
    repository.close()


def test_sqlite_outcome_attribution_linkage_fails_closed():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    invalid_values = (
        {"usage_decision_id": "other_usage"},
        {"memory_revision_ids": ("memory_revision_sha256_" + "f" * 64,)},
        {"recorded_at": _after(completed.outcome.measured_at, -1)},
        {
            "recorded_at": aware_datetime_to_rfc3339(
                parse_rfc3339(completed.outcome.measured_at)
                - timedelta(microseconds=1)
            )
        },
    )
    for changes in invalid_values:
        attribution = _attribution(completed.outcome, **changes)
        with pytest.raises(
            tbm.SQLiteOutcomeAttributionV3PersistenceError
        ) as rejected:
            repository.put_attribution(attribution)
        assert rejected.value.code == (
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_LINKAGE"
        )
    assert repository.list_attributions(
        completed.outcome.run_outcome_id
    ) == ()


def test_sqlite_outcome_attribution_direct_sql_guards_and_readback():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    repository.put_attribution(attribution)
    row = repository._attribution_row(attribution)

    connection = repository._connection
    for statement in (
        "UPDATE v3_outcome_attributions SET reason = 'changed'",
        "DELETE FROM v3_outcome_attributions",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(statement)
    connection.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            _insert_sql().replace("INSERT", "INSERT OR REPLACE", 1),
            row,
        )
    connection.execute("PRAGMA recursive_triggers = ON")

    changed = _attribution(
        completed.outcome,
        reason="A second observation.",
        recorded_at=_after(completed.outcome.measured_at, 2),
    )
    invalid = list(repository._attribution_row(changed))
    invalid[-1] = "{}"
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(_insert_sql(), invalid)

    whitespace_evidence = _attribution(
        completed.outcome,
        reason="Non-canonical evidence serialization.",
        recorded_at=_after(completed.outcome.measured_at, 5),
    )
    invalid = list(repository._attribution_row(whitespace_evidence))
    canonical_evidence = invalid[10]
    invalid[10] = f'[ "{DIGEST_A}" ]'
    invalid[-1] = invalid[-1].replace(
        canonical_evidence,
        invalid[10],
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(_insert_sql(), invalid)

    whitespace = _attribution(
        completed.outcome,
        reason="Non-canonical array serialization.",
        recorded_at=_after(completed.outcome.measured_at, 3),
    )
    invalid = list(repository._attribution_row(whitespace))
    canonical_array = invalid[3]
    invalid[3] = f'[ "{REVISION_A}" ]'
    invalid[-1] = invalid[-1].replace(canonical_array, invalid[3])
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(_insert_sql(), invalid)

    exponent = _attribution(
        completed.outcome,
        reason="Non-canonical numeric serialization.",
        recorded_at=_after(completed.outcome.measured_at, 4),
    )
    invalid = list(repository._attribution_row(exponent))
    invalid[11] = "5e-1"
    invalid[-1] = invalid[-1].replace(
        '"confidence":0.5',
        '"confidence":5e-1',
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(_insert_sql(), invalid)

    microsecond_reversal = _attribution(
        completed.outcome,
        reason="Microsecond time reversal.",
        recorded_at=aware_datetime_to_rfc3339(
            parse_rfc3339(completed.outcome.measured_at)
            - timedelta(microseconds=1)
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(
            _insert_sql(),
            repository._attribution_row(microsecond_reversal),
        )
    assert repository.get_attribution(attribution.attribution_id) == attribution


def test_sqlite_outcome_attribution_direct_sql_rejects_corrupt_outcome():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    connection = repository._connection
    connection.execute("DROP TRIGGER v3_run_outcomes_immutable_update")
    connection.execute(
        "UPDATE v3_run_outcomes "
        "SET descriptor = '{ ' || substr(descriptor, 2) "
        "WHERE run_outcome_id = ?",
        (completed.outcome.run_outcome_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        connection.execute(
            _insert_sql(),
            repository._attribution_row(attribution),
        )


def test_sqlite_outcome_attribution_time_formats_use_instant_order():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    fractional_now = parse_rfc3339("2099-01-01T00:00:00.100000Z")

    def fractional_clock():
        nonlocal fractional_now
        value = aware_datetime_to_rfc3339(fractional_now)
        fractional_now += timedelta(seconds=1)
        return value

    repository.outcomes._clock = fractional_clock
    repository.outcomes.gate_sessions._clock = fractional_clock
    completed = _complete(repository)
    earlier_without_fraction = aware_datetime_to_rfc3339(
        parse_rfc3339(completed.outcome.measured_at).replace(
            microsecond=0
        )
    )
    reversed_attribution = _attribution(
        completed.outcome,
        recorded_at=earlier_without_fraction,
        reason="Equivalent-format time reversal.",
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        repository._connection.execute(
            _insert_sql(),
            repository._attribution_row(reversed_attribution),
        )

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    whole_second_now = parse_rfc3339("2099-01-01T00:00:00Z")

    def whole_second_clock():
        nonlocal whole_second_now
        value = aware_datetime_to_rfc3339(whole_second_now)
        whole_second_now += timedelta(seconds=1)
        return value

    repository.outcomes._clock = whole_second_clock
    repository.outcomes.gate_sessions._clock = whole_second_clock
    completed = _complete(repository, suffix="002")
    later_with_fraction = aware_datetime_to_rfc3339(
        parse_rfc3339(completed.outcome.measured_at)
        + timedelta(microseconds=1)
    )
    valid = _attribution(
        completed.outcome,
        recorded_at=later_with_fraction,
        reason="Fractional instant after whole second.",
    )
    assert repository.put_attribution(valid).inserted is True


def test_sqlite_outcome_attribution_schema_and_pragma_drift_fail_closed():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    repository._connection.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        repository.put_attribution(attribution)
    repository._connection.execute("PRAGMA recursive_triggers = ON")
    repository._connection.execute(
        "CREATE INDEX unexpected_attribution_index "
        "ON v3_outcome_attributions (effect)"
    )
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        repository.put_attribution(attribution)


def test_sqlite_outcome_attribution_outer_transaction_uses_savepoint():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    connection = repository._connection
    connection.execute("CREATE TABLE caller_state (value TEXT)")
    connection.execute("INSERT INTO caller_state VALUES ('before')")
    assert repository.put_attribution(attribution).inserted is True
    connection.execute("INSERT INTO caller_state VALUES ('after')")
    connection.rollback()
    assert connection.execute(
        "SELECT count(*) FROM caller_state"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM v3_outcome_attributions"
    ).fetchone() == (0,)


def test_sqlite_outcome_attribution_concurrent_exact_replay(tmp_path: Path):
    import trace_backed_memory as tbm

    database = tmp_path / "attribution.sqlite3"
    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        database,
        initialize=True,
        timeout=5,
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    repository.close()

    def write_once():
        current = tbm.SQLiteOutcomeAttributionV3Repository.connect(
            database,
            timeout=5,
        )
        try:
            return current.put_attribution(attribution)
        finally:
            current.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: write_once(), range(2)))
    assert sorted(result.inserted for result in results) == [False, True]


def test_sqlite_outcome_attribution_errors_exports_and_resource():
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    _complete(repository)
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3NotFoundError):
        repository.get_attribution(
            "outcome_attribution_sha256_" + "f" * 64
        )
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3NotFoundError):
        repository.list_attributions("run_outcome_sha256_" + "f" * 64)
    with pytest.raises(TypeError):
        repository.put_attribution(object())
    with pytest.raises(ValueError):
        repository.get_attribution(1)
    with pytest.raises(ValueError):
        repository.list_attributions(1)
    assert tbm.SQLITE_OUTCOME_ATTRIBUTION_V3_SCHEMA_VERSION == 1
    assert tbm.read_packaged_resource(
        "schemas/sqlite-v3-outcome-attribution.sql"
    ) == SCHEMA.read_bytes()
    repository.close()
    repository.close()
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
        repository.get_attribution(
            "outcome_attribution_sha256_" + "f" * 64
        )


def test_sqlite_outcome_attribution_missing_and_temp_schema():
    import trace_backed_memory as tbm

    with pytest.raises(ValueError):
        tbm.SQLiteOutcomeAttributionV3Repository(None)
    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect()
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        repository.get_attribution(
            "outcome_attribution_sha256_" + "f" * 64
        )
    repository.close()

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    repository._connection.execute(
        "CREATE TEMP TABLE v3_outcome_attributions (value TEXT)"
    )
    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3SchemaError,
        match="temporary shadow",
    ):
        repository.list_attributions(completed.outcome.run_outcome_id)


def test_sqlite_outcome_attribution_private_helpers_and_conflicts(
    monkeypatch: pytest.MonkeyPatch,
):
    import trace_backed_memory as tbm

    assert attribution_module._validate_attribution_descriptor(None) == 0
    assert attribution_module._validate_attribution_descriptor("{}") == 0
    assert (
        attribution_module._time_not_before(
            "2099-01-01T00:00:00Z",
            "2099-01-01T00:00:00.100000Z",
        )
        == 0
    )
    assert attribution_module._time_not_before("invalid", "invalid") == 0
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        attribution_module._normalized_schema_sql(None)
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
        tbm.SQLiteOutcomeAttributionV3Repository._attribution_from_row(())

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    requested = _attribution(completed.outcome)
    different = _attribution(
        completed.outcome,
        reason="Different content.",
        recorded_at=_after(completed.outcome.measured_at, 2),
    )
    with monkeypatch.context() as patch:
        patch.setattr(repository, "_select_optional", lambda *_args: different)
        with pytest.raises(tbm.SQLiteOutcomeAttributionV3ConflictError):
            repository.put_attribution(requested)

    original_select = repository._select
    calls = 0

    def changed(cursor, attribution_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return different
        return original_select(cursor, attribution_id)

    with monkeypatch.context() as patch:
        patch.setattr(repository, "_select", changed)
        with pytest.raises(
            tbm.SQLiteOutcomeAttributionV3PersistenceError,
            match="read-back",
        ):
            repository.put_attribution(requested)
    assert repository.list_attributions(
        completed.outcome.run_outcome_id
    ) == ()


def test_sqlite_outcome_attribution_schema_helpers_and_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import trace_backed_memory as tbm

    with pytest.raises(ValueError):
        tbm.SQLiteOutcomeAttributionV3Repository.connect(initialize=1)
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
        tbm.SQLiteOutcomeAttributionV3Repository.connect(tmp_path)

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    with repository as opened:
        assert opened.outcomes is repository.outcomes
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
        repository.__enter__()
    repository.__exit__(None, None, None)

    attribution_module._canonical_schema_definitions.cache_clear()
    monkeypatch.setattr(
        attribution_module,
        "read_packaged_resource",
        lambda _name: b"",
    )
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        attribution_module._canonical_schema_definitions()
    attribution_module._canonical_schema_definitions.cache_clear()
    monkeypatch.setattr(
        attribution_module,
        "read_packaged_resource",
        lambda _name: b"\xff",
    )
    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3SchemaError,
        match="could not validate",
    ):
        attribution_module._canonical_schema_definitions()
    attribution_module._canonical_schema_definitions.cache_clear()

    class MissingCursor:
        def execute(self, *_args):
            return None

        def fetchall(self):
            return []

    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        attribution_module._read_schema_definitions(MissingCursor())

    class BrokenDefinitionCursor:
        calls = 0

        def execute(self, *_args):
            self.calls += 1

        def fetchall(self):
            return [(1, "name", "table", "sql")] * len(
                attribution_module._SCHEMA_OBJECT_NAMES
            )

        def fetchone(self):
            return None

    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3SchemaError,
        match="invalid shape",
    ):
        attribution_module._read_schema_definitions(
            BrokenDefinitionCursor()
        )


def test_sqlite_outcome_attribution_database_error_mapping():
    import trace_backed_memory as tbm

    with pytest.raises(tbm.SQLiteOutcomeAttributionV3SchemaError):
        tbm.SQLiteOutcomeAttributionV3Repository._raise_database_error(
            sqlite3.OperationalError("no such table: missing"),
            "ignored",
        )
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
        tbm.SQLiteOutcomeAttributionV3Repository._raise_database_error(
            sqlite3.DatabaseError("database failure"),
            "bounded failure",
        )


def test_sqlite_outcome_attribution_corrupt_rows_and_dependency_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    row = list(repository._attribution_row(attribution))
    row[-1] = "{}"
    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3PersistenceError,
        match="contract validation",
    ):
        repository._attribution_from_row(tuple(row))
    row = list(repository._attribution_row(attribution))
    row[12] = "changed"
    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3PersistenceError,
        match="columns",
    ):
        repository._attribution_from_row(tuple(row))

    dependency_error = tbm.SQLiteOutcomeV3PersistenceError(
        "TBM_TEST",
        "corrupt outcome",
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            repository._outcomes,
            "_select_outcome",
            lambda *_args: (_ for _ in ()).throw(dependency_error),
        )
        with pytest.raises(
            tbm.SQLiteOutcomeAttributionV3PersistenceError
        ) as mapped:
            repository.put_attribution(attribution)
        assert mapped.value.code == (
            "TBM_SQLITE_OUTCOME_ATTRIBUTION_DEPENDENCY"
        )
        assert mapped.value.__cause__ is dependency_error

    connection = repository._connection
    connection.execute("CREATE TABLE caller_failure_state (value TEXT)")
    connection.execute("INSERT INTO caller_failure_state VALUES ('before')")
    invalid = _attribution(
        completed.outcome,
        memory_revision_ids=("memory_revision_sha256_" + "f" * 64,),
    )
    with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
        repository.put_attribution(invalid)
    connection.execute("INSERT INTO caller_failure_state VALUES ('after')")
    assert connection.execute(
        "SELECT value FROM caller_failure_state ORDER BY value"
    ).fetchall() == [("after",), ("before",)]
    connection.rollback()


def test_sqlite_outcome_attribution_metadata_and_database_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    import trace_backed_memory as tbm

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    attribution = _attribution(completed.outcome)
    connection = repository._connection
    connection.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3SchemaError,
        match="foreign keys",
    ):
        repository.put_attribution(attribution)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "DROP TRIGGER v3_outcome_attribution_schema_immutable_delete"
    )
    connection.execute(
        "DELETE FROM trace_backed_memory_v3_outcome_attribution_schema"
    )
    with pytest.raises(
        tbm.SQLiteOutcomeAttributionV3SchemaError,
        match="metadata mismatch",
    ):
        repository.put_attribution(attribution)

    repository = tbm.SQLiteOutcomeAttributionV3Repository.connect(
        initialize=True
    )
    completed = _complete(repository)
    with monkeypatch.context() as patch:
        patch.setattr(
            repository,
            "_select",
            lambda *_args: (_ for _ in ()).throw(
                sqlite3.DatabaseError("read failure")
            ),
        )
        with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
            repository.get_attribution(
                "outcome_attribution_sha256_" + "f" * 64
            )
    with monkeypatch.context() as patch:
        patch.setattr(
            repository._outcomes,
            "_select_outcome",
            lambda *_args: (_ for _ in ()).throw(
                sqlite3.DatabaseError("list failure")
            ),
        )
        with pytest.raises(tbm.SQLiteOutcomeAttributionV3PersistenceError):
            repository.list_attributions(completed.outcome.run_outcome_id)
