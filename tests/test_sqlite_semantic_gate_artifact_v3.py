from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_semantic_gate_artifact_v3 as artifact_sqlite
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SCHEMA = (
    ROOT / "schemas" / "sqlite-v3-semantic-gate-artifacts.sql"
)
PROMPT = b"Review the exact bounded candidate set."
RESPONSE = b'{"decision":"allow","reason":"applicable"}'
NOW = "2026-07-27T08:02:00Z"


def _evidence() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    snapshot = tbm.loads_retrieval_snapshot(
        (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
    )
    evaluation = tbm.loads_system_gate_evaluation(
        (
            ROOT / "examples" / "system_gate_evaluation_v3.example.json"
        ).read_bytes()
    )
    return snapshot, evaluation


def _attempt(*, succeeded: bool = True) -> tbm.SemanticGateAttempt:
    _snapshot, evaluation = _evidence()
    example = tbm.loads_semantic_gate_attempt(
        (
            ROOT / "examples" / "semantic_gate_attempt_v3.example.json"
        ).read_bytes()
    )
    values = {
        key: value
        for key, value in example.__dict__.items()
        if key not in {"attempt_id", "contract_version"}
    }
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
    values.update(
        session_id=evaluation.session_id,
        retrieval_snapshot_id=evaluation.retrieval_snapshot_id,
        system_gate_evaluation_id=evaluation.evaluation_id,
        prompt_artifact_sha256=prompt_artifact.content_sha256,
        response_artifact_sha256=(
            response_artifact.content_sha256 if succeeded else None
        ),
        status="succeeded" if succeeded else "failed",
        decision_id="decision_001" if succeeded else None,
        final_allowed_revision_ids=(
            example.final_allowed_revision_ids if succeeded else ()
        ),
        final_blocked_revision_ids=(),
        reason=example.reason if succeeded else None,
        risk=example.risk if succeeded else None,
        recommended_injection=(
            example.recommended_injection if succeeded else None
        ),
        error_code=None if succeeded else "provider_timeout",
        output_tokens=example.output_tokens if succeeded else None,
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


def _seed_repository(
    database: str | Path = ":memory:",
) -> tbm.SQLiteSemanticGateArtifactV3Repository:
    snapshot, evaluation = _evidence()
    repository = tbm.SQLiteSemanticGateArtifactV3Repository.connect(
        database,
        initialize=True,
    )
    SQLiteGateEvidenceV3Repository(repository._connection).store_bundle(
        snapshot,
        evaluation,
    )
    return repository


def _seed_database(path: Path) -> None:
    with _seed_repository(path):
        pass


def test_sqlite_semantic_artifact_round_trip_and_exact_replay() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
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


def test_sqlite_semantic_artifact_failed_attempt_is_prompt_only() -> None:
    attempt = _attempt(succeeded=False)
    prompt, response = _artifacts(attempt)
    assert response is None
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, None)
        loaded = repository.load_attempt_with_artifacts(attempt.attempt_id)
        assert loaded.prompt == prompt
        assert loaded.response is None

        succeeded = _attempt()
        succeeded_prompt, _succeeded_response = _artifacts(succeeded)
        with pytest.raises(ValueError, match="requires response"):
            repository.store_attempt_with_artifacts(
                succeeded,
                succeeded_prompt,
                None,
            )


def test_sqlite_semantic_artifact_rejects_wrong_role_and_sensitive_bytes() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    assert response is not None
    with _seed_repository() as repository:
        with pytest.raises(ValueError, match="wrong role"):
            repository.store_attempt_with_artifacts(
                attempt,
                response,
                prompt,
            )
        restricted_prompt, restricted_response = _artifacts(
            attempt,
            classification="restricted",
            encryption_key_id="kms_key_001",
        )
        with pytest.raises(ValueError, match="encryption at rest"):
            repository.store_attempt_with_artifacts(
                attempt,
                restricted_prompt,
                restricted_response,
            )


def test_sqlite_semantic_artifact_conflict_rolls_back_new_attempt() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        artifact = prompt.binding.artifact
        repository._connection.execute(
            "INSERT INTO v3_semantic_gate_artifacts ("
            "artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
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
        repository._connection.commit()

        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3ConflictError,
            match="conflicting content",
        ):
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )
        with pytest.raises(tbm.SQLiteSemanticGateV3NotFoundError):
            tbm.SQLiteSemanticGateV3Repository(
                repository._connection
            ).load_attempt(attempt.attempt_id)


