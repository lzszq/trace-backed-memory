from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_authorization_v3 as postgres_authorization_v3
from trace_backed_memory.authorization_v3 import (
    AgentClientIdentity,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    PrincipalIdentity,
    RepositoryAlias,
    RepositoryTenantBinding,
    RoleBinding,
    authorize,
)
from trace_backed_memory.contracts_v3 import AuthorizationScope, CanonicalRepository
from trace_backed_memory.postgres_authorization_v3 import (
    PostgresAuthorizationV3ConflictError,
    PostgresAuthorizationV3PersistenceError,
    PostgresAuthorizationV3Repository,
    PostgresAuthorizationV3SchemaError,
)
from tests.postgres_support import PostgresCluster, assert_sql_succeeds


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-authorization.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-authorization-rollback.sql"
DIGEST = "sha256:" + "a" * 64
NOW = "2026-07-28T00:00:00Z"


class _RowsCursor:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def execute(self, _query: str, _parameters: tuple[object, ...] = ()) -> None:
        return None

    def fetchall(self) -> list[object]:
        return self.rows


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    result = cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr


def _policy(*, version: str = "policy_001") -> AuthorizationPolicyBundle:
    repository = CanonicalRepository(
        repository_id="repository_001",
        provider="local",
        provider_repository_id="provider_repository_001",
        canonical_locator_hash=DIGEST,
        display_name="Repository",
    )
    return AuthorizationPolicyBundle(
        policy_version=version,
        principals=(
            PrincipalIdentity(
                principal_id="principal_001",
                issuer="https://identity.example.test",
                subject_hash=DIGEST,
                tenant_id="tenant_001",
            ),
        ),
        agent_clients=(
            AgentClientIdentity(
                agent_client_id="agent_client_001",
                tenant_id="tenant_001",
                client_kind="service",
            ),
        ),
        repositories=(repository,),
        repository_tenants=(
            RepositoryTenantBinding(
                repository_id=repository.repository_id,
                tenant_id="tenant_001",
            ),
        ),
        repository_aliases=(
            RepositoryAlias(
                alias="owner/repository",
                repository_id=repository.repository_id,
                tenant_id="tenant_001",
                source="operator_registry",
            ),
        ),
        role_bindings=(
            RoleBinding(
                binding_id="binding_001",
                principal_id="principal_001",
                agent_client_id="agent_client_001",
                role_name="repository_reader",
                scope=AuthorizationScope(
                    kind="repository",
                    tenant_id="tenant_001",
                    repository_id=repository.repository_id,
                ),
                permissions=("memory:retrieve",),
                status="active",
                valid_from=NOW,
            ),
        ),
    )


def _request(*, request_id: str = "request_001") -> AuthorizationRequest:
    return AuthorizationRequest(
        request_id=request_id,
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        tenant_id="tenant_001",
        repository_reference="owner/repository",
        permission="memory:retrieve",
        requested_at=NOW,
    )


def _repository(cluster: PostgresCluster) -> PostgresAuthorizationV3Repository:
    return PostgresAuthorizationV3Repository.connect(**cluster.connection_kwargs())


def test_postgres_authorization_v3_public_exports_are_intentional():
    assert tbm.PostgresAuthorizationV3Repository is PostgresAuthorizationV3Repository
    assert tbm.POSTGRES_AUTHORIZATION_V3_SCHEMA_VERSION == 1
    assert "PostgresAuthorizationV3Repository" in tbm.__all__


def test_postgres_authorization_canonical_function_resource_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    postgres_authorization_v3._expected_function_bodies.cache_clear()
    monkeypatch.setattr(
        postgres_authorization_v3,
        "read_packaged_resource",
        lambda _name: b"",
    )
    with pytest.raises(PostgresAuthorizationV3SchemaError):
        postgres_authorization_v3._expected_function_bodies()

    postgres_authorization_v3._expected_function_bodies.cache_clear()

    monkeypatch.setattr(
        postgres_authorization_v3,
        "read_packaged_resource",
        lambda _name: b"\xff",
    )
    with pytest.raises(PostgresAuthorizationV3SchemaError):
        postgres_authorization_v3._expected_function_bodies()
    postgres_authorization_v3._expected_function_bodies.cache_clear()


