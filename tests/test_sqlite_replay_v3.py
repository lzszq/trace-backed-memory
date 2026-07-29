from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.replay_v3 import (
    REPLAY_COMPONENT_NAMES,
    DecisionReplayManifest,
    InjectionArtifact,
    build_decision_replay_manifest,
    create_content_addressed_artifact,
    create_injection_artifact,
    dumps_decision_replay_manifest,
    dumps_injection_artifact,
)
from trace_backed_memory.sqlite_replay_v3 import (
    SQLITE_REPLAY_V3_SCHEMA_VERSION,
    SQLiteReplayV3ConflictError,
    SQLiteReplayV3Error,
    SQLiteReplayV3PersistenceError,
    SQLiteReplayV3Repository,
    SQLiteReplayV3SchemaError,
)
from tests.test_usage_decision_v3 import _usage


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-27T00:00:00Z"


def _injection() -> tuple[InjectionArtifact, bytes]:
    snippet = "Use exact reviewed memory."
    return (
        create_injection_artifact(
            snippet,
            session_id="session_001",
            decision_id="decision_001",
            usage_decision_id="usage_001",
            memory_revision_ids=("revision_001",),
            renderer_id="renderer_001",
            renderer_version="1.0.0",
            policy_bundle_sha256="sha256:" + "a" * 64,
            rendered_at=NOW,
        ),
        snippet.encode(),
    )


def _manifest(
    injection: InjectionArtifact,
) -> DecisionReplayManifest:
    components = {
        name: "sha256:" + f"{index:x}" * 64
        for index, name in enumerate(REPLAY_COMPONENT_NAMES, start=1)
    }
    components["injection_artifact"] = injection.artifact.content_sha256
    return build_decision_replay_manifest(
        session_id=injection.session_id,
        decision_id=injection.decision_id,
        usage_decision_id=injection.usage_decision_id,
        component_hashes=components,
        injection_artifact_id=injection.artifact.artifact_id,
        completeness="complete",
        created_at=NOW,
    )


def _legacy_manifest() -> DecisionReplayManifest:
    components = {
        name: "sha256:" + f"{index:x}" * 64
        for index, name in enumerate(REPLAY_COMPONENT_NAMES, start=1)
    }
    components["semantic_gate_response"] = None
    components["injection_artifact"] = None
    return build_decision_replay_manifest(
        session_id="session_legacy",
        decision_id="decision_legacy",
        usage_decision_id="usage_legacy",
        component_hashes=components,
        injection_artifact_id=None,
        completeness="legacy_partial",
        created_at=NOW,
    )


def _replay_connection(*, foreign_keys: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    return connection


def _insert_manifest(
    connection: sqlite3.Connection,
    manifest: DecisionReplayManifest,
) -> None:
    connection.execute(
        "INSERT INTO v3_replay_manifests ("
        "manifest_sha256, session_id, decision_id, usage_decision_id, "
        "injection_artifact_id, completeness, descriptor"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            manifest.manifest_sha256,
            manifest.session_id,
            manifest.decision_id,
            manifest.usage_decision_id,
            manifest.injection_artifact_id,
            manifest.completeness,
            dumps_decision_replay_manifest(manifest),
        ),
    )


