from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_semantic_gate_artifact_v3 as artifact_module
from tests.postgres_support import PostgresCluster
from trace_backed_memory.postgres import _load_psycopg
from trace_backed_memory.postgres_gate_evidence_v3 import (
    PostgresGateEvidenceV3Repository,
)
from trace_backed_memory.postgres_semantic_gate_v3 import (
    PostgresSemanticGateV3NotFoundError,
    PostgresSemanticGateV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_EVIDENCE_INSTALL = ROOT / "schemas" / "postgres-v3-gate-evidence.sql"
SEMANTIC_GATE_INSTALL = ROOT / "schemas" / "postgres-v3-semantic-gate.sql"
INSTALL = ROOT / "schemas" / "postgres-v3-semantic-gate-artifacts.sql"
ROLLBACK = (
    ROOT / "schemas" / "postgres-v3-semantic-gate-artifacts-rollback.sql"
)
SCHEMA = "trace_backed_memory_v3_semantic_gate_artifacts"
PROMPT = b"Review the exact bounded candidate set."
RESPONSE = b'{"decision":"allow","reason":"applicable"}'
NOW = "2026-07-27T08:02:00Z"


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    for script in (
        GATE_EVIDENCE_INSTALL,
        SEMANTIC_GATE_INSTALL,
        INSTALL,
    ):
        result = postgres_cluster.run_script(script)
        assert result.returncode == 0, result.stderr


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
    prompt_artifact = tbm.create_content_addressed_artifact(
        PROMPT,
        media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
        classification="internal",
        created_at=NOW,
    )
    response_artifact = tbm.create_content_addressed_artifact(
        RESPONSE,
        media_type="application/json",
        classification="internal",
        created_at="2026-07-27T08:02:01Z",
    )
    values = {
        key: value
        for key, value in attempt.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        session_id=evaluation.session_id,
        retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
        system_gate_evaluation_id=evaluation.evaluation_id,
        prompt_artifact_sha256=prompt_artifact.content_sha256,
        response_artifact_sha256=response_artifact.content_sha256,
    )
    return (
        snapshot,
        evaluation,
        tbm.build_semantic_gate_attempt(**values),
    )


def _failed_attempt(
    attempt: tbm.SemanticGateAttempt,
) -> tbm.SemanticGateAttempt:
    values = {
        key: value
        for key, value in attempt.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
    values.update(
        status="failed",
        response_artifact_sha256=None,
        decision_id=None,
        final_allowed_revision_ids=(),
        final_blocked_revision_ids=(),
        reason=None,
        risk=None,
        recommended_injection=None,
        error_code="provider_timeout",
        output_tokens=None,
    )
    return tbm.build_semantic_gate_attempt(**values)


def _artifacts(
    attempt: tbm.SemanticGateAttempt,
    *,
    classification: tbm.DataClassification = "internal",
    encryption_key_id: str | None = None,
) -> tuple[
    tbm.StoredSemanticGateArtifact,
    tbm.StoredSemanticGateArtifact | None,
]:
    prompt_binding = tbm.create_semantic_gate_artifact_binding(
        attempt,
        PROMPT,
        artifact_role="prompt",
        media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
        classification=classification,
        created_at=NOW,
        encryption_key_id=encryption_key_id,
    )
    prompt = tbm.StoredSemanticGateArtifact(prompt_binding, PROMPT)
    if attempt.status == "failed":
        return prompt, None
    response_binding = tbm.create_semantic_gate_artifact_binding(
        attempt,
        RESPONSE,
        artifact_role="response",
        media_type="application/json",
        classification=classification,
        created_at="2026-07-27T08:02:01Z",
        encryption_key_id=encryption_key_id,
    )
    return (
        prompt,
        tbm.StoredSemanticGateArtifact(response_binding, RESPONSE),
    )


def _seed_evidence(postgres_cluster: PostgresCluster) -> None:
    snapshot, evaluation, _attempt = _records()
    with PostgresGateEvidenceV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as repository:
        repository.store_bundle(snapshot, evaluation)


def _repository(
    postgres_cluster: PostgresCluster,
) -> artifact_module.PostgresSemanticGateArtifactV3Repository:
    return artifact_module.PostgresSemanticGateArtifactV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )


def test_postgres_semantic_gate_artifact_schema_installs(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    result = postgres_cluster.run(
        "SELECT schema_version, contract_version "
        f"FROM {SCHEMA}.schema_metadata WHERE singleton"
    )
    assert result.returncode == 0, result.stderr
    assert "1|tbm.semantic-gate-artifact.v3" in result.stdout

    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                artifact_module
                ._POSTGRES_SEMANTIC_GATE_ARTIFACT_CATALOG_SHA256_QUERY,
                (SCHEMA,) * 7,
            )
            fingerprint = cursor.fetchone()["catalog_sha256"]
    assert fingerprint == artifact_module._EXPECTED_CATALOG_SHA256


def test_postgres_semantic_gate_artifact_schema_rolls_back(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    result = postgres_cluster.run_script(ROLLBACK)
    assert result.returncode == 0, result.stderr
    missing = postgres_cluster.run(
        "SELECT to_regnamespace("
        "'trace_backed_memory_v3_semantic_gate_artifacts') IS NULL"
    )
    assert missing.returncode == 0, missing.stderr
    assert "t" in missing.stdout


def test_postgres_semantic_gate_artifact_round_trip_and_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    assert response is not None
    with _repository(postgres_cluster) as repository:
        result = repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        replay = repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        assert result.attempt.inserted is True
        assert result.prompt_artifact_inserted is True
        assert result.prompt_binding_inserted is True
        assert result.response_artifact_inserted is True
        assert result.response_binding_inserted is True
        assert replay.attempt.inserted is False
        assert replay.prompt_artifact_inserted is False
        assert replay.prompt_binding_inserted is False
        assert replay.response_artifact_inserted is False
        assert replay.response_binding_inserted is False
        assert repository.load_attempt_with_artifacts(
            attempt.attempt_id
        ) == tbm.StoredSemanticGateAttemptArtifacts(
            attempt,
            prompt,
            response,
        )


def test_postgres_semantic_gate_artifact_failed_is_prompt_only(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, succeeded = _records()
    attempt = _failed_attempt(succeeded)
    prompt, response = _artifacts(attempt)
    assert response is None
    with _repository(postgres_cluster) as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, None)
        loaded = repository.load_attempt_with_artifacts(attempt.attempt_id)
        assert loaded.prompt == prompt
        assert loaded.response is None


def test_postgres_semantic_gate_artifact_rejects_sensitive_plaintext(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(
        attempt,
        classification="restricted",
        encryption_key_id="kms_key_001",
    )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(ValueError, match="encryption at rest"):
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )


def test_postgres_semantic_gate_artifact_direct_writes_are_guarded(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    with _repository(postgres_cluster) as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, response)

    for statement in (
        f"UPDATE {SCHEMA}.semantic_gate_artifacts SET content = content",
        f"DELETE FROM {SCHEMA}.semantic_gate_artifact_bindings",
        f"TRUNCATE {SCHEMA}.semantic_gate_artifacts CASCADE",
        f"UPDATE {SCHEMA}.schema_metadata SET schema_version = 1",
    ):
        result = postgres_cluster.run(statement)
        assert result.returncode != 0
        assert "immutable" in result.stderr

    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        artifact = prompt.binding.artifact
        with pytest.raises(Exception, match="bytes do not match identity"):
            with connection.transaction():
                connection.execute(
                    f"INSERT INTO {SCHEMA}.semantic_gate_artifacts ("
                    "artifact_id, content_sha256, size_bytes, media_type, "
                    "classification, created_at, encryption_key_id, "
                    "redaction_policy_id, content"
                    ") VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s)",
                    (
                        artifact.artifact_id,
                        artifact.content_sha256,
                        artifact.size_bytes,
                        artifact.media_type,
                        artifact.classification,
                        artifact.created_at,
                        b"X" * artifact.size_bytes,
                    ),
                )
        other = tbm.create_content_addressed_artifact(
            b"other",
            media_type="application/octet-stream",
            classification="internal",
            created_at=NOW,
        )
        with pytest.raises(Exception, match="created_at is not canonical"):
            with connection.transaction():
                connection.execute(
                    f"INSERT INTO {SCHEMA}.semantic_gate_artifacts ("
                    "artifact_id, content_sha256, size_bytes, media_type, "
                    "classification, created_at, encryption_key_id, "
                    "redaction_policy_id, content"
                    ") VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s)",
                    (
                        other.artifact_id,
                        other.content_sha256,
                        other.size_bytes,
                        other.media_type,
                        other.classification,
                        "2026-07-27T08:02:00+00:00",
                        b"other",
                    ),
                )


def test_postgres_semantic_gate_artifact_rejects_noncanonical_direct_binding(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, _response = _artifacts(attempt)
    with PostgresSemanticGateV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as semantic_repository:
        semantic_repository.store_attempt(attempt)

    artifact = prompt.binding.artifact
    canonical = tbm.dumps_semantic_gate_artifact_binding(prompt.binding)
    duplicate = canonical.replace(
        '"contract_version":"tbm.semantic-gate-artifact.v3"',
        '"contract_version":"tbm.semantic-gate-artifact.v3",'
        '"contract_version":"tbm.semantic-gate-artifact.v3"',
    )
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.transaction():
            connection.execute(
                f"INSERT INTO {SCHEMA}.semantic_gate_artifacts ("
                "artifact_id, content_sha256, size_bytes, media_type, "
                "classification, created_at, encryption_key_id, "
                "redaction_policy_id, content"
                ") VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s)",
                (
                    artifact.artifact_id,
                    artifact.content_sha256,
                    artifact.size_bytes,
                    artifact.media_type,
                    artifact.classification,
                    artifact.created_at,
                    prompt.content,
                ),
            )
        with pytest.raises(Exception, match="descriptor is not canonical"):
            with connection.transaction():
                connection.execute(
                    f"INSERT INTO {SCHEMA}."
                    "semantic_gate_artifact_bindings ("
                    "attempt_id, artifact_role, artifact_id, "
                    "content_sha256, descriptor"
                    ") VALUES (%s, %s, %s, %s, %s)",
                    (
                        attempt.attempt_id,
                        "prompt",
                        artifact.artifact_id,
                        artifact.content_sha256,
                        duplicate,
                    ),
                )


def test_postgres_semantic_gate_artifact_normalizes_valid_timestamp(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt_binding = tbm.create_semantic_gate_artifact_binding(
        attempt,
        PROMPT,
        artifact_role="prompt",
        media_type=tbm.SEMANTIC_GATE_PROMPT_ARTIFACT_MEDIA_TYPE,
        classification="internal",
        created_at="2026-07-27T08:02:00+00:00",
    )
    prompt = tbm.StoredSemanticGateArtifact(prompt_binding, PROMPT)
    _default_prompt, response = _artifacts(attempt)
    assert prompt.binding.artifact.created_at == NOW
    with _repository(postgres_cluster) as repository:
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        loaded = repository.load_attempt_with_artifacts(attempt.attempt_id)
    assert loaded.prompt == prompt


def test_postgres_semantic_gate_artifact_conflict_rolls_back_attempt(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    artifact = prompt.binding.artifact
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.transaction():
            connection.execute(
                f"INSERT INTO {SCHEMA}.semantic_gate_artifacts ("
                "artifact_id, content_sha256, size_bytes, media_type, "
                "classification, created_at, encryption_key_id, "
                "redaction_policy_id, content"
                ") VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, %s)",
                (
                    artifact.artifact_id,
                    artifact.content_sha256,
                    artifact.size_bytes,
                    "application/octet-stream",
                    artifact.classification,
                    artifact.created_at,
                    prompt.content,
                ),
            )

    with _repository(postgres_cluster) as repository:
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3ConflictError,
            match="conflicting content",
        ):
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )
    with PostgresSemanticGateV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as semantic_repository:
        with pytest.raises(PostgresSemanticGateV3NotFoundError):
            semantic_repository.load_attempt(attempt.attempt_id)


def test_postgres_semantic_gate_artifact_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    )
    repository = artifact_module.PostgresSemanticGateArtifactV3Repository(
        connection
    )
    try:
        with connection.transaction():
            connection.execute("CREATE TEMP TABLE caller_work (value text)")
            connection.execute("INSERT INTO caller_work VALUES ('before')")
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )
            assert connection.execute(
                "SELECT value FROM caller_work"
            ).fetchall() == [{"value": "before"}]
        assert repository.load_attempt_with_artifacts(
            attempt.attempt_id
        ).attempt == attempt
    finally:
        repository.close()
        connection.close()


def test_postgres_semantic_gate_artifact_concurrent_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)

    def store() -> bool:
        with _repository(postgres_cluster) as repository:
            return repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            ).attempt.inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: store(), range(2)))
    assert sorted(results) == [False, True]