def test_sqlite_semantic_artifact_preserves_caller_transaction() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository._connection.execute(
            "CREATE TABLE caller_work (value TEXT NOT NULL)"
        )
        repository._connection.execute(
            "INSERT INTO caller_work VALUES ('before')"
        )
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        assert repository._connection.in_transaction
        repository._connection.rollback()
        assert repository._connection.execute(
            "SELECT * FROM caller_work"
        ).fetchall() == []
        with pytest.raises(tbm.SQLiteSemanticGateV3NotFoundError):
            tbm.SQLiteSemanticGateV3Repository(
                repository._connection
            ).load_attempt(attempt.attempt_id)


def test_sqlite_semantic_artifact_direct_mutation_and_replace_are_blocked() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        for statement in (
            "UPDATE v3_semantic_gate_artifacts SET content = X'00'",
            "DELETE FROM v3_semantic_gate_artifacts",
            "UPDATE v3_semantic_gate_artifact_bindings "
            "SET artifact_role = 'response'",
            "DELETE FROM v3_semantic_gate_artifact_bindings",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                repository._connection.execute(statement)
            repository._connection.rollback()

        repository._connection.execute("PRAGMA recursive_triggers = OFF")
        artifact_row = repository._artifact_row(prompt)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repository._connection.execute(
                "INSERT OR REPLACE INTO v3_semantic_gate_artifacts ("
                "artifact_id, content_sha256, size_bytes, media_type, "
                "classification, created_at, encryption_key_id, "
                "redaction_policy_id, content"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                artifact_row,
            )
        repository._connection.rollback()
        binding_row = repository._binding_row(prompt.binding)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repository._connection.execute(
                "INSERT OR REPLACE INTO v3_semantic_gate_artifact_bindings ("
                "attempt_id, artifact_role, artifact_id, content_sha256, "
                "descriptor) VALUES (?, ?, ?, ?, ?)",
                binding_row,
            )
        repository._connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repository._connection.execute(
                "INSERT OR REPLACE INTO "
                "trace_backed_memory_v3_semantic_gate_artifacts_schema "
                "(singleton, schema_version, contract_version) "
                "VALUES (1, 1, 'tbm.semantic-gate-artifact.v3')"
            )


def test_sqlite_semantic_artifact_detects_tamper_and_schema_drift() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        repository._connection.execute(
            "DROP TRIGGER v3_semantic_gate_artifacts_immutable_update"
        )
        repository._connection.execute(
            "UPDATE v3_semantic_gate_artifacts SET content = zeroblob(size_bytes)"
            " WHERE artifact_id = ?",
            (prompt.binding.artifact.artifact_id,),
        )
        repository._connection.commit()
        with pytest.raises(tbm.SQLiteSemanticGateArtifactV3SchemaError):
            repository.load_attempt_with_artifacts(attempt.attempt_id)


def test_sqlite_semantic_artifact_requires_its_schema() -> None:
    snapshot, evaluation = _evidence()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-evidence.sql",
        "schemas/sqlite-v3-semantic-gate.sql",
    ):
        connection.executescript(tbm.read_packaged_resource(resource).decode())
    SQLiteGateEvidenceV3Repository(connection).store_bundle(
        snapshot,
        evaluation,
    )
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    repository = tbm.SQLiteSemanticGateArtifactV3Repository(
        connection,
        owns_connection=True,
    )
    with repository:
        with pytest.raises(tbm.SQLiteSemanticGateArtifactV3SchemaError):
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )
        with pytest.raises(tbm.SQLiteSemanticGateV3NotFoundError):
            tbm.SQLiteSemanticGateV3Repository(
                connection
            ).load_attempt(attempt.attempt_id)