def test_postgres_authorization_defensive_row_and_error_validation():
    policy = _policy()
    request = _request()
    decision = authorize(policy, request, decided_at="2026-07-28T00:00:01Z")
    policy_values = PostgresAuthorizationV3Repository._policy_values(policy)
    decision_values = PostgresAuthorizationV3Repository._decision_values(decision)

    with pytest.raises(PostgresAuthorizationV3SchemaError):
        PostgresAuthorizationV3Repository._catalog_names(
            _RowsCursor([{"name": 1}]), "SELECT"
        )
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._stored_policy({"descriptor": 1})
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._stored_policy(
            {
                "policy_sha256": policy_values[0],
                "policy_version": policy_values[1],
                "descriptor": "{}",
            }
        )
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._stored_policy(
            {
                "policy_sha256": "sha256:" + "b" * 64,
                "policy_version": policy_values[1],
                "descriptor": policy_values[2],
            }
        )
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._stored_decision({"descriptor": 1})
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._stored_decision({"descriptor": "{}"})
    decision_names = (
        "authorization_event_id",
        "request_id",
        "request_sha256",
        "policy_sha256",
        "principal_id",
        "agent_client_id",
        "tenant_id",
        "repository_id",
        "permission",
        "allowed",
        "reason",
        "decided_at",
        "descriptor",
    )
    mismatched = dict(zip(decision_names, decision_values, strict=True))
    mismatched["reason"] = "changed"
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._stored_decision(mismatched)
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._select_one(
            _RowsCursor([{}, {}]), "SELECT", ()
        )

    class _DatabaseError(Exception):
        def __init__(self, sqlstate: str | None) -> None:
            self.sqlstate = sqlstate

    with pytest.raises(PostgresAuthorizationV3SchemaError):
        PostgresAuthorizationV3Repository._raise_database_error(
            _DatabaseError("42P01"), "failed"
        )
    with pytest.raises(PostgresAuthorizationV3ConflictError):
        PostgresAuthorizationV3Repository._raise_database_error(
            _DatabaseError("23505"), "failed"
        )
    with pytest.raises(PostgresAuthorizationV3ConflictError):
        PostgresAuthorizationV3Repository._raise_database_error(
            _DatabaseError("P0001"), "failed"
        )
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        PostgresAuthorizationV3Repository._raise_database_error(
            _DatabaseError(None), "failed"
        )


def test_postgres_authorization_schema_install_and_fail_closed_rollback(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_authorization.schema_metadata",
        )
        == "1|tbm.authorization.v3"
    )
    assert postgres_cluster.run_script(INSTALL).returncode != 0

    assert_sql_succeeds(
        postgres_cluster,
        "ALTER INDEX "
        "trace_backed_memory_v3_authorization.authorization_decisions_policy "
        "RENAME TO authorization_decisions_policy_drift",
    )
    failed = postgres_cluster.run_script(ROLLBACK)
    assert failed.returncode != 0
    assert "catalog mismatch" in failed.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER INDEX "
        "trace_backed_memory_v3_authorization.authorization_decisions_policy_drift "
        "RENAME TO authorization_decisions_policy",
    )
    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr


def test_postgres_authorization_authorizes_persists_and_replays(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    policy = _policy()
    request = _request()
    with _repository(postgres_cluster) as repository:
        decision, result = repository.authorize_and_record(
            policy, request, decided_at="2026-07-28T00:00:01Z"
        )
        assert decision.allowed is True
        assert result.policy_inserted is True
        assert result.decision_inserted is True
        assert repository.load_policy(policy.policy_sha256) == policy
        assert repository.load_decision(decision.authorization_event_id) == decision
        assert repository.list_decisions(policy.policy_sha256) == (decision,)

        replayed, replay_result = repository.authorize_and_record(
            policy, request, decided_at="2026-07-28T00:00:01Z"
        )
        assert replayed == decision
        assert replay_result.policy_inserted is False
        assert replay_result.decision_inserted is False


def test_postgres_authorization_denial_and_conflicts_are_durable(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    policy = _policy()
    denied_request = replace(_request(), principal_id="missing_principal")
    first = authorize(policy, denied_request, decided_at="2026-07-28T00:00:01Z")
    conflicting = authorize(policy, denied_request, decided_at="2026-07-28T00:00:02Z")
    changed_policy = replace(
        policy,
        repository_aliases=(
            RepositoryAlias(
                alias="different/repository",
                repository_id="repository_001",
                tenant_id="tenant_001",
                source="operator_registry",
            ),
        ),
    )
    with _repository(postgres_cluster) as repository:
        result = repository.append_decision(policy, denied_request, first)
        assert result.decision_inserted is True
        assert repository.load_decision(first.authorization_event_id).allowed is False
        with pytest.raises(PostgresAuthorizationV3ConflictError):
            repository.append_decision(policy, denied_request, conflicting)
        with pytest.raises(PostgresAuthorizationV3ConflictError):
            repository.store_policy(changed_policy)
        assert repository.list_decisions(policy.policy_sha256) == (first,)


def test_postgres_authorization_exact_request_verification_is_required(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    policy = _policy()
    request = _request()
    decision = authorize(policy, request, decided_at="2026-07-28T00:00:01Z")
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuthorizationV3ConflictError):
            repository.append_decision(
                policy,
                _request(request_id="request_other"),
                decision,
            )
        assert repository.store_policy(policy) is True
        assert repository.store_policy(policy) is False


def test_postgres_authorization_preserves_caller_transaction_savepoint(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = tbm.postgres._load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(), row_factory=dict_row
    )
    try:
        repository = PostgresAuthorizationV3Repository(connection)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_work (value integer)")
                cursor.execute("INSERT INTO caller_work VALUES (1)")
            repository.store_policy(_policy())
            with pytest.raises(KeyError):
                repository.load_policy("sha256:" + "b" * 64)
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_work")
                assert cursor.fetchall() == [{"value": 1}]
    finally:
        connection.close()


def test_postgres_authorization_schema_drift_and_direct_mutation_fail_closed(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    policy = _policy()
    with _repository(postgres_cluster) as repository:
        repository.store_policy(policy)
        assert (
            postgres_cluster.run(
                "UPDATE trace_backed_memory_v3_authorization.authorization_policies "
                "SET policy_version = 'changed'"
            ).returncode
            != 0
        )
        assert (
            postgres_cluster.run(
                "DELETE FROM "
                "trace_backed_memory_v3_authorization.authorization_policies"
            ).returncode
            != 0
        )
        assert (
            postgres_cluster.run(
                "TRUNCATE trace_backed_memory_v3_authorization.authorization_policies"
            ).returncode
            != 0
        )
        assert_sql_succeeds(
            postgres_cluster,
            "ALTER INDEX "
            "trace_backed_memory_v3_authorization.authorization_decisions_principal "
            "RENAME TO authorization_decisions_principal_drift",
        )
        with pytest.raises(PostgresAuthorizationV3SchemaError):
            repository.load_policy(policy.policy_sha256)


def test_postgres_authorization_rejects_same_name_constraint_weakening(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER TABLE "
        "trace_backed_memory_v3_authorization.authorization_decisions "
        "DROP CONSTRAINT authorization_decisions_request_key; "
        "CREATE INDEX authorization_decisions_request_key ON "
        "trace_backed_memory_v3_authorization.authorization_decisions "
        "(request_id); "
        "ALTER TABLE "
        "trace_backed_memory_v3_authorization.authorization_decisions "
        "ADD CONSTRAINT authorization_decisions_request_key "
        "CHECK (char_length(request_id) BETWEEN 1 AND 128)",
    )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuthorizationV3SchemaError):
            repository.store_policy(_policy())
    failed = postgres_cluster.run_script(ROLLBACK)
    assert failed.returncode != 0
    assert "fingerprint mismatch" in failed.stderr


def test_postgres_authorization_rejects_trigger_shape_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "DROP TRIGGER authorization_policies_immutable ON "
        "trace_backed_memory_v3_authorization.authorization_policies; "
        "CREATE TRIGGER authorization_policies_immutable "
        "BEFORE UPDATE OR DELETE ON "
        "trace_backed_memory_v3_authorization.authorization_policies "
        "FOR EACH ROW WHEN (false) EXECUTE FUNCTION "
        "trace_backed_memory_v3_authorization.reject_immutable_change()",
    )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuthorizationV3SchemaError):
            repository.store_policy(_policy())
    assert postgres_cluster.run_script(ROLLBACK).returncode != 0


