from __future__ import annotations

from dataclasses import replace
import hashlib
import sqlite3
from types import SimpleNamespace

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
        read = service.get_with_receipt(
            _context(registry), descriptor.artifact_id
        )
        assert read.content == b"secret artifact"
        assert read.authorization_event_id.startswith("authz_sha256_")
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


def test_artifact_write_authorization_denial_is_mapped():
    registry = _registry(permissions=("artifact:read",))
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as auth, tbm.SQLiteArtifactV3Repository.connect(
        initialize=True
    ) as artifacts:
        service = _service(registry, artifacts, auth)
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.put(
                _context(registry),
                _artifact(),
                b"secret artifact",
            )
        assert caught.value.code == "TBM_SERVICE_AUTHORIZATION_DENIED"
        assert caught.value.__cause__ is None


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


def test_sqlite_artifact_authority_idempotence_conflict_and_not_found():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as auth, tbm.SQLiteArtifactV3Repository.connect(
        initialize=True
    ) as artifacts:
        record = _service(registry, artifacts, auth).put(
            _context(registry),
            _artifact(),
            b"secret artifact",
        ).record
        assert artifacts.load(record.artifact.artifact_id) == record
        assert artifacts.find(record.artifact.artifact_id) == record
        assert artifacts.find(_artifact(b"missing").artifact_id) is None
        assert artifacts.put(record) == tbm.SQLiteArtifactV3StoreResult(
            artifact_id=record.artifact.artifact_id,
            artifact_inserted=False,
        )
        with pytest.raises(tbm.SQLiteArtifactV3NotFoundError):
            artifacts.load(_artifact(b"missing").artifact_id)
        with pytest.raises(ValueError):
            artifacts.put(object())
        with pytest.raises(tbm.SQLiteArtifactV3ConflictError):
            artifacts.put(
                replace(
                    record,
                    retention=tbm.ArtifactRetention(legal_hold=True),
                )
            )


def test_sqlite_artifact_authority_uses_nested_savepoint():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as auth, tbm.SQLiteArtifactV3Repository.connect(
        initialize=True
    ) as source:
        record = _service(registry, source, auth).put(
            _context(registry),
            _artifact(),
            b"secret artifact",
        ).record
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-artifact-authority.sql"
        ).decode()
    )
    repository = tbm.SQLiteArtifactV3Repository(connection)
    connection.execute("BEGIN")
    assert repository.put(record).artifact_inserted is True
    connection.rollback()
    assert repository.find(record.artifact.artifact_id) is None
    connection.close()


def test_sqlite_artifact_authority_closes_after_commit_and_rollback_failure():
    class BrokenTransactionConnection(sqlite3.Connection):
        fail_transactions = False

        def commit(self):
            if self.fail_transactions:
                raise sqlite3.OperationalError("commit secret")
            return super().commit()

        def rollback(self):
            if self.fail_transactions:
                raise sqlite3.OperationalError("rollback secret")
            return super().rollback()

    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as auth, tbm.SQLiteArtifactV3Repository.connect(
        initialize=True
    ) as source:
        record = _service(registry, source, auth).put(
            _context(registry),
            _artifact(),
            b"secret artifact",
        ).record

    connection = sqlite3.connect(
        ":memory:",
        factory=BrokenTransactionConnection,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-artifact-authority.sql"
        ).decode()
    )
    repository = tbm.SQLiteArtifactV3Repository(connection)
    connection.fail_transactions = True
    with pytest.raises(tbm.SQLiteArtifactV3PersistenceError) as caught:
        repository.put(record)
    assert "secret" not in str(caught.value)
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("nonce", "not-a-blob"),
        ("stored_at", "bad"),
        ("stored_at", "2026-07-29T08:00:00+08:00"),
    ),
)
def test_sqlite_artifact_authority_rejects_corrupt_rows(column, value):
    registry = _registry()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    schema = tbm.read_packaged_resource(
        "schemas/sqlite-v3-artifact-authority.sql"
    ).decode()
    connection.executescript(schema)
    repository = tbm.SQLiteArtifactV3Repository(connection)
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as auth:
        record = _service(registry, repository, auth).put(
            _context(registry),
            _artifact(),
            b"secret artifact",
        ).record
    connection.execute(
        "DROP TRIGGER v3_encrypted_artifacts_immutable_update"
    )
    connection.execute(
        f"UPDATE v3_encrypted_artifacts SET {column} = ?",
        (value,),
    )
    connection.executescript(schema)
    with pytest.raises(tbm.SQLiteArtifactV3PersistenceError):
        repository.load(record.artifact.artifact_id)
    connection.close()


