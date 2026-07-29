from __future__ import annotations

from dataclasses import replace
import hashlib
import sqlite3

import pytest

import trace_backed_memory as tbm


NOW = "2026-07-29T00:00:00Z"
DIGEST = "sha256:" + "a" * 64


class _Provider:
    provider_id = "test_aead"
    algorithm = "TEST-AEAD-SHA256"

    def encrypt(self, plaintext: bytes, *, key_id: str, aad: bytes) -> tuple[bytes, bytes]:
        nonce = b"fixed-test-nonce"
        mask = hashlib.sha256(key_id.encode() + nonce + aad).digest()
        body = bytes(value ^ mask[index % len(mask)] for index, value in enumerate(plaintext))
        tag = hashlib.sha256(key_id.encode() + aad + nonce + body).digest()
        return body + tag, nonce

    def decrypt(self, ciphertext: bytes, *, key_id: str, nonce: bytes, aad: bytes) -> bytes:
        body, tag = ciphertext[:-32], ciphertext[-32:]
        if hashlib.sha256(key_id.encode() + aad + nonce + body).digest() != tag:
            raise ValueError("authentication failed")
        mask = hashlib.sha256(key_id.encode() + nonce + aad).digest()
        return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(body))


def _registry(*, permissions: tuple[str, ...] = ("artifact:read", "artifact:write")) -> tbm.EntityRegistrySnapshot:
    principal = tbm.PrincipalIdentity(
        principal_id="principal_001", issuer="https://id.example.test",
        subject_hash=DIGEST, tenant_id="tenant_001",
    )
    client = tbm.AgentClientIdentity(
        agent_client_id="client_001", tenant_id="tenant_001", client_kind="service"
    )
    repository = tbm.CanonicalRepository(
        repository_id="repository_001", provider="local",
        provider_repository_id="provider_repository_001",
        canonical_locator_hash=DIGEST, display_name="Repository",
    )
    policy = tbm.AuthorizationPolicyBundle(
        policy_version="policy_001", principals=(principal,), agent_clients=(client,),
        repositories=(repository,),
        repository_tenants=(tbm.RepositoryTenantBinding(repository.repository_id, "tenant_001"),),
        repository_aliases=(tbm.RepositoryAlias("owner/repository", repository.repository_id, "tenant_001", "operator_registry"),),
        role_bindings=(tbm.RoleBinding(
            binding_id="binding_001", principal_id=principal.principal_id,
            agent_client_id=client.agent_client_id, role_name="artifact_operator",
            scope=tbm.AuthorizationScope(kind="repository", tenant_id="tenant_001", repository_id=repository.repository_id),
            permissions=permissions, status="active", valid_from=NOW,
        ),),
    )
    return tbm.EntityRegistrySnapshot(
        registry_version="registry_001",
        organizations=(tbm.OrganizationIdentity("organization_001", "Organization"),),
        tenants=(tbm.TenantEntity("tenant_001", "organization_001", "Tenant"),),
        environments=(tbm.EnvironmentIdentity(
            environment_id="environment_001", tenant_id="tenant_001",
            repository_id="repository_001", environment_kind="production",
            display_name="Production",
        ),), authorization_policy=policy,
    )


def _context(registry: tbm.EntityRegistrySnapshot) -> tbm.AuthenticatedServiceContext:
    return tbm.AuthenticatedServiceContext(
        principal=registry.authorization_policy.principals[0],
        agent_client=registry.authorization_policy.agent_clients[0],
        tenant_id="tenant_001", repository_reference="owner/repository",
        environment_id="environment_001",
    )


def _artifact(content: bytes = b"secret artifact") -> tbm.ContentAddressedArtifact:
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return tbm.ContentAddressedArtifact(
        artifact_id=tbm.artifact_id_from_sha256(digest), content_sha256=digest,
        size_bytes=len(content), media_type="application/octet-stream",
        classification="restricted", created_at=NOW, encryption_key_id="key_001",
    )


def _service(
    registry: tbm.EntityRegistrySnapshot,
    artifact_repository: tbm.SQLiteArtifactV3Repository,
    authorization_repository: tbm.SQLiteAuthorizationV3Repository,
    *, clock=lambda: NOW,
    provider=None,
) -> tbm.AuthenticatedArtifactService:
    request_number = iter(range(1, 100))
    authorization = tbm.AuthenticatedRetrievalService(
        registry_provider=lambda: registry, decision_writer=authorization_repository,
        clock=lambda: NOW,
        request_id_factory=lambda: f"request_{next(request_number):03d}",
    )
    return tbm.AuthenticatedArtifactService(
        authorization_service=authorization, authority=artifact_repository,
        encryption_provider=_Provider() if provider is None else provider, clock=clock,
    )