def test_postgres_semantic_gate_artifact_detects_catalog_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    with _repository(postgres_cluster) as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, response)
    drift = postgres_cluster.run(
        f"ALTER TABLE {SCHEMA}.semantic_gate_artifacts "
        "DISABLE TRIGGER semantic_gate_artifacts_verify_content"
    )
    assert drift.returncode == 0, drift.stderr
    with _repository(postgres_cluster) as repository:
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3SchemaError,
            match="catalog does not match",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)
    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert "catalog mismatch" in rollback.stderr


def test_postgres_semantic_gate_artifact_rollback_is_fail_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    dependency = postgres_cluster.run(
        "CREATE VIEW public.semantic_artifact_dependency AS "
        f"SELECT artifact_id FROM {SCHEMA}.semantic_gate_artifacts"
    )
    assert dependency.returncode == 0, dependency.stderr
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "depend" in rejected.stderr
    present = postgres_cluster.run(
        "SELECT to_regnamespace("
        "'trace_backed_memory_v3_semantic_gate_artifacts') IS NOT NULL"
    )
    assert present.returncode == 0, present.stderr
    assert "t" in present.stdout


def test_postgres_semantic_gate_artifact_resources_and_exports() -> None:
    for name in (
        "postgres-v3-semantic-gate-artifacts.sql",
        "postgres-v3-semantic-gate-artifacts-rollback.sql",
    ):
        canonical = (ROOT / "schemas" / name).read_bytes()
        assert tbm.read_packaged_resource(f"schemas/{name}") == canonical
    assert tbm.POSTGRES_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION == 1
    expected = {
        "POSTGRES_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION",
        "PostgresSemanticGateArtifactV3ConflictError",
        "PostgresSemanticGateArtifactV3Error",
        "PostgresSemanticGateArtifactV3NotFoundError",
        "PostgresSemanticGateArtifactV3PersistenceError",
        "PostgresSemanticGateArtifactV3Repository",
        "PostgresSemanticGateArtifactV3SchemaError",
        "PostgresSemanticGateArtifactV3StoreResult",
        "StoredSemanticGateAttemptArtifacts",
    }
    assert expected <= set(tbm.__all__)


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