def test_sqlite_artifact_authority_rejects_schema_and_connection_failures():
    with pytest.raises(ValueError):
        tbm.SQLiteArtifactV3Repository(object())
    with pytest.raises(tbm.SQLiteArtifactV3PersistenceError):
        tbm.SQLiteArtifactV3Repository.connect(object())
    with tbm.SQLiteArtifactV3Repository.connect() as uninitialized:
        with pytest.raises(tbm.SQLiteArtifactV3PersistenceError):
            uninitialized.find(_artifact().artifact_id)
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-artifact-authority.sql"
        ).decode()
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    repository = tbm.SQLiteArtifactV3Repository(connection)
    with pytest.raises(tbm.SQLiteArtifactV3SchemaError):
        repository.find(_artifact().artifact_id)
    connection.close()

    version_connection = sqlite3.connect(":memory:")
    version_connection.execute("PRAGMA foreign_keys = ON")
    version_connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-artifact-authority.sql"
        ).decode()
    )
    version_connection.execute(
        "PRAGMA ignore_check_constraints = ON"
    )
    version_connection.execute(
        "UPDATE trace_backed_memory_v3_artifact_authority_schema "
        "SET schema_version = 2"
    )
    version_repository = tbm.SQLiteArtifactV3Repository(
        version_connection
    )
    with pytest.raises(tbm.SQLiteArtifactV3SchemaError):
        version_repository.find(_artifact().artifact_id)
    version_connection.close()

    definition_connection = sqlite3.connect(":memory:")
    definition_connection.execute("PRAGMA foreign_keys = ON")
    definition_connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-artifact-authority.sql"
        ).decode()
    )
    definition_connection.execute(
        "DROP INDEX v3_encrypted_artifacts_scope"
    )
    definition_connection.execute(
        "CREATE INDEX v3_encrypted_artifacts_scope "
        "ON v3_encrypted_artifacts(tenant_id)"
    )
    definition_repository = tbm.SQLiteArtifactV3Repository(
        definition_connection
    )
    with pytest.raises(tbm.SQLiteArtifactV3SchemaError):
        definition_repository.find(_artifact().artifact_id)
    definition_connection.close()

    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as auth, tbm.SQLiteArtifactV3Repository.connect(
        initialize=True
    ) as record_repository:
        record = _service(registry, record_repository, auth).put(
            _context(registry),
            _artifact(),
            b"secret artifact",
        ).record
    repository = tbm.SQLiteArtifactV3Repository.connect(initialize=True)
    repository.close()
    repository.close()
    with pytest.raises(tbm.SQLiteArtifactV3PersistenceError):
        repository.load(_artifact().artifact_id)
    with pytest.raises(tbm.SQLiteArtifactV3PersistenceError):
        repository.find(_artifact().artifact_id)
    with pytest.raises(tbm.SQLiteArtifactV3PersistenceError):
        repository.put(record)


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