def _complete_bundle():
    snippet = b"bounded snippet"
    usage_created_at = "2026-07-30T01:00:00Z"
    provisional = tbm.create_content_addressed_artifact(
        snippet,
        media_type=tbm.INJECTION_ARTIFACT_MEDIA_TYPE,
        classification="internal",
        created_at=usage_created_at,
    )
    component_artifacts = []
    component_hashes = {}
    for name in REPLAY_COMPONENT_NAMES:
        if name == "injection_artifact":
            component_hashes[name] = provisional.content_sha256
            continue
        component_content = f"{name} component".encode()
        descriptor = tbm.create_content_addressed_artifact(
            component_content,
            media_type="application/octet-stream",
            classification="internal",
            created_at=NOW,
        )
        component_artifacts.append(
            tbm.StoredReplayArtifact(descriptor, component_content)
        )
        component_hashes[name] = descriptor.content_sha256
    components = tuple(
        (name, component_hashes[name]) for name in REPLAY_COMPONENT_NAMES
    )
    usage = _usage(
        replay_components=components,
        injection_artifact_id=provisional.artifact_id,
    )
    stored_usage = tbm.create_usage_decision_artifact(usage)
    injection = tbm.create_injection_artifact(
        snippet.decode(),
        session_id=usage.session_id,
        decision_id=usage.decision_id,
        usage_decision_id=usage.usage_decision_id,
        memory_revision_ids=usage.final_memory_revision_ids,
        renderer_id=usage.renderer_id,
        renderer_version=usage.renderer_version,
        policy_bundle_sha256=usage.policy_bundle_sha256,
        rendered_at=usage.created_at,
    )
    assert injection.artifact == provisional
    manifest = tbm.build_decision_replay_manifest(
        session_id=usage.session_id,
        decision_id=usage.decision_id,
        usage_decision_id=usage.usage_decision_id,
        component_hashes=dict(usage.replay_components),
        injection_artifact_id=injection.artifact.artifact_id,
        completeness="complete",
        created_at=usage.created_at,
    )
    return (
        (stored_usage, *component_artifacts),
        injection,
        snippet,
        manifest,
    )


def test_store_complete_bundle_is_atomic_and_requires_exact_usage_linkage():
    connection = _replay_connection()
    repository = SQLiteReplayV3Repository(connection)
    supporting, injection, snippet, manifest = _complete_bundle()
    invalid = tbm.StoredReplayArtifact(
        supporting[1].artifact,
        b"tampered",
    )
    invalid_supporting = (supporting[0], invalid, *supporting[2:])
    try:
        with pytest.raises(ValueError):
            repository.store_complete_bundle(
                invalid_supporting,
                injection,
                snippet,
                manifest,
            )
        with pytest.raises(KeyError):
            repository.load_artifact(supporting[0].artifact.artifact_id)

        stored = repository.store_complete_bundle(
            supporting,
            injection,
            snippet,
            manifest,
        )
        assert stored.artifact_inserted is True
        assert (
            repository.load_artifact(supporting[0].artifact.artifact_id)
            == supporting[0]
        )
        replayed = repository.store_complete_bundle(
            supporting,
            injection,
            snippet,
            manifest,
        )
        assert replayed.artifact_inserted is False
        assert replayed.injection_inserted is False
        assert replayed.manifest_inserted is False

        with pytest.raises(ValueError, match="usage and injection linkage"):
            repository.store_complete_bundle(
                (),
                injection,
                snippet,
                manifest,
            )
        with pytest.raises(ValueError, match="component set"):
            repository.store_complete_bundle(
                supporting[:-1],
                injection,
                snippet,
                manifest,
            )
    finally:
        repository.close()
        connection.close()


def test_store_artifact_is_exact_idempotent_and_round_trips_bytes():
    content = b"\x00exact replay bytes\xff"
    artifact = create_content_addressed_artifact(
        content,
        media_type="application/octet-stream",
        classification="internal",
        created_at=NOW,
    )
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    assert repository.store_artifact(artifact, content) is True
    assert repository.store_artifact(artifact, content) is False
    stored = repository.load_artifact(artifact.artifact_id)

    assert stored.artifact == artifact
    assert stored.content == content


def test_store_artifact_rejects_content_mismatch_and_metadata_conflict():
    content = b"exact replay bytes"
    artifact = create_content_addressed_artifact(
        content,
        media_type="application/octet-stream",
        classification="internal",
        created_at=NOW,
    )
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    with pytest.raises(ValueError, match="does not match"):
        repository.store_artifact(artifact, b"different")

    repository.store_artifact(artifact, content)
    conflicting = replace(artifact, media_type="text/plain")
    with pytest.raises(
        SQLiteReplayV3ConflictError,
        match="conflicting immutable content",
    ):
        repository.store_artifact(conflicting, content)