def test_sqlite_semantic_artifact_concurrent_exact_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-artifacts.sqlite3"
    _seed_database(database)
    attempt = _attempt()
    prompt, response = _artifacts(attempt)

    def store() -> bool:
        with tbm.SQLiteSemanticGateArtifactV3Repository.connect(
            database,
            timeout=5,
        ) as repository:
            return repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            ).attempt.inserted

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: store(), range(2)))
    assert sorted(results) == [False, True]


def test_sqlite_semantic_artifact_resource_and_exports() -> None:
    canonical = ARTIFACT_SCHEMA.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/sqlite-v3-semantic-gate-artifacts.sql"
    ) == canonical
    assert (
        tbm.SQLITE_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION
        == 1
    )
    expected = {
        "SQLITE_SEMANTIC_GATE_ARTIFACT_V3_SCHEMA_VERSION",
        "SQLiteSemanticGateArtifactV3ConflictError",
        "SQLiteSemanticGateArtifactV3Error",
        "SQLiteSemanticGateArtifactV3NotFoundError",
        "SQLiteSemanticGateArtifactV3PersistenceError",
        "SQLiteSemanticGateArtifactV3Repository",
        "SQLiteSemanticGateArtifactV3SchemaError",
        "SQLiteSemanticGateArtifactV3StoreResult",
        "StoredSemanticGateAttemptArtifacts",
    }
    assert expected <= set(tbm.__all__)


def test_sqlite_semantic_artifact_binding_trigger_rejects_wrong_digest() -> None:
    attempt = _attempt(succeeded=False)
    prompt, response = _artifacts(attempt)
    assert response is None
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            None,
        )
        other_binding = replace(
            prompt.binding,
            artifact_role="response",
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not match"):
            repository._connection.execute(
                "INSERT INTO v3_semantic_gate_artifact_bindings ("
                "attempt_id, artifact_role, artifact_id, content_sha256, "
                "descriptor) VALUES (?, ?, ?, ?, ?)",
                repository._binding_row(other_binding),
            )


def test_sqlite_semantic_artifact_database_rejects_forged_bytes_and_descriptor(
) -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    assert response is not None
    with _seed_repository() as repository:
        tbm.SQLiteSemanticGateV3Repository(
            repository._connection
        ).store_attempt(attempt)
        artifact = response.binding.artifact
        with pytest.raises(sqlite3.IntegrityError):
            repository._connection.execute(
                "INSERT INTO v3_semantic_gate_artifacts ("
                "artifact_id, content_sha256, size_bytes, media_type, "
                "classification, created_at, encryption_key_id, "
                "redaction_policy_id, content"
                ") VALUES (?, ?, 1, 'application/json', 'internal', ?, "
                "NULL, NULL, X'58')",
                (
                    artifact.artifact_id,
                    artifact.content_sha256,
                    artifact.created_at,
                ),
            )
        repository._connection.rollback()

        repository._connection.execute(
            "INSERT INTO v3_semantic_gate_artifacts ("
            "artifact_id, content_sha256, size_bytes, media_type, "
            "classification, created_at, encryption_key_id, "
            "redaction_policy_id, content"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            repository._artifact_row(response),
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not match"):
            repository._connection.execute(
                "INSERT INTO v3_semantic_gate_artifact_bindings ("
                "attempt_id, artifact_role, artifact_id, content_sha256, "
                "descriptor) VALUES (?, 'response', ?, ?, 'x')",
                (
                    attempt.attempt_id,
                    artifact.artifact_id,
                    artifact.content_sha256,
                ),
            )


def test_sqlite_semantic_artifact_detects_extra_managed_trigger() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        repository._connection.execute(
            "CREATE TRIGGER unexpected_artifact_trigger "
            "BEFORE INSERT ON v3_semantic_gate_artifacts "
            "BEGIN SELECT RAISE(ABORT, 'unexpected'); END"
        )
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3SchemaError,
            match="unexpected managed object",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)


def test_sqlite_semantic_artifact_detects_temporary_managed_trigger() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(
            attempt,
            prompt,
            response,
        )
        repository._connection.execute(
            "CREATE TEMP TRIGGER temp_artifact_trigger "
            "BEFORE INSERT ON main.v3_semantic_gate_artifacts "
            "BEGIN SELECT RAISE(ABORT, 'temp unexpected'); END"
        )
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3SchemaError,
            match="temporary shadow",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)