def test_artifact_service_constructor_guards():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        valid = _service(registry, artifacts, auth)
        with pytest.raises(TypeError):
            tbm.AuthenticatedArtifactService(
                authorization_service=object(),
                authority=artifacts,
                encryption_provider=_Provider(),
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError):
            tbm.AuthenticatedArtifactService(
                authorization_service=valid._authorization_service,
                authority=artifacts,
                encryption_provider=_Provider(),
                clock=None,
            )
        for provider in (
            SimpleNamespace(
                provider_id="",
                algorithm="TEST",
                encrypt=lambda *_args, **_kwargs: None,
                decrypt=lambda *_args, **_kwargs: None,
            ),
            SimpleNamespace(provider_id="provider", algorithm="TEST"),
        ):
            with pytest.raises(TypeError):
                tbm.AuthenticatedArtifactService(
                    authorization_service=valid._authorization_service,
                    authority=artifacts,
                    encryption_provider=provider,
                    clock=lambda: NOW,
                )


@pytest.mark.parametrize(
    ("artifact", "content", "retention", "code"),
    (
        (object(), b"secret artifact", None, "TBM_ARTIFACT_INPUT_INVALID"),
        (_artifact(), object(), None, "TBM_ARTIFACT_INPUT_INVALID"),
        (
            replace(
                _artifact(),
                classification="internal",
                encryption_key_id=None,
            ),
            b"secret artifact",
            None,
            "TBM_ARTIFACT_KEY_REQUIRED",
        ),
        (_artifact(), b"wrong", None, "TBM_ARTIFACT_CONTENT_MISMATCH"),
        (
            _artifact(),
            b"secret artifact",
            object(),
            "TBM_ARTIFACT_RETENTION_INVALID",
        ),
    ),
)
def test_artifact_service_put_input_guards(
    artifact,
    content,
    retention,
    code,
):
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth)
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.put(
                _context(registry),
                artifact,
                content,
                retention=retention,
            )
        assert caught.value.code == code


def test_artifact_service_rejects_existing_conflict_and_corruption():
    registry = _registry()
    descriptor = _artifact()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth)
        service.put(_context(registry), descriptor, b"secret artifact")
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.put(
                _context(registry),
                descriptor,
                b"secret artifact",
                retention=tbm.ArtifactRetention(legal_hold=True),
            )
        assert caught.value.code == "TBM_ARTIFACT_IMMUTABLE_CONFLICT"

        class FailingDecryptProvider(_Provider):
            def decrypt(self, *_args, **_kwargs):
                raise RuntimeError("provider secret")

        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            _service(
                registry,
                artifacts,
                auth,
                provider=FailingDecryptProvider(),
            ).put(_context(registry), descriptor, b"secret artifact")
        assert caught.value.code == "TBM_ARTIFACT_DECRYPTION_FAILED"
        assert "provider secret" not in str(caught.value)

        class WrongPlaintextProvider(_Provider):
            def decrypt(self, *_args, **_kwargs):
                return b"wrong"

        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            _service(
                registry,
                artifacts,
                auth,
                provider=WrongPlaintextProvider(),
            ).put(_context(registry), descriptor, b"secret artifact")
        assert caught.value.code == "TBM_ARTIFACT_INTEGRITY_FAILED"


@pytest.mark.parametrize("mode", ("non_bytes", "plaintext", "roundtrip"))
def test_artifact_service_rejects_invalid_encryption_provider_roundtrip(mode):
    class InvalidProvider(_Provider):
        def encrypt(self, plaintext, *, key_id, aad):
            if mode == "non_bytes":
                return "ciphertext", b"nonce"
            if mode == "plaintext":
                return plaintext, b"nonce"
            return b"ciphertext", b"nonce"

        def decrypt(self, *_args, **_kwargs):
            return b"wrong"

    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            _service(
                registry,
                artifacts,
                auth,
                provider=InvalidProvider(),
            ).put(_context(registry), _artifact(), b"secret artifact")
        assert caught.value.code == "TBM_ARTIFACT_ENCRYPTION_FAILED"