def test_store_artifact_rejects_sensitive_bytes_without_encryption_provider():
    content = b"sensitive plaintext"
    artifact = create_content_addressed_artifact(
        content,
        media_type="application/octet-stream",
        classification="confidential",
        created_at=NOW,
        encryption_key_id="kms_key_001",
    )
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    with pytest.raises(ValueError, match="encryption provider"):
        repository.store_artifact(artifact, content)


def test_store_injection_persists_descriptor_and_exact_content_atomically():
    injection, content = _injection()
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    first = repository.store_injection(injection, content)
    replay = repository.store_injection(injection, content)
    loaded, loaded_content = repository.load_injection(injection.artifact.artifact_id)

    assert first.artifact_inserted is True
    assert first.injection_inserted is True
    assert replay.artifact_inserted is False
    assert replay.injection_inserted is False
    assert loaded == injection
    assert loaded_content == content


def test_store_bundle_links_and_replays_all_records_in_one_transaction():
    injection, content = _injection()
    manifest = _manifest(injection)
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    result = repository.store_bundle(injection, content, manifest)
    replay = repository.store_bundle(injection, content, manifest)

    assert result.artifact_inserted is True
    assert result.injection_inserted is True
    assert result.manifest_inserted is True
    assert replay.artifact_inserted is False
    assert replay.injection_inserted is False
    assert replay.manifest_inserted is False
    assert repository.load_manifest(manifest.manifest_sha256) == manifest


def test_store_bundle_rejects_link_mismatch_without_partial_rows():
    injection, content = _injection()
    manifest = _manifest(injection)
    mismatched = build_decision_replay_manifest(
        session_id="session_other",
        decision_id=manifest.decision_id,
        usage_decision_id=manifest.usage_decision_id,
        component_hashes=dict(manifest.components),
        injection_artifact_id=manifest.injection_artifact_id,
        completeness="complete",
        created_at=NOW,
    )
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    with pytest.raises(ValueError, match="linkage"):
        repository.store_bundle(injection, content, mismatched)
    with pytest.raises(KeyError):
        repository.load_artifact(injection.artifact.artifact_id)


def test_store_bundle_rolls_back_new_rows_on_manifest_conflict():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    repository = SQLiteReplayV3Repository(connection)
    injection, content = _injection()
    manifest = _manifest(injection)
    connection.execute(
        "INSERT INTO v3_replay_manifests ("
        "manifest_sha256, session_id, decision_id, usage_decision_id, "
        "injection_artifact_id, completeness, descriptor"
        ") VALUES (?, ?, ?, ?, NULL, 'legacy_partial', '{}')",
        (
            manifest.manifest_sha256,
            "session_conflict",
            "decision_conflict",
            "usage_conflict",
        ),
    )
    connection.commit()

    with pytest.raises(SQLiteReplayV3ConflictError):
        repository.store_bundle(injection, content, manifest)
    with pytest.raises(KeyError):
        repository.load_artifact(injection.artifact.artifact_id)
    assert connection.execute(
        "SELECT count(*) FROM v3_replay_injections"
    ).fetchone() == (0,)


def test_manifest_requires_existing_exact_injection_linkage():
    injection, content = _injection()
    manifest = _manifest(injection)
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    with pytest.raises(
        SQLiteReplayV3ConflictError,
        match="unknown injection",
    ):
        repository.store_manifest(manifest)

    repository.store_injection(injection, content)
    mismatched = build_decision_replay_manifest(
        session_id="session_other",
        decision_id=manifest.decision_id,
        usage_decision_id=manifest.usage_decision_id,
        component_hashes=dict(manifest.components),
        injection_artifact_id=manifest.injection_artifact_id,
        completeness="complete",
        created_at=NOW,
    )
    with pytest.raises(
        SQLiteReplayV3ConflictError,
        match="linkage conflicts",
    ):
        repository.store_manifest(mismatched)


def test_legacy_partial_manifest_without_injection_is_storable():
    manifest = _legacy_manifest()
    repository = SQLiteReplayV3Repository.connect(initialize=True)

    assert repository.store_manifest(manifest) is True
    assert repository.store_manifest(manifest) is False
    assert repository.load_manifest(manifest.manifest_sha256) == manifest