def test_sqlite_semantic_artifact_connection_lifecycle_and_validation() -> None:
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        tbm.SQLiteSemanticGateArtifactV3Repository(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="boolean"):
        tbm.SQLiteSemanticGateArtifactV3Repository.connect(  # type: ignore[arg-type]
            initialize=1
        )
    with pytest.raises(
        tbm.SQLiteSemanticGateArtifactV3PersistenceError,
        match="failed to connect",
    ):
        tbm.SQLiteSemanticGateArtifactV3Repository.connect(object())  # type: ignore[arg-type]

    owned = tbm.SQLiteSemanticGateArtifactV3Repository.connect()
    owned.close()
    owned.close()
    with pytest.raises(tbm.SQLiteSemanticGateArtifactV3Error, match="closed"):
        owned.__enter__()

    connection = sqlite3.connect(":memory:")
    borrowed = tbm.SQLiteSemanticGateArtifactV3Repository(connection)
    borrowed.close()
    assert connection.execute("SELECT 1").fetchone() == (1,)
    connection.close()

    with pytest.raises(
        tbm.SQLiteSemanticGateArtifactV3SchemaError,
        match="invalid definition",
    ):
        artifact_sqlite._normalized_schema_sql(None)
    with pytest.raises(ValueError, match="requires a BLOB"):
        artifact_sqlite._sqlite_sha256("not bytes")


def test_sqlite_semantic_artifact_schema_preconditions_and_metadata() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, response)
        repository._connection.execute("PRAGMA foreign_keys = OFF")
        cursor = repository._connection.cursor()
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3SchemaError,
            match="foreign keys",
        ):
            repository._require_artifact_schema(cursor)
        cursor.close()
        repository._connection.execute("PRAGMA foreign_keys = ON")
        repository._connection.execute("PRAGMA recursive_triggers = OFF")
        cursor = repository._connection.cursor()
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3SchemaError,
            match="recursive triggers",
        ):
            repository._require_artifact_schema(cursor)
        cursor.close()
        repository._connection.execute("PRAGMA recursive_triggers = ON")

        trigger_sql = repository._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = "
            "'v3_semantic_gate_artifacts_schema_"
            "immutable_delete'"
        ).fetchone()
        assert trigger_sql is not None
        repository._connection.execute(
            "DROP TRIGGER "
            "v3_semantic_gate_artifacts_schema_immutable_delete"
        )
        repository._connection.execute(
            "DELETE FROM trace_backed_memory_v3_semantic_gate_artifacts_schema"
        )
        repository._connection.execute(trigger_sql[0])
        repository._connection.commit()
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3SchemaError,
            match="metadata mismatch",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)


class _ArtifactRowCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def execute(
        self,
        _statement: str,
        _parameters: tuple[object, ...],
    ) -> None:
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FaultConnection(sqlite3.Connection):
    fail_commit_once = False
    fail_artifact_release_once = False
    fail_artifact_rollback_once = False
    fail_rollback_once = False

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
        /,
    ) -> sqlite3.Cursor:
        normalized = statement.strip().upper()
        artifact_savepoint = "TBM_SQLITE_SEMANTIC_GATE_ARTIFACT_V3_"
        if (
            self.fail_artifact_release_once
            and normalized.startswith("RELEASE SAVEPOINT")
            and artifact_savepoint in normalized
        ):
            self.fail_artifact_release_once = False
            raise sqlite3.OperationalError("injected artifact release failure")
        if (
            self.fail_artifact_rollback_once
            and normalized.startswith("ROLLBACK TO SAVEPOINT")
            and artifact_savepoint in normalized
        ):
            self.fail_artifact_rollback_once = False
            raise sqlite3.OperationalError("injected artifact rollback failure")
        return super().execute(statement, parameters)

    def commit(self) -> None:
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise sqlite3.OperationalError("injected commit failure")
        super().commit()

    def rollback(self) -> None:
        if self.fail_rollback_once:
            self.fail_rollback_once = False
            raise sqlite3.OperationalError("injected rollback failure")
        super().rollback()