def test_postgres_authorization_rejects_function_body_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        """
        CREATE OR REPLACE FUNCTION
        trace_backed_memory_v3_authorization.reject_immutable_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RETURN NEW;
        END
        $$;
        """,
    )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuthorizationV3SchemaError):
            repository.store_policy(_policy())
    failed = postgres_cluster.run_script(ROLLBACK)
    assert failed.returncode != 0
    assert "fingerprint mismatch" in failed.stderr


def test_postgres_authorization_rejects_function_acl_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE ROLE authorization_v3_reader NOLOGIN; "
        "GRANT EXECUTE ON FUNCTION "
        "trace_backed_memory_v3_authorization.reject_immutable_change() "
        "TO authorization_v3_reader",
    )
    try:
        with _repository(postgres_cluster) as repository:
            with pytest.raises(PostgresAuthorizationV3SchemaError):
                repository.store_policy(_policy())
        failed = postgres_cluster.run_script(ROLLBACK)
        assert failed.returncode != 0
        assert "fingerprint mismatch" in failed.stderr
    finally:
        assert_sql_succeeds(
            postgres_cluster,
            "REVOKE EXECUTE ON FUNCTION "
            "trace_backed_memory_v3_authorization.reject_immutable_change() "
            "FROM authorization_v3_reader; "
            "DROP ROLE authorization_v3_reader",
        )


def test_postgres_authorization_rollback_rejects_column_definition_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "ALTER TABLE "
        "trace_backed_memory_v3_authorization.authorization_policies "
        "ALTER COLUMN policy_version DROP NOT NULL",
    )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresAuthorizationV3SchemaError):
            repository.store_policy(_policy())
    failed = postgres_cluster.run_script(ROLLBACK)
    assert failed.returncode != 0
    assert "fingerprint mismatch" in failed.stderr


def test_postgres_authorization_rejects_column_acl_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE ROLE authorization_v3_column_writer NOLOGIN; "
        "GRANT INSERT (policy_sha256, policy_version, descriptor) ON "
        "trace_backed_memory_v3_authorization.authorization_policies "
        "TO authorization_v3_column_writer",
    )
    try:
        with _repository(postgres_cluster) as repository:
            with pytest.raises(PostgresAuthorizationV3SchemaError):
                repository.store_policy(_policy())
        failed = postgres_cluster.run_script(ROLLBACK)
        assert failed.returncode != 0
        assert "fingerprint mismatch" in failed.stderr
    finally:
        assert_sql_succeeds(
            postgres_cluster,
            "REVOKE INSERT (policy_sha256, policy_version, descriptor) ON "
            "trace_backed_memory_v3_authorization.authorization_policies "
            "FROM authorization_v3_column_writer; "
            "DROP ROLE authorization_v3_column_writer",
        )