def test_load_manifest_rejects_orphan_injection_reference():
    injection, _ = _injection()
    manifest = _manifest(injection)
    connection = _replay_connection(foreign_keys=False)
    _insert_manifest(connection, manifest)
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    repository = SQLiteReplayV3Repository(connection)

    with pytest.raises(
        SQLiteReplayV3PersistenceError,
        match="unknown injection",
    ):
        repository.load_manifest(manifest.manifest_sha256)


def test_load_manifest_rejects_invalid_referenced_injection():
    injection, content = _injection()
    manifest = _manifest(injection)
    connection = _replay_connection()
    repository = SQLiteReplayV3Repository(connection)
    repository.store_artifact(injection.artifact, content)
    connection.execute(
        "INSERT INTO v3_replay_injections ("
        "artifact_id, session_id, decision_id, usage_decision_id, descriptor"
        ") VALUES (?, ?, ?, ?, ?)",
        (
            injection.artifact.artifact_id,
            injection.session_id,
            injection.decision_id,
            injection.usage_decision_id,
            "{}",
        ),
    )
    _insert_manifest(connection, manifest)
    connection.commit()

    with pytest.raises(
        SQLiteReplayV3PersistenceError,
        match="descriptor failed validation",
    ):
        repository.load_manifest(manifest.manifest_sha256)


def test_load_manifest_rechecks_injection_linkage():
    injection, content = _injection()
    manifest = _manifest(injection)
    mismatched = build_decision_replay_manifest(
        session_id="session_other",
        decision_id=manifest.decision_id,
        usage_decision_id=manifest.usage_decision_id,
        component_hashes=dict(manifest.components),
        injection_artifact_id=manifest.injection_artifact_id,
        completeness="complete",
        created_at=NOW,
    )
    connection = _replay_connection()
    repository = SQLiteReplayV3Repository(connection)
    repository.store_injection(injection, content)
    _insert_manifest(connection, mismatched)
    connection.commit()

    with pytest.raises(
        SQLiteReplayV3PersistenceError,
        match="linkage differs",
    ):
        repository.load_manifest(mismatched.manifest_sha256)


def test_caller_transaction_uses_savepoint_and_outer_rollback_owns_commit():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    repository = SQLiteReplayV3Repository(connection)
    injection, content = _injection()

    connection.execute("BEGIN")
    repository.store_injection(injection, content)
    assert connection.in_transaction is True
    connection.rollback()

    with pytest.raises(KeyError):
        repository.load_injection(injection.artifact.artifact_id)


@pytest.mark.parametrize(
    ("table", "key_column"),
    (
        ("v3_replay_artifacts", "artifact_id"),
        ("v3_replay_injections", "artifact_id"),
        ("v3_replay_manifests", "manifest_sha256"),
    ),
)
def test_direct_sql_update_and_delete_are_immutable(
    table: str,
    key_column: str,
):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    repository = SQLiteReplayV3Repository(connection)
    injection, content = _injection()
    manifest = _manifest(injection)
    repository.store_bundle(injection, content, manifest)
    key = (
        manifest.manifest_sha256
        if table == "v3_replay_manifests"
        else injection.artifact.artifact_id
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            f"UPDATE {table} SET {key_column} = {key_column} WHERE {key_column} = ?",
            (key,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            f"DELETE FROM {table} WHERE {key_column} = ?",
            (key,),
        )


def test_load_detects_direct_sql_artifact_hash_corruption():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    repository = SQLiteReplayV3Repository(connection)
    digest = "sha256:" + "a" * 64
    artifact_id = "artifact_sha256_" + "a" * 64
    connection.execute(
        "INSERT INTO v3_replay_artifacts ("
        "artifact_id, content_sha256, size_bytes, media_type, "
        "classification, created_at, encryption_key_id, "
        "redaction_policy_id, content"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            digest,
            1,
            "application/octet-stream",
            "internal",
            NOW,
            None,
            None,
            b"x",
        ),
    )
    connection.commit()

    with pytest.raises(
        SQLiteReplayV3PersistenceError,
        match="do not match",
    ):
        repository.load_artifact(artifact_id)