def test_artifact_service_sanitizes_authority_write_failures():
    class Authority:
        def __init__(self, mode):
            self.mode = mode
            self.record = None

        def find(self, _artifact_id):
            if self.mode == "find":
                raise RuntimeError("database secret")
            return None

        def put(self, record):
            self.record = record
            if self.mode == "receipt":
                return object()
            return SimpleNamespace(
                artifact_inserted=True,
                artifact_id=record.artifact.artifact_id,
            )

        def load(self, _artifact_id):
            if self.mode == "readback":
                return replace(self.record, tenant_id="tenant_other")
            return self.record

    registry = _registry()
    for mode, code in (
        ("find", "TBM_ARTIFACT_READ_FAILED"),
        ("receipt", "TBM_ARTIFACT_PERSIST_FAILED"),
        ("readback", "TBM_ARTIFACT_PERSIST_FAILED"),
    ):
        with tbm.SQLiteAuthorizationV3Repository.connect(
            initialize=True
        ) as auth:
            service = _service(registry, Authority(mode), auth)
            with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
                service.put(
                    _context(registry),
                    _artifact(),
                    b"secret artifact",
                )
            assert caught.value.code == code
            assert "database secret" not in str(caught.value)


def test_artifact_service_rejects_read_scope_provider_and_integrity_drift():
    registry = _registry()
    descriptor = _artifact()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth)
        record = service.put(
            _context(registry),
            descriptor,
            b"secret artifact",
        ).record

        class Authority:
            def __init__(self, loaded):
                self.loaded = loaded

            def find(self, _artifact_id):
                return None

            def put(self, _record):
                return object()

            def load(self, _artifact_id):
                if isinstance(self.loaded, Exception):
                    raise self.loaded
                return self.loaded

        other_artifact = _artifact(b"other")
        cases = (
            (
                RuntimeError("database secret"),
                _Provider(),
                "TBM_ARTIFACT_READ_FAILED",
            ),
            (
                replace(record, artifact=other_artifact),
                _Provider(),
                "TBM_ARTIFACT_INTEGRITY_FAILED",
            ),
            (
                replace(record, tenant_id="tenant_other"),
                _Provider(),
                "TBM_ARTIFACT_SCOPE_REJECTED",
            ),
            (
                replace(record, encryption_provider_id="provider_other"),
                _Provider(),
                "TBM_ARTIFACT_PROVIDER_MISMATCH",
            ),
        )
        for loaded, provider, code in cases:
            with tbm.SQLiteAuthorizationV3Repository.connect(
                initialize=True
            ) as read_auth:
                read_service = _service(
                    registry,
                    Authority(loaded),
                    read_auth,
                    provider=provider,
                )
                with pytest.raises(
                    tbm.AuthenticatedArtifactServiceV3Error
                ) as caught:
                    read_service.get(
                        _context(registry),
                        descriptor.artifact_id,
                    )
                assert caught.value.code == code
                assert "database secret" not in str(caught.value)

        class FailingDecryptProvider(_Provider):
            def decrypt(self, *_args, **_kwargs):
                raise RuntimeError("provider secret")

        class WrongPlaintextProvider(_Provider):
            def decrypt(self, *_args, **_kwargs):
                return b"wrong"

        for provider, code in (
            (FailingDecryptProvider(), "TBM_ARTIFACT_DECRYPTION_FAILED"),
            (WrongPlaintextProvider(), "TBM_ARTIFACT_INTEGRITY_FAILED"),
        ):
            with tbm.SQLiteAuthorizationV3Repository.connect(
                initialize=True
            ) as read_auth:
                read_service = _service(
                    registry,
                    Authority(record),
                    read_auth,
                    provider=provider,
                )
                with pytest.raises(
                    tbm.AuthenticatedArtifactServiceV3Error
                ) as caught:
                    read_service.get(
                        _context(registry),
                        descriptor.artifact_id,
                    )
                assert caught.value.code == code


def test_artifact_service_rejects_non_string_trusted_clock():
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(initialize=True) as auth, tbm.SQLiteArtifactV3Repository.connect(initialize=True) as artifacts:
        service = _service(registry, artifacts, auth, clock=lambda: 1)
        with pytest.raises(tbm.AuthenticatedArtifactServiceV3Error) as caught:
            service.put(_context(registry), _artifact(), b"secret artifact")
        assert caught.value.code == "TBM_ARTIFACT_CLOCK_FAILED"
