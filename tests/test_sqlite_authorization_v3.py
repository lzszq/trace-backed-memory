from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_authorization_v3 as sqlite_authorization_v3
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
from trace_backed_memory.sqlite_authorization_v3 import (
    SQLiteAuthorizationV3ConflictError,
    SQLiteAuthorizationV3Error,
    SQLiteAuthorizationV3PersistenceError,
    SQLiteAuthorizationV3Repository,
    SQLiteAuthorizationV3SchemaError,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "sqlite-v3-authorization.sql"
DIGEST = "sha256:" + "a" * 64
NOW = "2026-07-28T00:00:00Z"


def test_sqlite_authorization_v3_public_exports_are_intentional():
    assert tbm.SQLiteAuthorizationV3Repository is SQLiteAuthorizationV3Repository
    assert tbm.SQLITE_AUTHORIZATION_V3_SCHEMA_VERSION == 1
    assert "SQLiteAuthorizationV3Repository" in tbm.__all__


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


def test_sqlite_authorization_v3_authorizes_persists_and_replays():
    policy = _policy()
    request = _request()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        decision, result = repository.authorize_and_record(
            policy,
            request,
            decided_at="2026-07-28T00:00:01Z",
        )
        assert decision.allowed is True
        assert result.policy_inserted is True
        assert result.decision_inserted is True
        assert repository.load_policy(policy.policy_sha256) == policy
        assert repository.load_decision(decision.authorization_event_id) == decision
        assert repository.list_decisions(policy.policy_sha256) == (decision,)

        replayed, replay_result = repository.authorize_and_record(
            policy,
            request,
            decided_at="2026-07-28T00:00:01Z",
        )
        assert replayed == decision
        assert replay_result.policy_inserted is False
        assert replay_result.decision_inserted is False


def test_sqlite_authorization_v3_denial_is_durable():
    policy = _policy()
    request = replace(_request(), principal_id="missing_principal")
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        decision, _result = repository.authorize_and_record(
            policy,
            request,
            decided_at="2026-07-28T00:00:01Z",
        )
        assert decision.allowed is False
        assert decision.reason == "unknown_principal"
        assert repository.load_decision(decision.authorization_event_id) == decision


def test_sqlite_authorization_v3_request_identity_is_unique_and_atomic():
    policy = _policy()
    first = authorize(
        policy,
        _request(),
        decided_at="2026-07-28T00:00:01Z",
    )
    conflicting = authorize(
        policy,
        _request(),
        decided_at="2026-07-28T00:00:02Z",
    )
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        repository.append_decision(policy, _request(), first)
        with pytest.raises(SQLiteAuthorizationV3ConflictError):
            repository.append_decision(policy, _request(), conflicting)
        assert repository.list_decisions(policy.policy_sha256) == (first,)


def test_sqlite_authorization_v3_policy_version_is_immutable():
    first = _policy()
    changed = _policy(version=first.policy_version)
    changed = replace(
        changed,
        repository_aliases=(
            RepositoryAlias(
                alias="different/repository",
                repository_id="repository_001",
                tenant_id="tenant_001",
                source="operator_registry",
            ),
        ),
    )
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        assert repository.store_policy(first) is True
        with pytest.raises(SQLiteAuthorizationV3ConflictError):
            repository.store_policy(changed)
        assert repository.load_policy(first.policy_sha256) == first


def test_sqlite_authorization_v3_uses_caller_savepoints():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    repository = SQLiteAuthorizationV3Repository(connection)
    policy = _policy()
    connection.execute("CREATE TABLE caller_work (value INTEGER)")
    connection.execute("INSERT INTO caller_work VALUES (1)")
    assert repository.store_policy(policy) is True
    with pytest.raises(SQLiteAuthorizationV3ConflictError):
        repository.store_policy(
            replace(
                policy,
                repository_aliases=(
                    RepositoryAlias(
                        alias="changed/repository",
                        repository_id="repository_001",
                        tenant_id="tenant_001",
                        source="operator_registry",
                    ),
                ),
            )
        )
    assert connection.execute("SELECT value FROM caller_work").fetchone() == (1,)
    connection.rollback()
    connection.close()


def test_sqlite_authorization_v3_schema_drift_fails_closed():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("DROP INDEX v3_authorization_decisions_policy")
    repository = SQLiteAuthorizationV3Repository(connection)
    with pytest.raises(SQLiteAuthorizationV3SchemaError):
        repository.store_policy(_policy())
    connection.close()

    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_master SET sql = replace(sql, 'immutable', 'changed') "
        "WHERE name = 'v3_authorization_policies_immutable_update'"
    )
    connection.execute("PRAGMA writable_schema = OFF")
    repository = SQLiteAuthorizationV3Repository(connection)
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="definitions"):
        repository.store_policy(_policy())
    connection.close()


