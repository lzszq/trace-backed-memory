from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_artifact_v3 as artifact_module
from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from tests.test_artifact_service_v3 import (
    NOW,
    _Provider,
    _artifact,
    _context,
    _registry,
    _service,
)
from trace_backed_memory.postgres import _load_psycopg


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-artifact-authority.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-artifact-authority-rollback.sql"
SCHEMA = "trace_backed_memory_v3_artifacts"


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    result = postgres_cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr


def _repository(
    postgres_cluster: PostgresCluster,
) -> artifact_module.PostgresArtifactV3Repository:
    return artifact_module.PostgresArtifactV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )


def _record() -> tbm.EncryptedArtifactRecord:
    artifact = _artifact()
    retention = tbm.ArtifactRetention()
    provider = _Provider()
    authorization_event_id = "authz_sha256_" + "a" * 64
    aad = tbm.artifact_aad(
        artifact,
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
        write_authorization_event_id=authorization_event_id,
        encryption_provider_id=provider.provider_id,
        encryption_algorithm=provider.algorithm,
        encryption_key_id="key_001",
        retention=retention,
        stored_at=NOW,
    )
    ciphertext, nonce = provider.encrypt(
        b"secret artifact",
        key_id="key_001",
        aad=aad,
    )
    return tbm.EncryptedArtifactRecord(
        artifact=artifact,
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
        write_authorization_event_id=authorization_event_id,
        encryption_provider_id=provider.provider_id,
        encryption_algorithm=provider.algorithm,
        encryption_key_id="key_001",
        nonce=nonce,
        ciphertext=ciphertext,
        ciphertext_sha256=(
            "sha256:" + hashlib.sha256(ciphertext).hexdigest()
        ),
        retention=retention,
        stored_at=NOW,
    )


def _row(record: tbm.EncryptedArtifactRecord) -> dict[str, object]:
    artifact = record.artifact
    return {
        "artifact_id": artifact.artifact_id,
        "content_sha256": artifact.content_sha256,
        "size_bytes": artifact.size_bytes,
        "media_type": artifact.media_type,
        "classification": artifact.classification,
        "created_at": artifact.created_at,
        "redaction_policy_id": artifact.redaction_policy_id,
        "tenant_id": record.tenant_id,
        "repository_id": record.repository_id,
        "environment_id": record.environment_id,
        "write_authorization_event_id":
            record.write_authorization_event_id,
        "encryption_provider_id": record.encryption_provider_id,
        "encryption_algorithm": record.encryption_algorithm,
        "artifact_encryption_key_id": artifact.encryption_key_id,
        "encryption_key_id": record.encryption_key_id,
        "nonce": record.nonce,
        "ciphertext": record.ciphertext,
        "ciphertext_sha256": record.ciphertext_sha256,
        "retain_until": record.retention.retain_until,
        "legal_hold": record.retention.legal_hold,
        "stored_at": record.stored_at,
    }


class _ResultCursor:
    def __init__(self, *results: list[dict[str, object]]) -> None:
        self._results = iter(results)

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchall(self) -> list[dict[str, object]]:
        return next(self._results)


class _DatabaseError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("database detail must remain sanitized")
        self.sqlstate = sqlstate


def test_postgres_artifact_service_round_trip_and_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    registry = _registry()
    with tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    ) as authorization, _repository(postgres_cluster) as artifacts:
        service = _service(registry, artifacts, authorization)
        descriptor = _artifact()
        first = service.put(
            _context(registry),
            descriptor,
            b"secret artifact",
        )
        replay = service.put(
            _context(registry),
            descriptor,
            b"secret artifact",
        )
        read = service.get_with_receipt(
            _context(registry),
            descriptor.artifact_id,
        )
        assert first.inserted is True
        assert replay == tbm.StoredArtifactResult(first.record, False)
        assert read.content == b"secret artifact"
        assert read.authorization_event_id.startswith("authz_sha256_")
        assert artifacts.find(descriptor.artifact_id) == first.record