def test_authenticated_artifact_service_encrypts_authorizes_and_replays():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth)
        descriptor = _artifact()
        result = service.put(_context(registry), descriptor, b"secret artifact")
        assert result.inserted is True
        assert result.record.ciphertext != b"secret artifact"
        assert b"secret artifact" not in result.record.ciphertext
        replayed = service.put(_context(registry), descriptor, b"secret artifact")
        assert replayed == tbm.StoredArtifactResult(record=result.record, inserted=False)
        assert service.get(_context(registry), descriptor.artifact_id) == b"secret artifact"
        permissions = tuple(decision.permission for decision in auth.list_decisions(registry.authorization_policy.policy_sha256))
        assert permissions.count("artifact:write") == 2
        assert permissions.count("artifact:read") == 1


def test_authorization_denial_happens_before_artifact_lookup():
    registry = _registry(permissions=("artifact:write",))
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth)
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.get(_context(registry), _artifact().artifact_id)
        assert caught.value.code == "TBM_SERVICE_AUTHORIZATION_DENIED"


def test_expired_artifact_is_denied_but_legal_hold_remains_readable():
    registry = _registry()
    times = iter((NOW, "2026-07-31T00:00:00Z", NOW, "2026-07-31T00:00:00Z"))
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth, clock=lambda: next(times))
        descriptor = _artifact()
        service.put(_context(registry), descriptor, b"secret artifact", retention=tbm.ArtifactRetention("2026-07-30T00:00:00Z"))
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.get(_context(registry), descriptor.artifact_id)
        assert caught.value.code == "TBM_ARTIFACT_RETENTION_EXPIRED"

    held = replace(descriptor, content_sha256="sha256:" + hashlib.sha256(b"held").hexdigest(), artifact_id=tbm.artifact_id_from_sha256("sha256:" + hashlib.sha256(b"held").hexdigest()), size_bytes=4)
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth, clock=lambda: NOW)
        service.put(_context(registry), held, b"held", retention=tbm.ArtifactRetention("2026-07-28T00:00:00Z", True))
        assert service.get(_context(registry), held.artifact_id) == b"held"


def test_sqlite_artifact_authority_rejects_conflict_drift_and_mutation():
    registry = _registry()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript((tbm.read_packaged_resource("schemas/sqlite-v3-artifact-authority.sql")).decode())
    repository = tbm.SQLiteArtifactV3Repository(connection)
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth:
        record = _service(registry, repository, auth).put(_context(registry), _artifact(), b"secret artifact").record
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE v3_encrypted_artifacts SET media_type = 'text/plain'")
    connection.execute(
        "CREATE TRIGGER unexpected_artifact_trigger BEFORE INSERT ON "
        "v3_encrypted_artifacts BEGIN SELECT RAISE(ABORT, 'unexpected'); END"
    )
    with pytest.raises(tbm.SQLiteArtifactV3SchemaError):
        repository.load(record.artifact.artifact_id)


def test_artifact_service_rejects_unbounded_id_before_authorization():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth)
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.get(_context(registry), "x" * 10000)
        assert caught.value.code == "TBM_ARTIFACT_INPUT_INVALID"
        assert auth.list_decisions(registry.authorization_policy.policy_sha256) == ()


def test_artifact_service_sanitizes_malformed_provider_output():
    class BadProvider(_Provider):
        def encrypt(self, plaintext: bytes, *, key_id: str, aad: bytes) -> tuple[bytes, bytes]:
            return b"ciphertext", b""

        def decrypt(self, ciphertext: bytes, *, key_id: str, nonce: bytes, aad: bytes) -> bytes:
            return b"secret artifact"

    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth, provider=BadProvider())
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.put(_context(registry), _artifact(), b"secret artifact")
        assert caught.value.code == "TBM_ARTIFACT_ENCRYPTION_FAILED"
        assert caught.value.__cause__ is None


def test_artifact_service_rejects_invalid_trusted_time():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth, clock=lambda: "not-a-timestamp")
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.put(_context(registry), _artifact(), b"secret artifact")
        assert caught.value.code == "TBM_ARTIFACT_CLOCK_FAILED"