def test_postgres_semantic_gate_artifact_lifecycle_and_schema_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="connection is required"):
        artifact_module.PostgresSemanticGateArtifactV3Repository(None)

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
        artifact_module.PostgresSemanticGateArtifactV3PersistenceError,
        match="failed to connect",
    ):
        artifact_module.PostgresSemanticGateArtifactV3Repository.connect()

    class _Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    repository = artifact_module.PostgresSemanticGateArtifactV3Repository(
        connection,
        owns_connection=True,
    )
    repository.close()
    repository.close()
    assert connection.closed is True
    with pytest.raises(
        artifact_module.PostgresSemanticGateArtifactV3Error,
        match="closed",
    ):
        repository.__enter__()

    repository = artifact_module.PostgresSemanticGateArtifactV3Repository(
        object()
    )
    with pytest.raises(
        artifact_module.PostgresSemanticGateArtifactV3SchemaError,
        match="unsupported",
    ):
        repository._verify_schema_catalog(
            _ResultCursor([{
                "policy_count": 1,
                "rule_count": 0,
                "unsupported_relation_count": 0,
            }])
        )
    with pytest.raises(
        artifact_module.PostgresSemanticGateArtifactV3SchemaError,
        match="search_path",
    ):
        repository._lock_schema(
            _ResultCursor([{"search_path": None}]),
            for_write=False,
        )
    with pytest.raises(
        artifact_module.PostgresSemanticGateArtifactV3SchemaError,
        match="metadata mismatch",
    ):
        repository._lock_schema(
            _ResultCursor(
                [{"search_path": "public"}],
                [{
                    "active_version": 2,
                    "semantic_version": 1,
                    "semantic_contract": "tbm.semantic-gate-attempt.v3",
                    "artifact_version": 99,
                    "artifact_contract":
                        "tbm.semantic-gate-artifact.v3",
                }],
            ),
            for_write=False,
        )