def test_load_detects_descriptor_and_projection_mismatch():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    repository = SQLiteReplayV3Repository(connection)
    injection, content = _injection()
    repository.store_artifact(injection.artifact, content)
    connection.execute(
        "INSERT INTO v3_replay_injections ("
        "artifact_id, session_id, decision_id, usage_decision_id, descriptor"
        ") VALUES (?, ?, ?, ?, ?)",
        (
            injection.artifact.artifact_id,
            "session_projection_mismatch",
            injection.decision_id,
            injection.usage_decision_id,
            dumps_injection_artifact(injection),
        ),
    )
    connection.commit()

    with pytest.raises(
        SQLiteReplayV3PersistenceError,
        match="do not match descriptor",
    ):
        repository.load_injection(injection.artifact.artifact_id)


def test_concurrent_same_bundle_replay_has_one_inserter(tmp_path: Path):
    database = tmp_path / "replay.db"
    SQLiteReplayV3Repository.connect(
        database,
        initialize=True,
    ).close()
    injection, content = _injection()
    manifest = _manifest(injection)

    def store_once() -> tuple[bool, bool, bool]:
        with SQLiteReplayV3Repository.connect(
            database,
            timeout=10,
        ) as repository:
            result = repository.store_bundle(
                injection,
                content,
                manifest,
            )
            return (
                result.artifact_inserted,
                result.injection_inserted,
                result.manifest_inserted,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: store_once(), range(2)))

    assert sorted(results) == [
        (False, False, False),
        (True, True, True),
    ]


def test_schema_version_definition_and_foreign_key_drift_fail_closed():
    repository = SQLiteReplayV3Repository.connect(initialize=True)
    injection, content = _injection()
    repository._connection.execute("PRAGMA ignore_check_constraints = ON")
    repository._connection.execute(
        "UPDATE trace_backed_memory_v3_replay_schema SET schema_version = 2"
    )
    repository._connection.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(SQLiteReplayV3SchemaError, match="version mismatch"):
        repository.store_injection(injection, content)

    missing = SQLiteReplayV3Repository.connect()
    with pytest.raises(SQLiteReplayV3SchemaError, match="missing"):
        missing.store_injection(injection, content)

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    disabled = SQLiteReplayV3Repository(connection)
    with pytest.raises(SQLiteReplayV3SchemaError, match="foreign keys"):
        disabled.store_injection(injection, content)


def test_schema_object_drift_and_closed_repository_fail_stably():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")
    )
    connection.execute("DROP INDEX v3_replay_manifests_decision")
    repository = SQLiteReplayV3Repository(connection)
    injection, content = _injection()

    with pytest.raises(SQLiteReplayV3SchemaError, match="missing"):
        repository.store_injection(injection, content)

    repository.close()
    repository.close()
    with pytest.raises(SQLiteReplayV3Error, match="closed"):
        repository.load_artifact(injection.artifact.artifact_id)


def test_schema_version_and_canonical_resource_are_isolated():
    sql = (ROOT / "schemas" / "sqlite-v3-replay.sql").read_text(encoding="utf-8")

    assert SQLITE_REPLAY_V3_SCHEMA_VERSION == 1
    assert "trace_backed_memory_v3_replay_schema" in sql
    assert "trace_backed_memory_schema" not in sql
    assert "trace_backed_memory_v3_gate_session_schema" not in sql
    for name in (
        "SQLITE_REPLAY_V3_SCHEMA_VERSION",
        "SQLiteReplayV3ConflictError",
        "SQLiteReplayV3Error",
        "SQLiteReplayV3PersistenceError",
        "SQLiteReplayV3Repository",
        "SQLiteReplayV3SchemaError",
        "SQLiteReplayV3StoreResult",
        "StoredReplayArtifact",
    ):
        assert name in tbm.__all__