def test_sqlite_authorization_v3_direct_mutation_fails():
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        policy = _policy()
        decision, _result = repository.authorize_and_record(
            policy,
            _request(),
            decided_at="2026-07-28T00:00:01Z",
        )
        connection = repository._connection
        connection.execute("PRAGMA recursive_triggers = OFF")
        for statement, parameters in (
            (
                "UPDATE v3_authorization_policies SET policy_version = 'changed'",
                (),
            ),
            ("DELETE FROM v3_authorization_policies", ()),
            (
                "INSERT OR REPLACE INTO v3_authorization_policies "
                "(policy_sha256, policy_version, descriptor) VALUES (?, ?, ?)",
                SQLiteAuthorizationV3Repository._policy_values(policy),
            ),
            ("UPDATE v3_authorization_decisions SET allowed = 0", ()),
            ("DELETE FROM v3_authorization_decisions", ()),
            (
                "INSERT OR REPLACE INTO v3_authorization_decisions ("
                "authorization_event_id, request_id, request_sha256, "
                "policy_sha256, principal_id, agent_client_id, tenant_id, "
                "repository_id, permission, allowed, reason, decided_at, "
                "descriptor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                SQLiteAuthorizationV3Repository._decision_values(decision),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement, parameters)
        connection.execute("PRAGMA recursive_triggers = ON")
        assert repository.load_decision(decision.authorization_event_id) == decision


def test_sqlite_authorization_v3_validates_inputs_and_missing_records():
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        with pytest.raises(ValueError, match="exactly"):
            repository.store_policy(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="canonical digest"):
            repository.load_policy("bad")
        with pytest.raises(ValueError, match="canonical"):
            repository.load_decision("bad")
        with pytest.raises(ValueError, match="limit"):
            repository.list_decisions(DIGEST, limit=0)
        with pytest.raises(KeyError):
            repository.load_policy(DIGEST)
        with pytest.raises(KeyError):
            repository.load_decision("authz_sha256_" + "f" * 64)


def test_sqlite_authorization_v3_revalidates_stored_rows():
    policy = _policy()
    policy_row = SQLiteAuthorizationV3Repository._policy_values(policy)
    assert SQLiteAuthorizationV3Repository._stored_policy(policy_row) == policy
    for row, message in (
        ((policy.policy_sha256,), "invalid shape"),
        ((policy.policy_sha256, policy.policy_version, "{"), "failed validation"),
        (
            ("sha256:" + "b" * 64, policy.policy_version, policy_row[2]),
            "do not match",
        ),
    ):
        with pytest.raises(SQLiteAuthorizationV3PersistenceError, match=message):
            SQLiteAuthorizationV3Repository._stored_policy(row)

    request = _request()
    decision = authorize(
        policy,
        request,
        decided_at="2026-07-28T00:00:01Z",
    )
    decision_row = SQLiteAuthorizationV3Repository._decision_values(decision)
    assert SQLiteAuthorizationV3Repository._stored_decision(decision_row) == decision
    for row, message in (
        ((decision.authorization_event_id,), "invalid shape"),
        ((*decision_row[:-1], "{"), "failed validation"),
        (
            (
                decision.authorization_event_id,
                "changed_request",
                *decision_row[2:],
            ),
            "do not match",
        ),
    ):
        with pytest.raises(SQLiteAuthorizationV3PersistenceError, match=message):
            SQLiteAuthorizationV3Repository._stored_decision(row)

    with pytest.raises(SQLiteAuthorizationV3ConflictError, match="verify"):
        with SQLiteAuthorizationV3Repository.connect(
            initialize=True,
        ) as repository:
            repository.append_decision(
                policy,
                replace(request, request_id="other_request"),
                decision,
            )


def test_sqlite_authorization_v3_schema_and_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        SQLiteAuthorizationV3Repository(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="boolean"):
        SQLiteAuthorizationV3Repository.connect(initialize=1)  # type: ignore[arg-type]

    missing = SQLiteAuthorizationV3Repository.connect()
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="missing"):
        missing.store_policy(_policy())
    missing.close()
    with pytest.raises(SQLiteAuthorizationV3Error, match="closed"):
        missing.store_policy(_policy())
    missing.close()

    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("PRAGMA foreign_keys = OFF")
    repository = SQLiteAuthorizationV3Repository(connection)
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="foreign keys"):
        repository.store_policy(_policy())
    connection.close()

    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.execute("PRAGMA recursive_triggers = OFF")
    repository = SQLiteAuthorizationV3Repository(connection)
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="recursive triggers"):
        repository.store_policy(_policy())
    connection.close()

    sqlite_authorization_v3._canonical_schema_definitions.cache_clear()

    def unreadable(_name: str) -> bytes:
        raise sqlite_authorization_v3.PackagedResourceError(
            "read",
            name="schemas/sqlite-v3-authorization.sql",
        )

    monkeypatch.setattr(
        sqlite_authorization_v3,
        "read_packaged_resource",
        unreadable,
    )
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="canonical"):
        sqlite_authorization_v3._canonical_schema_definitions()
    sqlite_authorization_v3._canonical_schema_definitions.cache_clear()