def test_postgres_semantic_gate_artifact_validation_and_corrupt_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    assert response is not None
    repository = artifact_module.PostgresSemanticGateArtifactV3Repository(
        object()
    )
    with pytest.raises(ValueError, match="attempt must"):
        repository._validate_bundle(  # type: ignore[arg-type]
            object(),
            prompt,
            response,
        )
    with pytest.raises(ValueError, match="exactly Stored"):
        repository._validate_stored(  # type: ignore[arg-type]
            attempt,
            object(),
            expected_role="prompt",
        )
    with pytest.raises(ValueError, match="wrong role"):
        repository._validate_stored(
            attempt,
            response,
            expected_role="prompt",
        )
    encrypted_prompt, _ = _artifacts(
        attempt,
        encryption_key_id="kms_key_001",
    )
    with pytest.raises(ValueError, match="cannot claim"):
        repository._validate_stored(
            attempt,
            encrypted_prompt,
            expected_role="prompt",
        )
    with pytest.raises(ValueError, match="requires response"):
        repository._validate_bundle(attempt, prompt, None)
    failed = _failed_attempt(attempt)
    failed_prompt, _ = _artifacts(failed)
    with pytest.raises(ValueError, match="forbids response"):
        repository._validate_bundle(failed, failed_prompt, response)

    artifact = prompt.binding.artifact
    valid_row: dict[str, object] = {
        "attempt_id": attempt.attempt_id,
        "artifact_role": "prompt",
        "artifact_id": artifact.artifact_id,
        "content_sha256": artifact.content_sha256,
        "descriptor": tbm.dumps_semantic_gate_artifact_binding(
            prompt.binding
        ),
        "size_bytes": artifact.size_bytes,
        "media_type": artifact.media_type,
        "classification": artifact.classification,
        "created_at": artifact.created_at,
        "encryption_key_id": artifact.encryption_key_id,
        "redaction_policy_id": artifact.redaction_policy_id,
        "content": memoryview(prompt.content),
    }
    assert repository._stored_artifact(
        valid_row,
        attempt,
        expected_role="prompt",
    ) == prompt
    for replacement, message in (
        ({"descriptor": None}, "descriptor has invalid shape"),
        ({"content": 1}, "content has invalid shape"),
        ({"descriptor": "{"}, "failed validation"),
        ({"size_bytes": artifact.size_bytes + 1}, "columns do not match"),
    ):
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3PersistenceError,
            match=message,
        ):
            repository._stored_artifact(
                {**valid_row, **replacement},
                attempt,
                expected_role="prompt",
            )
    monkeypatch.setattr(
        artifact_module,
        "verify_semantic_gate_artifact_binding",
        lambda *_args: False,
    )
    with pytest.raises(ValueError, match="does not match"):
        repository._validate_stored(
            attempt,
            prompt,
            expected_role="prompt",
        )
    with pytest.raises(
        artifact_module.PostgresSemanticGateArtifactV3PersistenceError,
        match="does not match attempt",
    ):
        repository._stored_artifact(
            valid_row,
            attempt,
            expected_role="prompt",
        )