def test_postgres_artifact_resources_exports_and_lifecycle_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "postgres-v3-artifact-authority.sql",
        "postgres-v3-artifact-authority-rollback.sql",
    ):
        canonical = (ROOT / "schemas" / name).read_bytes()
        assert tbm.read_packaged_resource(f"schemas/{name}") == canonical
    expected = {
        "POSTGRES_ARTIFACT_V3_SCHEMA_VERSION",
        "PostgresArtifactV3ConflictError",
        "PostgresArtifactV3Error",
        "PostgresArtifactV3NotFoundError",
        "PostgresArtifactV3PersistenceError",
        "PostgresArtifactV3Repository",
        "PostgresArtifactV3SchemaError",
        "PostgresArtifactV3StoreResult",
    }
    assert expected <= set(tbm.__all__)
    with pytest.raises(ValueError, match="connection is required"):
        artifact_module.PostgresArtifactV3Repository(None)

    class _Psycopg:
        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("connection failed")

    monkeypatch.setattr(
        artifact_module,
        "_load_psycopg",
        lambda: (_Psycopg, object(), object()),
    )
    with pytest.raises(
        artifact_module.PostgresArtifactV3PersistenceError,
        match="failed to connect",
    ):
        artifact_module.PostgresArtifactV3Repository.connect()

    class _Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    repository = artifact_module.PostgresArtifactV3Repository(
        connection,
        owns_connection=True,
    )
    repository.close()
    repository.close()
    assert connection.closed is True
    with pytest.raises(
        artifact_module.PostgresArtifactV3Error,
        match="closed",
    ):
        repository.__enter__()


def test_postgres_artifact_schema_row_and_error_guards() -> None:
    repository = artifact_module.PostgresArtifactV3Repository(object())
    empty_counts = {
        "policy_count": 0,
        "rule_count": 0,
        "unsupported_relation_count": 0,
    }
    with pytest.raises(
        artifact_module.PostgresArtifactV3SchemaError,
        match="unsupported",
    ):
        repository._verify_schema_catalog(
            _ResultCursor([{
                **empty_counts,
                "policy_count": 1,
            }])
        )
    with pytest.raises(
        artifact_module.PostgresArtifactV3SchemaError,
        match="catalog does not match",
    ):
        repository._verify_schema_catalog(
            _ResultCursor(
                [empty_counts],
                [{"catalog_sha256": "bad"}],
            )
        )
    with pytest.raises(
        artifact_module.PostgresArtifactV3SchemaError,
        match="search_path",
    ):
        repository._lock_schema(
            _ResultCursor([{"search_path": None}]),
            for_write=False,
        )
    with pytest.raises(
        artifact_module.PostgresArtifactV3SchemaError,
        match="metadata mismatch",
    ):
        repository._lock_schema(
            _ResultCursor(
                [{"search_path": "public"}],
                [{
                    "active_version": 2,
                    "artifact_version": 99,
                    "artifact_contract":
                        tbm.ARTIFACT_AUTHORITY_CONTRACT_VERSION,
                }],
            ),
            for_write=False,
        )

    record = _record()
    row = _row(record)
    assert repository._record({
        **row,
        "nonce": memoryview(record.nonce),
        "ciphertext": memoryview(record.ciphertext),
    }) == record
    with pytest.raises(
        artifact_module.PostgresArtifactV3PersistenceError,
        match="invalid byte columns",
    ):
        repository._record({**row, "nonce": object()})
    with pytest.raises(
        artifact_module.PostgresArtifactV3PersistenceError,
        match="failed validation",
    ):
        repository._record({**row, "legal_hold": 1})
    with pytest.raises(
        artifact_module.PostgresArtifactV3PersistenceError,
        match="columns do not match",
    ):
        repository._record({
            **row,
            "stored_at": record.stored_at.replace("Z", "+00:00"),
        })
    with pytest.raises(
        artifact_module.PostgresArtifactV3PersistenceError,
        match="identity is not unique",
    ):
        repository._select(
            _ResultCursor([row, row]),
            record.artifact.artifact_id,
            for_share=False,
        )
    for sqlstate, error_type in (
        ("42P01", artifact_module.PostgresArtifactV3SchemaError),
        ("23505", artifact_module.PostgresArtifactV3ConflictError),
        ("P0001", artifact_module.PostgresArtifactV3ConflictError),
        ("XX000", artifact_module.PostgresArtifactV3PersistenceError),
        (None, artifact_module.PostgresArtifactV3PersistenceError),
    ):
        with pytest.raises(error_type):
            repository._raise_database(_DatabaseError(sqlstate))