def test_postgres_authorization_rollback_rejects_empty_trigger_catalog(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        """
        DO $$
        DECLARE trigger_record record;
        BEGIN
            FOR trigger_record IN
                SELECT trigger.tgname, class.relname
                FROM pg_catalog.pg_trigger AS trigger
                JOIN pg_catalog.pg_class AS class
                  ON class.oid = trigger.tgrelid
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname =
                          'trace_backed_memory_v3_authorization'
                  AND NOT trigger.tgisinternal
            LOOP
                EXECUTE pg_catalog.format(
                    'DROP TRIGGER %I ON '
                    'trace_backed_memory_v3_authorization.%I',
                    trigger_record.tgname,
                    trigger_record.relname
                );
            END LOOP;
        END
        $$;
        """,
    )
    failed = postgres_cluster.run_script(ROLLBACK)
    assert failed.returncode != 0
    assert "catalog mismatch" in failed.stderr


def test_postgres_authorization_revalidates_corrupted_stored_descriptors(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    policy = _policy()
    request = _request()
    with _repository(postgres_cluster) as repository:
        decision, _result = repository.authorize_and_record(
            policy, request, decided_at="2026-07-28T00:00:01Z"
        )
        assert_sql_succeeds(
            postgres_cluster,
            "ALTER TABLE "
            "trace_backed_memory_v3_authorization.authorization_policies "
            "DISABLE TRIGGER authorization_policies_immutable; "
            "UPDATE trace_backed_memory_v3_authorization.authorization_policies "
            "SET descriptor = '{}'; "
            "ALTER TABLE "
            "trace_backed_memory_v3_authorization.authorization_policies "
            "ENABLE TRIGGER authorization_policies_immutable",
        )
        with pytest.raises(PostgresAuthorizationV3PersistenceError):
            repository.load_policy(policy.policy_sha256)

        assert_sql_succeeds(
            postgres_cluster,
            "ALTER TABLE "
            "trace_backed_memory_v3_authorization.authorization_decisions "
            "DISABLE TRIGGER authorization_decisions_immutable; "
            "UPDATE "
            "trace_backed_memory_v3_authorization.authorization_decisions "
            "SET descriptor = '{}'; "
            "ALTER TABLE "
            "trace_backed_memory_v3_authorization.authorization_decisions "
            "ENABLE TRIGGER authorization_decisions_immutable",
        )
        with pytest.raises(PostgresAuthorizationV3PersistenceError):
            repository.load_decision(decision.authorization_event_id)


def test_postgres_authorization_missing_schema_closed_and_inputs(
    postgres_cluster: PostgresCluster,
):
    postgres_cluster.load_schema()
    repository = _repository(postgres_cluster)
    with pytest.raises(PostgresAuthorizationV3SchemaError):
        repository.store_policy(_policy())
    repository.close()
    repository.close()
    with pytest.raises(PostgresAuthorizationV3PersistenceError):
        repository.store_policy(_policy())
    with pytest.raises(ValueError):
        PostgresAuthorizationV3Repository(None)

    installed = postgres_cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr
    with _repository(postgres_cluster) as repository:
        with pytest.raises(ValueError):
            repository.store_policy(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            repository.authorize_and_record(  # type: ignore[arg-type]
                object(), _request(), decided_at=NOW
            )
        with pytest.raises(ValueError):
            repository.authorize_and_record(  # type: ignore[arg-type]
                _policy(), object(), decided_at=NOW
            )
        with pytest.raises(ValueError):
            repository.append_decision(  # type: ignore[arg-type]
                _policy(), _request(), object()
            )
        with pytest.raises(ValueError):
            repository.load_policy("invalid")
        with pytest.raises(ValueError):
            repository.load_decision("invalid")
        with pytest.raises(ValueError):
            repository.list_decisions("invalid")
        with pytest.raises(ValueError):
            repository.list_decisions(DIGEST, limit=0)
        with pytest.raises(KeyError):
            repository.load_decision("authz_sha256_" + "b" * 64)


def test_postgres_authorization_concurrent_exact_replay_is_idempotent(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    policy = _policy()
    request = _request()

    def append() -> bool:
        with _repository(postgres_cluster) as repository:
            _decision, result = repository.authorize_and_record(
                policy, request, decided_at="2026-07-28T00:00:01Z"
            )
            return result.decision_inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        inserted = tuple(executor.map(lambda _index: append(), range(2)))
    assert sorted(inserted) == [False, True]