def _seed_fault_repository() -> tuple[
    tbm.SQLiteSemanticGateArtifactV3Repository,
    _FaultConnection,
]:
    snapshot, evaluation = _evidence()
    connection = sqlite3.connect(":memory:", factory=_FaultConnection)
    assert isinstance(connection, _FaultConnection)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-gate-evidence.sql",
        "schemas/sqlite-v3-semantic-gate.sql",
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
    ):
        connection.executescript(tbm.read_packaged_resource(resource).decode())
    SQLiteGateEvidenceV3Repository(connection).store_bundle(
        snapshot,
        evaluation,
    )
    return (
        tbm.SQLiteSemanticGateArtifactV3Repository(
            connection,
            owns_connection=True,
        ),
        connection,
    )


def test_sqlite_semantic_artifact_rejects_corrupt_loaded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    prompt, _response = _artifacts(attempt)
    with _seed_repository() as repository:
        valid_row = (
            *repository._binding_row(prompt.binding),
            *repository._artifact_row(prompt)[2:],
        )
        for row, message in (
            (("short",), "invalid shape"),
            (
                (*valid_row[:4], "{", *valid_row[5:]),
                "failed validation",
            ),
            (
                (*valid_row[:5], 1, *valid_row[6:]),
                "do not match descriptor",
            ),
        ):
            with pytest.raises(
                tbm.SQLiteSemanticGateArtifactV3PersistenceError,
                match=message,
            ):
                repository._load_stored(  # type: ignore[arg-type]
                    _ArtifactRowCursor(row),
                    attempt,
                    "prompt",
                )

        monkeypatch.setattr(
            artifact_sqlite,
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
            tbm.SQLiteSemanticGateArtifactV3PersistenceError,
            match="does not match attempt",
        ):
            repository._load_stored(  # type: ignore[arg-type]
                _ArtifactRowCursor(valid_row),
                attempt,
                "prompt",
            )


def test_sqlite_semantic_artifact_rejects_invalid_loaded_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    assert response is not None
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, response)

        with pytest.raises(ValueError, match="attempt_id"):
            repository.load_attempt_with_artifacts("bad")

        loads = iter((None, None))
        monkeypatch.setattr(
            repository,
            "_load_stored",
            lambda *_args: next(loads),
        )
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3NotFoundError,
            match="prompt artifact",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)

        loads = iter((prompt, None))
        monkeypatch.setattr(
            repository,
            "_load_stored",
            lambda *_args: next(loads),
        )
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3PersistenceError,
            match="missing response",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)

    failed = _attempt(succeeded=False)
    failed_prompt, _ = _artifacts(failed)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(failed, failed_prompt, None)
        loads = iter((failed_prompt, response))
        monkeypatch.setattr(
            repository,
            "_load_stored",
            lambda *_args: next(loads),
        )
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3PersistenceError,
            match="failed attempt has a response",
        ):
            repository.load_attempt_with_artifacts(failed.attempt_id)


def test_sqlite_semantic_artifact_maps_sqlite_load_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, response)
        monkeypatch.setattr(
            repository._semantic_repository,
            "load_attempt",
            lambda _attempt_id: (_ for _ in ()).throw(
                sqlite3.OperationalError("no such table: missing")
            ),
        )
        with pytest.raises(tbm.SQLiteSemanticGateArtifactV3SchemaError):
            repository.load_attempt_with_artifacts(attempt.attempt_id)

        monkeypatch.setattr(
            repository._semantic_repository,
            "load_attempt",
            lambda _attempt_id: (_ for _ in ()).throw(
                sqlite3.OperationalError("disk I/O error")
            ),
        )
        with pytest.raises(tbm.SQLiteSemanticGateArtifactV3PersistenceError):
            repository.load_attempt_with_artifacts(attempt.attempt_id)