def test_postgres_artifact_repository_conflict_not_found_and_guards(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    record = _record()
    with _repository(postgres_cluster) as repository:
        assert repository.put(record).artifact_inserted is True
        assert repository.put(record).artifact_inserted is False
        assert repository.load(record.artifact.artifact_id) == record
        with pytest.raises(artifact_module.PostgresArtifactV3ConflictError):
            repository.put(
                replace(
                    record,
                    retention=tbm.ArtifactRetention(legal_hold=True),
                )
            )
        missing = _artifact(b"missing").artifact_id
        assert repository.find(missing) is None
        with pytest.raises(
            artifact_module.PostgresArtifactV3NotFoundError
        ):
            repository.load(missing)
        with pytest.raises(ValueError):
            repository.put(object())
        with pytest.raises(ValueError):
            repository.find("invalid")
    with pytest.raises(artifact_module.PostgresArtifactV3Error):
        repository.load(record.artifact.artifact_id)


def test_postgres_artifact_database_guards_are_immutable(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    record = _record()
    with _repository(postgres_cluster) as repository:
        repository.put(record)
    for statement in (
        f"UPDATE {SCHEMA}.encrypted_artifacts "
        "SET media_type = 'text/plain'",
        f"DELETE FROM {SCHEMA}.encrypted_artifacts",
        f"TRUNCATE {SCHEMA}.encrypted_artifacts",
        f"UPDATE {SCHEMA}.schema_metadata SET schema_version = 1",
        f"TRUNCATE {SCHEMA}.schema_metadata",
    ):
        result = postgres_cluster.run(statement)
        assert result.returncode != 0
        assert "immutable" in result.stderr
    psycopg, dict_row, _Jsonb = _load_psycopg()
    bad_values = list(
        artifact_module.PostgresArtifactV3Repository._values(record)
    )
    bad_values[17] = "sha256:" + "0" * 64
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with pytest.raises(
            psycopg.Error,
            match="ciphertext digest mismatch",
        ):
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SCHEMA}.encrypted_artifacts ("
                    "artifact_id, content_sha256, size_bytes, media_type, "
                    "classification, created_at, redaction_policy_id, "
                    "tenant_id, repository_id, environment_id, "
                    "write_authorization_event_id, "
                    "encryption_provider_id, encryption_algorithm, "
                    "artifact_encryption_key_id, encryption_key_id, nonce, "
                    "ciphertext, ciphertext_sha256, retain_until, "
                    "legal_hold, stored_at"
                    ") VALUES ("
                    + ", ".join("%s" for _ in bad_values)
                    + ")",
                    bad_values,
                )


def test_postgres_artifact_catalog_drift_fails_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    record = _record()
    with _repository(postgres_cluster) as repository:
        repository.put(record)
        drift = postgres_cluster.run(
            f"CREATE INDEX unexpected_artifact_index "
            f"ON {SCHEMA}.encrypted_artifacts(media_type)"
        )
        assert drift.returncode == 0, drift.stderr
        with pytest.raises(
            artifact_module.PostgresArtifactV3SchemaError
        ):
            repository.load(record.artifact.artifact_id)
    rejected_rollback = postgres_cluster.run_script(ROLLBACK)
    assert rejected_rollback.returncode != 0
    assert "rollback catalog mismatch" in rejected_rollback.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            f"SELECT to_regnamespace('{SCHEMA}') IS NOT NULL",
        )
        == "t"
    )
    repaired = postgres_cluster.run(
        f"DROP INDEX {SCHEMA}.unexpected_artifact_index"
    )
    assert repaired.returncode == 0, repaired.stderr
    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr


@pytest.mark.parametrize(
    "drift_sql",
    (
        f"ALTER INDEX {SCHEMA}.encrypted_artifacts_scope "
        "SET (fillfactor = 70)",
        f"ALTER FUNCTION {SCHEMA}.verify_encrypted_artifact() "
        "PARALLEL SAFE",
    ),
    ids=("index-reloptions", "function-parallel"),
)
def test_postgres_artifact_runtime_and_rollback_share_catalog_fingerprint(
    postgres_cluster: PostgresCluster,
    drift_sql: str,
) -> None:
    _install(postgres_cluster)
    record = _record()
    with _repository(postgres_cluster) as repository:
        repository.put(record)
        drift = postgres_cluster.run(drift_sql)
        assert drift.returncode == 0, drift.stderr
        with pytest.raises(
            artifact_module.PostgresArtifactV3SchemaError,
            match="catalog does not match",
        ):
            repository.load(record.artifact.artifact_id)
    rejected_rollback = postgres_cluster.run_script(ROLLBACK)
    assert rejected_rollback.returncode != 0
    assert "rollback catalog mismatch" in rejected_rollback.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            f"SELECT to_regnamespace('{SCHEMA}') IS NOT NULL",
        )
        == "t"
    )


def test_postgres_artifact_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    record = _record()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        repository = artifact_module.PostgresArtifactV3Repository(connection)
        with pytest.raises(RuntimeError):
            with connection.transaction():
                repository.put(record)
                raise RuntimeError("rollback outer transaction")
        assert repository.find(record.artifact.artifact_id) is None


def test_postgres_artifact_restores_caller_search_path(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    record = _record()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA artifact_caller")
            cursor.execute(
                "SET search_path = artifact_caller, public"
            )
        repository = artifact_module.PostgresArtifactV3Repository(connection)
        assert repository.put(record).artifact_inserted is True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('search_path') AS search_path"
            )
            assert cursor.fetchone() == {
                "search_path": "artifact_caller, public"
            }
        assert repository.load(record.artifact.artifact_id) == record


def test_postgres_artifact_concurrent_replay_is_single_insert(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    record = _record()

    def store() -> bool:
        with _repository(postgres_cluster) as repository:
            return repository.put(record).artifact_inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        inserted = tuple(executor.map(lambda _item: store(), range(2)))
    assert sorted(inserted) == [False, True]


def test_postgres_artifact_install_gate_and_rollback(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    downgrade = postgres_cluster.run(
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 1 WHERE singleton"
    )
    assert downgrade.returncode == 0, downgrade.stderr
    rejected = postgres_cluster.run_script(INSTALL)
    assert rejected.returncode != 0
    assert "active schema version 2" in rejected.stderr

    restore = postgres_cluster.run(
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 2 WHERE singleton"
    )
    assert restore.returncode == 0, restore.stderr
    installed = postgres_cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr
    dependency = postgres_cluster.run(
        "CREATE VIEW public.artifact_dependency AS "
        f"SELECT artifact_id FROM {SCHEMA}.encrypted_artifacts"
    )
    assert dependency.returncode == 0, dependency.stderr
    rejected_dependency = postgres_cluster.run_script(ROLLBACK)
    assert rejected_dependency.returncode != 0
    assert "depend" in rejected_dependency.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            f"SELECT to_regnamespace('{SCHEMA}') IS NOT NULL",
        )
        == "t"
    )
    removed_dependency = postgres_cluster.run(
        "DROP VIEW public.artifact_dependency"
    )
    assert removed_dependency.returncode == 0, removed_dependency.stderr
    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            f"SELECT to_regnamespace('{SCHEMA}') IS NULL",
        )
        == "t"
    )