def test_sqlite_authorization_v3_error_mapping_and_schema_normalization():
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="invalid definition"):
        sqlite_authorization_v3._normalized_schema_sql(None)
    assert sqlite_authorization_v3._normalized_schema_sql(" SELECT 1 ") == "select1"

    class InvalidSchemaCursor:
        def execute(self, *_args: object) -> None:
            pass

        def fetchall(self) -> list[tuple[object, ...]]:
            return [(None, "name", "table", "CREATE TABLE value (id INTEGER)")] * len(
                sqlite_authorization_v3._SCHEMA_OBJECT_NAMES
            )

    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="invalid shape"):
        sqlite_authorization_v3._read_schema_definitions(InvalidSchemaCursor())  # type: ignore[arg-type]
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="missing"):
        SQLiteAuthorizationV3Repository._raise_database_error(
            sqlite3.OperationalError("no such table"),
            "failed",
        )
    with pytest.raises(SQLiteAuthorizationV3PersistenceError, match="failed"):
        SQLiteAuthorizationV3Repository._raise_database_error(
            sqlite3.OperationalError("disk I/O"),
            "failed",
        )
    connection = sqlite3.connect(":memory:")
    repository = SQLiteAuthorizationV3Repository(connection)
    repository._rollback_connection_or_close(
        RuntimeError("primary"),
        context="idle connection",
    )
    connection.close()


def test_sqlite_authorization_v3_defensive_public_branches(
    monkeypatch: pytest.MonkeyPatch,
):
    policy = _policy()
    request = _request()
    with SQLiteAuthorizationV3Repository.connect(
        initialize=True,
    ) as repository:
        with pytest.raises(ValueError, match="exactly"):
            repository.authorize_and_record(  # type: ignore[arg-type]
                object(),
                request,
                decided_at=NOW,
            )
        with pytest.raises(ValueError, match="exactly"):
            repository.authorize_and_record(  # type: ignore[arg-type]
                policy,
                object(),
                decided_at=NOW,
            )
        with pytest.raises(ValueError, match="exact authorization"):
            repository.append_decision(  # type: ignore[arg-type]
                policy,
                request,
                object(),
            )
        with pytest.raises(ValueError, match="canonical digest"):
            repository.list_decisions("bad")

        other_policy = _policy(version="policy_002")
        other_decision = authorize(
            other_policy,
            request,
            decided_at="2026-07-28T00:00:01Z",
        )
        monkeypatch.setattr(
            sqlite_authorization_v3,
            "verify_authorization_decision",
            lambda *_args: None,
        )
        with pytest.raises(
            SQLiteAuthorizationV3ConflictError, match="different policy"
        ):
            repository.append_decision(policy, request, other_decision)

    connection = sqlite3.connect(":memory:")
    repository = SQLiteAuthorizationV3Repository(connection)
    connection.close()
    with pytest.raises(SQLiteAuthorizationV3Error, match="closed"):
        repository.store_policy(policy)

    metadata = sqlite3.connect(":memory:")
    metadata.execute(
        "CREATE TABLE trace_backed_memory_v3_authorization_schema ("
        "singleton INTEGER, schema_version INTEGER, contract_version TEXT)"
    )
    metadata.execute(
        "INSERT INTO trace_backed_memory_v3_authorization_schema VALUES "
        "(1, 2, 'tbm.authorization.v3')"
    )
    with pytest.raises(SQLiteAuthorizationV3SchemaError, match="metadata"):
        SQLiteAuthorizationV3Repository(metadata).store_policy(policy)
    metadata.close()