def test_sqlite_semantic_artifact_bundle_and_readback_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    assert response is not None
    with pytest.raises(ValueError, match="attempt must"):
        artifact_sqlite.SQLiteSemanticGateArtifactV3Repository._validate_bundle(
            object(),  # type: ignore[arg-type]
            prompt,
            response,
        )
    with pytest.raises(ValueError, match="exactly Stored"):
        artifact_sqlite.SQLiteSemanticGateArtifactV3Repository._validate_stored(
            attempt,
            object(),  # type: ignore[arg-type]
            expected_role="prompt",
        )
    failed = _attempt(succeeded=False)
    failed_prompt, _ = _artifacts(failed)
    with pytest.raises(ValueError, match="forbids response"):
        artifact_sqlite.SQLiteSemanticGateArtifactV3Repository._validate_bundle(
            failed,
            failed_prompt,
            response,
        )

    with _seed_repository() as repository:
        monkeypatch.setattr(
            repository,
            "_load_stored",
            lambda *_args: None,
        )
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3PersistenceError,
            match="read-back",
        ):
            repository.store_attempt_with_artifacts(
                attempt,
                prompt,
                response,
            )


def test_sqlite_semantic_artifact_transaction_failure_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)

    repository, connection = _seed_fault_repository()
    connection.fail_commit_once = True
    with pytest.raises(
        tbm.SQLiteSemanticGateArtifactV3PersistenceError,
        match="operation failed",
    ):
        repository.store_attempt_with_artifacts(attempt, prompt, response)
    assert not connection.in_transaction
    repository.close()

    repository, connection = _seed_fault_repository()
    connection.execute("BEGIN")
    connection.fail_artifact_release_once = True
    with pytest.raises(
        tbm.SQLiteSemanticGateArtifactV3PersistenceError,
        match="operation failed",
    ):
        repository.store_attempt_with_artifacts(attempt, prompt, response)
    assert connection.in_transaction
    connection.rollback()
    repository.close()

    repository, connection = _seed_fault_repository()
    connection.execute("BEGIN")
    connection.fail_artifact_rollback_once = True
    connection.fail_rollback_once = True
    monkeypatch.setattr(
        repository,
        "_put_artifact",
        lambda *_args: (_ for _ in ()).throw(
            sqlite3.IntegrityError("injected integrity failure")
        ),
    )
    with pytest.raises(
        tbm.SQLiteSemanticGateArtifactV3ConflictError,
        match="immutable storage",
    ) as failure:
        repository.store_attempt_with_artifacts(attempt, prompt, response)
    assert failure.value.__cause__ is not None
    notes = failure.value.__cause__.__notes__
    assert any("failed to roll back" in note for note in notes)
    assert any("left the outer" in note for note in notes)
    assert repository._closed is True


def test_sqlite_semantic_artifact_detects_definition_drift() -> None:
    attempt = _attempt()
    prompt, response = _artifacts(attempt)
    with _seed_repository() as repository:
        repository.store_attempt_with_artifacts(attempt, prompt, response)
        repository._connection.execute(
            "DROP TRIGGER v3_semantic_gate_artifacts_verify_content"
        )
        repository._connection.execute(
            "CREATE TRIGGER v3_semantic_gate_artifacts_verify_content "
            "BEFORE INSERT ON v3_semantic_gate_artifacts "
            "WHEN NEW.content_sha256 <> tbm_sha256(NEW.content) OR 1 = 0 "
            "BEGIN SELECT RAISE(ABORT, 'invalid artifact content'); END"
        )
        repository._connection.commit()
        with pytest.raises(
            tbm.SQLiteSemanticGateArtifactV3SchemaError,
            match="definitions do not match",
        ):
            repository.load_attempt_with_artifacts(attempt.attempt_id)