def test_postgres_semantic_gate_artifact_missing_states_and_error_mapping(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    assert response is not None
    with _repository(postgres_cluster) as repository:
        repository._semantic_repository.store_attempt(attempt)
        with pytest.raises(ValueError, match="attempt_id"):
            repository.load_attempt_with_artifacts("bad")

        selected = iter((None, None))
        monkeypatch.setattr(
            repository,
            "_select_stored",
            lambda *_args, **_kwargs: next(selected),
        )
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3NotFoundError,
            match="prompt artifact",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)

        selected = iter((prompt, None))
        monkeypatch.setattr(
            repository,
            "_select_stored",
            lambda *_args, **_kwargs: next(selected),
        )
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3PersistenceError,
            match="missing response",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)

        failed = _failed_attempt(attempt)
        failed_prompt, _ = _artifacts(failed)
        monkeypatch.setattr(
            repository._semantic_repository,
            "load_attempt",
            lambda _attempt_id: failed,
        )
        selected = iter((failed_prompt, response))
        monkeypatch.setattr(
            repository,
            "_select_stored",
            lambda *_args, **_kwargs: next(selected),
        )
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3PersistenceError,
            match="failed attempt has a response",
        ):
            repository.load_attempt_with_artifacts(failed.attempt_id)

    repository = artifact_module.PostgresSemanticGateArtifactV3Repository(
        object()
    )
    for sqlstate, error_type in (
        ("42P01", artifact_module.PostgresSemanticGateArtifactV3SchemaError),
        ("23505", artifact_module.PostgresSemanticGateArtifactV3ConflictError),
        ("P0001", artifact_module.PostgresSemanticGateArtifactV3ConflictError),
        ("XX000",
         artifact_module.PostgresSemanticGateArtifactV3PersistenceError),
        (None, artifact_module.PostgresSemanticGateArtifactV3PersistenceError),
    ):
        with pytest.raises(error_type):
            repository._raise_database(_DatabaseError(sqlstate))


def test_postgres_semantic_gate_artifact_readback_mismatch_rolls_back(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(postgres_cluster)
    _seed_evidence(postgres_cluster)
    _snapshot, _evaluation, attempt = _records()
    prompt, response = _artifacts(attempt)
    with _repository(postgres_cluster) as repository:
        monkeypatch.setattr(
            repository,
            "_select_stored",
            lambda *_args, **_kwargs: None,
        )
        with pytest.raises(
            artifact_module.PostgresSemanticGateArtifactV3PersistenceError,
            match="read-back",
        ):
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )
    with PostgresSemanticGateV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as semantic_repository:
        with pytest.raises(PostgresSemanticGateV3NotFoundError):
            semantic_repository.load_attempt(attempt.attempt_id)