def test_sqlite_authorization_v3_descriptor_storage_bounds(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(sqlite_authorization_v3, "AUTHORIZATION_JSON_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="policy descriptor"):
        SQLiteAuthorizationV3Repository._policy_values(_policy())
    decision = authorize(
        _policy(),
        _request(),
        decided_at="2026-07-28T00:00:01Z",
    )
    with pytest.raises(ValueError, match="decision descriptor"):
        SQLiteAuthorizationV3Repository._decision_values(decision)


def test_sqlite_authorization_v3_concurrent_exact_replay_is_idempotent(
    tmp_path: Path,
):
    database = tmp_path / "authorization.sqlite3"
    with SQLiteAuthorizationV3Repository.connect(
        database,
        initialize=True,
    ):
        pass
    policy = _policy()
    request = _request()

    def append() -> bool:
        with SQLiteAuthorizationV3Repository.connect(
            database,
            timeout=5,
        ) as repository:
            _decision, result = repository.authorize_and_record(
                policy,
                request,
                decided_at="2026-07-28T00:00:01Z",
            )
            return result.decision_inserted

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _value: append(), range(2)))
    assert sorted(results) == [False, True]


def test_sqlite_authorization_v3_ddl_rejects_non_hex_identities():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    policy = _policy()
    policy_values = list(SQLiteAuthorizationV3Repository._policy_values(policy))
    policy_values[0] = "sha256:a" + "Z" * 63
    policy_values[1] = "malformed_policy"
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO v3_authorization_policies "
            "(policy_sha256, policy_version, descriptor) VALUES (?, ?, ?)",
            policy_values,
        )

    connection.execute(
        "INSERT INTO v3_authorization_policies "
        "(policy_sha256, policy_version, descriptor) VALUES (?, ?, ?)",
        SQLiteAuthorizationV3Repository._policy_values(policy),
    )
    decision = authorize(
        policy,
        _request(),
        decided_at="2026-07-28T00:00:01Z",
    )
    decision_values = list(SQLiteAuthorizationV3Repository._decision_values(decision))
    decision_values[0] = "authz_sha256_a" + "Z" * 63
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO v3_authorization_decisions ("
            "authorization_event_id, request_id, request_sha256, "
            "policy_sha256, principal_id, agent_client_id, tenant_id, "
            "repository_id, permission, allowed, reason, decided_at, descriptor"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            decision_values,
        )
    connection.close()


def test_sqlite_authorization_v3_schema_install_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = SCHEMA.read_text(encoding="utf-8").replace(
        "\nCOMMIT;",
        "\nTHIS IS INVALID;\nCOMMIT;",
    )
    monkeypatch.setattr(
        sqlite_authorization_v3,
        "read_packaged_resource",
        lambda _name: source.encode(),
    )
    database = tmp_path / "failed-install.sqlite3"
    with pytest.raises(SQLiteAuthorizationV3PersistenceError, match="connect"):
        SQLiteAuthorizationV3Repository.connect(database, initialize=True)
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE name = 'trace_backed_memory_v3_authorization_schema' "
        "OR name LIKE 'v3_authorization_%'"
    ).fetchone() == (0,)
    connection.close()
