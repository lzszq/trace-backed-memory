from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory import postgres_replay_v3
from trace_backed_memory.replay_v3 import (
    REPLAY_COMPONENT_NAMES,
    DecisionReplayManifest,
    InjectionArtifact,
    build_decision_replay_manifest,
    create_injection_artifact,
)
from tests.postgres_support import PostgresCluster, assert_sql_succeeds
from tests.test_usage_decision_v3 import _usage


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-replay.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-replay-rollback.sql"
ARTIFACT_ID = "artifact_sha256_" + "a" * 64
DIGEST = "sha256:" + "a" * 64
ARTIFACT_ID_B = "artifact_sha256_" + "b" * 64
DIGEST_B = "sha256:" + "b" * 64
NOW = "2026-07-27T00:00:00Z"


class _RowsCursor:
    def __init__(self, *responses: list[object]) -> None:
        self.responses = list(responses)
        self.current: list[object] = []

    def execute(
        self,
        _query: str,
        _parameters: tuple[object, ...] = (),
    ) -> None:
        self.current = self.responses.pop(0)

    def fetchall(self) -> list[object]:
        return self.current


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    installed = cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr


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
            policy_bundle_sha256=DIGEST,
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


def _insert_bundle_sql() -> str:
    return (
        "INSERT INTO trace_backed_memory_v3_replay.replay_artifacts "
        "(artifact_id, content_sha256, size_bytes, media_type, "
        "classification, created_at, encryption_key_id, "
        "redaction_policy_id, content) VALUES "
        f"('{ARTIFACT_ID}', '{DIGEST}', 2, "
        "'text/plain; charset=utf-8', 'internal', "
        "'2026-07-27T00:00:00Z', NULL, NULL, "
        "pg_catalog.decode('6869', 'hex')); "
        "INSERT INTO trace_backed_memory_v3_replay.replay_injections "
        "(artifact_id, session_id, decision_id, usage_decision_id, "
        "descriptor) VALUES "
        f"('{ARTIFACT_ID}', 'session_001', 'decision_001', "
        "'usage_001', '{}'); "
        "INSERT INTO trace_backed_memory_v3_replay.replay_manifests "
        "(manifest_sha256, session_id, decision_id, usage_decision_id, "
        "injection_artifact_id, completeness, descriptor) VALUES "
        f"('{DIGEST}', 'session_001', 'decision_001', 'usage_001', "
        f"'{ARTIFACT_ID}', 'complete', '{{}}');"
    )


def test_postgres_replay_schema_install_invariants_and_rollback(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT schema_version, contract_version "
            "FROM trace_backed_memory_v3_replay.schema_metadata",
        )
        == "1|tbm.replay.v3"
    )
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            _insert_bundle_sql() + " SELECT pg_catalog.encode(content, 'hex') "
            "FROM trace_backed_memory_v3_replay.replay_artifacts;",
        )
        == "6869"
    )

    for statement in (
        "UPDATE trace_backed_memory_v3_replay.replay_artifacts "
        "SET media_type = 'application/octet-stream'",
        "DELETE FROM trace_backed_memory_v3_replay.replay_injections",
        "TRUNCATE trace_backed_memory_v3_replay.replay_manifests",
        "UPDATE trace_backed_memory_v3_replay.schema_metadata SET schema_version = 2",
    ):
        rejected = postgres_cluster.run(statement)
        assert rejected.returncode != 0
        assert "immutable" in rejected.stderr

    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_replay') IS NULL",
        )
        == "t"
    )
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT schema_version FROM public.trace_backed_memory_schema",
        )
        == "2"
    )


def test_postgres_replay_schema_rejects_invalid_linkage_and_is_atomic(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    missing_artifact = postgres_cluster.run(
        "INSERT INTO trace_backed_memory_v3_replay.replay_injections "
        "(artifact_id, session_id, decision_id, usage_decision_id, "
        "descriptor) VALUES "
        f"('{ARTIFACT_ID}', 'session_001', 'decision_001', "
        "'usage_001', '{}')"
    )
    assert missing_artifact.returncode != 0
    assert "foreign key" in missing_artifact.stderr.lower()

    incomplete_complete = postgres_cluster.run(
        "INSERT INTO trace_backed_memory_v3_replay.replay_manifests "
        "(manifest_sha256, session_id, decision_id, usage_decision_id, "
        "injection_artifact_id, completeness, descriptor) VALUES "
        f"('{DIGEST}', 'session_001', 'decision_001', 'usage_001', "
        "NULL, 'complete', '{}')"
    )
    assert incomplete_complete.returncode != 0
    assert "replay_manifests_injection_shape" in incomplete_complete.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT count(*) FROM trace_backed_memory_v3_replay.replay_injections",
        )
        == "0"
    )

    assert_sql_succeeds(postgres_cluster, _insert_bundle_sql())
    mismatched_linkage = postgres_cluster.run(
        "INSERT INTO trace_backed_memory_v3_replay.replay_manifests "
        "(manifest_sha256, session_id, decision_id, usage_decision_id, "
        "injection_artifact_id, completeness, descriptor) VALUES "
        f"('{DIGEST_B}', 'other_session', 'decision_001', 'usage_001', "
        f"'{ARTIFACT_ID}', 'complete', '{{}}')"
    )
    assert mismatched_linkage.returncode != 0
    assert "foreign key" in mismatched_linkage.stderr.lower()


def test_postgres_replay_schema_enforces_derived_and_injection_shapes(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    wrong_derived_id = postgres_cluster.run(
        "INSERT INTO trace_backed_memory_v3_replay.replay_artifacts "
        "(artifact_id, content_sha256, size_bytes, media_type, "
        "classification, created_at, encryption_key_id, "
        "redaction_policy_id, content) VALUES "
        f"('{ARTIFACT_ID}', '{DIGEST_B}', 2, "
        "'text/plain; charset=utf-8', 'internal', "
        "'2026-07-27T00:00:00Z', NULL, NULL, "
        "pg_catalog.decode('6869', 'hex'))"
    )
    assert wrong_derived_id.returncode != 0
    assert "replay_artifacts_derived_id_check" in wrong_derived_id.stderr

    assert_sql_succeeds(
        postgres_cluster,
        "INSERT INTO trace_backed_memory_v3_replay.replay_artifacts "
        "(artifact_id, content_sha256, size_bytes, media_type, "
        "classification, created_at, encryption_key_id, "
        "redaction_policy_id, content) VALUES "
        f"('{ARTIFACT_ID_B}', '{DIGEST_B}', 2, "
        "'application/octet-stream', 'internal', "
        "'2026-07-27T00:00:00Z', NULL, NULL, "
        "pg_catalog.decode('6869', 'hex'))",
    )
    invalid_injection = postgres_cluster.run(
        "INSERT INTO trace_backed_memory_v3_replay.replay_injections "
        "(artifact_id, session_id, decision_id, usage_decision_id, "
        "descriptor) VALUES "
        f"('{ARTIFACT_ID_B}', 'session_001', 'decision_001', "
        "'usage_001', '{}')"
    )
    assert invalid_injection.returncode != 0
    assert "injection artifact shape is invalid" in invalid_injection.stderr


def test_postgres_replay_rollback_fails_closed_on_catalog_drift(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    assert_sql_succeeds(
        postgres_cluster,
        "CREATE TABLE trace_backed_memory_v3_replay.unexpected (value integer)",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog mismatch" in rejected.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regclass("
            "'trace_backed_memory_v3_replay.unexpected') IS NOT NULL",
        )
        == "t"
    )


def test_postgres_replay_install_and_rollback_require_active_v2(
    postgres_cluster: PostgresCluster,
):
    postgres_cluster.load_schema()
    assert_sql_succeeds(
        postgres_cluster,
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 1 WHERE singleton",
    )
    rejected_install = postgres_cluster.run_script(INSTALL)
    assert rejected_install.returncode != 0
    assert "requires active schema version 2" in rejected_install.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_replay') IS NULL",
        )
        == "t"
    )

    assert_sql_succeeds(
        postgres_cluster,
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 2 WHERE singleton",
    )
    installed = postgres_cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr
    assert_sql_succeeds(
        postgres_cluster,
        "UPDATE public.trace_backed_memory_schema "
        "SET schema_version = 1 WHERE singleton",
    )
    rejected_rollback = postgres_cluster.run_script(ROLLBACK)
    assert rejected_rollback.returncode != 0
    assert "requires active schema version 2" in rejected_rollback.stderr
    assert (
        assert_sql_succeeds(
            postgres_cluster,
            "SELECT pg_catalog.to_regnamespace("
            "'trace_backed_memory_v3_replay') IS NOT NULL",
        )
        == "t"
    )


def test_postgres_replay_repository_bundle_round_trip_and_idempotency(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    manifest = _manifest(injection)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        stored = repository.store_bundle(injection, content, manifest)
        assert stored.artifact_inserted is True
        assert stored.injection_inserted is True
        assert stored.manifest_inserted is True
        assert repository.load_artifact(
            injection.artifact.artifact_id
        ) == tbm.StoredReplayArtifact(injection.artifact, content)
        assert repository.load_injection(injection.artifact.artifact_id) == (
            injection,
            content,
        )
        assert repository.load_manifest(manifest.manifest_sha256) == manifest

        replayed = repository.store_bundle(injection, content, manifest)
        assert replayed.artifact_inserted is False
        assert replayed.injection_inserted is False
        assert replayed.manifest_inserted is False
        legacy = _legacy_manifest()
        assert repository.store_manifest(legacy) is True
        assert repository.store_manifest(legacy) is False
        assert repository.load_manifest(legacy.manifest_sha256) == legacy
        with pytest.raises(KeyError):
            repository.load_manifest(DIGEST_B)
    assert tbm.POSTGRES_REPLAY_V3_SCHEMA_VERSION == 1
    for name in (
        "PostgresReplayV3ConflictError",
        "PostgresReplayV3Error",
        "PostgresReplayV3PersistenceError",
        "PostgresReplayV3Repository",
        "PostgresReplayV3SchemaError",
        "PostgresReplayV3StoreResult",
    ):
        assert name in tbm.__all__


def test_postgres_complete_bundle_is_atomic_and_usage_bound(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    supporting, injection, snippet, manifest = _complete_bundle()
    invalid = tbm.StoredReplayArtifact(
        supporting[1].artifact,
        b"tampered",
    )
    invalid_supporting = (supporting[0], invalid, *supporting[2:])
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
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
        exported = tbm.export_replay_bundle(
            repository,
            manifest.manifest_sha256,
            allowed_classifications=frozenset({"internal"}),
        )
        assert exported.manifest == manifest
        assert tbm.verify_replay_bundle_export(exported)
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


def test_postgres_replay_repository_connect_lifecycle_and_validation(
    postgres_cluster: PostgresCluster,
):
    _install(postgres_cluster)
    with tbm.PostgresReplayV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as repository:
        legacy = _legacy_manifest()
        assert repository.store_manifest(legacy) is True
        assert repository.load_manifest(legacy.manifest_sha256) == legacy
    repository.close()
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="closed",
    ):
        repository.load_manifest(_legacy_manifest().manifest_sha256)
    with pytest.raises(ValueError, match="connection is required"):
        tbm.PostgresReplayV3Repository(None)
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        open_repository = tbm.PostgresReplayV3Repository(connection)
        with pytest.raises(ValueError, match="artifact_id"):
            open_repository.load_artifact("bad")
        with pytest.raises(ValueError, match="manifest_sha256"):
            open_repository.load_manifest("bad")
        open_repository.close()
        assert connection.closed is False


def test_postgres_replay_repository_individual_store_paths_and_type_checks(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    manifest = _manifest(injection)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        stored = repository.store_injection(injection, content)
        assert stored.artifact_inserted is True
        assert stored.injection_inserted is True
        replayed = repository.store_injection(injection, content)
        assert replayed.artifact_inserted is False
        assert replayed.injection_inserted is False
        assert repository.store_manifest(manifest) is True
        assert repository.store_manifest(manifest) is False
        with pytest.raises(ValueError, match="injection"):
            repository.store_injection(object(), content)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="manifest"):
            repository.store_manifest(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="exact replay records"):
            repository.store_bundle(
                object(),  # type: ignore[arg-type]
                content,
                manifest,
            )
        conflicting_manifest = build_decision_replay_manifest(
            session_id="session_other",
            decision_id=manifest.decision_id,
            usage_decision_id=manifest.usage_decision_id,
            component_hashes=dict(manifest.components),
            injection_artifact_id=manifest.injection_artifact_id,
            completeness=manifest.completeness,
            created_at=manifest.created_at,
        )
        with pytest.raises(ValueError, match="linkage"):
            repository.store_bundle(
                injection,
                content,
                conflicting_manifest,
            )


def test_postgres_replay_repository_maps_missing_schema_for_every_operation(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    postgres_cluster.load_schema()
    injection, content = _injection()
    manifest = _manifest(injection)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        operations = (
            lambda: repository.store_artifact(injection.artifact, content),
            lambda: repository.store_injection(injection, content),
            lambda: repository.store_manifest(manifest),
            lambda: repository.store_bundle(injection, content, manifest),
            lambda: repository.load_artifact(injection.artifact.artifact_id),
            lambda: repository.load_injection(injection.artifact.artifact_id),
            lambda: repository.load_manifest(manifest.manifest_sha256),
        )
        for operation in operations:
            with pytest.raises(
                tbm.PostgresReplayV3SchemaError,
                match="missing or incomplete",
            ):
                operation()
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="synthetic database failure",
    ):
        tbm.PostgresReplayV3Repository._raise_database_error(
            RuntimeError(),
            "synthetic database failure",
        )


def test_postgres_replay_repository_maps_driver_errors_for_every_operation(
    postgres_cluster: PostgresCluster,
    monkeypatch: pytest.MonkeyPatch,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    manifest = _manifest(injection)
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)

        def fail_lock(_cursor: object) -> None:
            raise psycopg.OperationalError("synthetic driver failure")

        monkeypatch.setattr(repository, "_lock_schema", fail_lock)
        operations = (
            lambda: repository.store_artifact(injection.artifact, content),
            lambda: repository.store_injection(injection, content),
            lambda: repository.store_manifest(manifest),
            lambda: repository.store_bundle(injection, content, manifest),
            lambda: repository.load_artifact(injection.artifact.artifact_id),
            lambda: repository.load_injection(injection.artifact.artifact_id),
            lambda: repository.load_manifest(manifest.manifest_sha256),
        )
        for operation in operations:
            with pytest.raises(
                tbm.PostgresReplayV3PersistenceError,
                match="failed to",
            ):
                operation()


def test_postgres_replay_repository_connect_and_schema_resource_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    psycopg = pytest.importorskip("psycopg")

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("synthetic connection failure")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="failed to connect",
    ):
        tbm.PostgresReplayV3Repository.connect("postgresql://invalid")

    postgres_replay_v3._expected_function_bodies.cache_clear()
    monkeypatch.setattr(
        postgres_replay_v3,
        "read_packaged_resource",
        lambda _path: b"\xff",
    )
    with pytest.raises(
        tbm.PostgresReplayV3SchemaError,
        match="could not read",
    ):
        postgres_replay_v3._expected_function_bodies()
    monkeypatch.setattr(
        postgres_replay_v3,
        "read_packaged_resource",
        lambda _path: b"SELECT 1;",
    )
    with pytest.raises(
        tbm.PostgresReplayV3SchemaError,
        match="functions are incomplete",
    ):
        postgres_replay_v3._expected_function_bodies()
    postgres_replay_v3._expected_function_bodies.cache_clear()


def test_postgres_replay_repository_rejects_metadata_and_catalog_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.trace_backed_memory_schema "
                "SET schema_version = 1 WHERE singleton"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresReplayV3SchemaError, match="metadata"):
            repository.store_artifact(injection.artifact, content)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.trace_backed_memory_schema "
                "SET schema_version = 2 WHERE singleton"
            )
            cursor.execute(
                "CREATE TABLE trace_backed_memory_v3_replay.unexpected (value integer)"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresReplayV3SchemaError, match="definitions"):
            repository.store_artifact(injection.artifact, content)
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE trace_backed_memory_v3_replay.unexpected")
            cursor.execute(
                "ALTER TABLE "
                "trace_backed_memory_v3_replay.replay_artifacts "
                "ADD COLUMN unexpected integer"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresReplayV3SchemaError, match="definitions"):
            repository.store_artifact(injection.artifact, content)
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE "
                "trace_backed_memory_v3_replay.replay_artifacts "
                "DROP COLUMN unexpected"
            )
            cursor.execute(
                "CREATE OR REPLACE FUNCTION "
                "trace_backed_memory_v3_replay."
                "reject_replay_artifact_mutation() RETURNS trigger "
                "LANGUAGE plpgsql SET search_path = pg_catalog AS "
                "$body$ BEGIN RETURN OLD; END $body$"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresReplayV3SchemaError, match="definitions"):
            repository.store_artifact(injection.artifact, content)
    with pytest.raises(tbm.PostgresReplayV3SchemaError, match="definitions"):
        tbm.PostgresReplayV3Repository._catalog_names(
            _RowsCursor([{"name": 1}]),
            "SELECT name",
        )


def test_postgres_replay_repository_accepts_canonical_equivalent_timestamps(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    snippet = "Use exact reviewed memory."
    content = snippet.encode()
    injection = create_injection_artifact(
        snippet,
        session_id="session_offset",
        decision_id="decision_offset",
        usage_decision_id="usage_offset",
        memory_revision_ids=("revision_001",),
        renderer_id="renderer_001",
        renderer_version="1.0.0",
        policy_bundle_sha256=DIGEST,
        rendered_at="2026-07-27T08:00:00+08:00",
    )
    manifest = _manifest(injection)
    manifest = replace(
        manifest,
        created_at="2026-07-27T08:00:00+08:00",
    )
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        stored = repository.store_bundle(injection, content, manifest)
        assert stored.artifact_inserted is True
        assert stored.injection_inserted is True
        assert stored.manifest_inserted is True
        loaded_injection, loaded_content = repository.load_injection(
            injection.artifact.artifact_id
        )
        loaded_manifest = repository.load_manifest(manifest.manifest_sha256)
        assert loaded_content == content
        assert loaded_injection.artifact.created_at == NOW
        assert loaded_injection.rendered_at == NOW
        assert loaded_manifest.created_at == NOW
        replayed = repository.store_bundle(injection, content, manifest)
        assert replayed.artifact_inserted is False
        assert replayed.injection_inserted is False
        assert replayed.manifest_inserted is False


def test_postgres_replay_repository_defensive_row_validation():
    injection, content = _injection()
    manifest = _manifest(injection)
    artifact_fields = (
        "artifact_id",
        "content_sha256",
        "size_bytes",
        "media_type",
        "classification",
        "created_at",
        "encryption_key_id",
        "redaction_policy_id",
        "content",
    )
    artifact_row = dict(
        zip(
            artifact_fields,
            postgres_replay_v3.PostgresReplayV3Repository._artifact_values(
                injection.artifact,
                content,
            ),
            strict=True,
        )
    )
    memoryview_row = dict(artifact_row)
    memoryview_row["content"] = memoryview(content)
    assert (
        postgres_replay_v3.PostgresReplayV3Repository._stored_artifact(
            memoryview_row
        ).content
        == content
    )
    malformed_rows = (
        {**artifact_row, "content": "not-bytes"},
        {**artifact_row, "media_type": 1},
        {
            **artifact_row,
            "classification": "confidential",
            "encryption_key_id": "key_001",
        },
    )
    for row in malformed_rows:
        with pytest.raises(tbm.PostgresReplayV3PersistenceError):
            postgres_replay_v3.PostgresReplayV3Repository._stored_artifact(row)
    sensitive_descriptor_row = {
        key: value
        for key, value in malformed_rows[2].items()
        if key != "content"
    }
    sensitive_descriptor_row["content_size"] = len(content)
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="preflight sensitive",
    ):
        postgres_replay_v3.PostgresReplayV3Repository(
            object()
        )._load_artifact_descriptor(
            _RowsCursor([sensitive_descriptor_row]),
            injection.artifact.artifact_id,
        )
    with pytest.raises(tbm.PostgresReplayV3PersistenceError, match="shape"):
        postgres_replay_v3.PostgresReplayV3Repository._mapping_values(
            {},
            ("missing",),
        )

    injection_fields = (
        "artifact_id",
        "session_id",
        "decision_id",
        "usage_decision_id",
        "descriptor",
    )
    injection_row = dict(
        zip(
            injection_fields,
            postgres_replay_v3.PostgresReplayV3Repository._injection_values(injection),
            strict=True,
        )
    )
    for row in (
        {**injection_row, "descriptor": 1},
        {**injection_row, "descriptor": "{}"},
        {**injection_row, "session_id": "different"},
    ):
        with pytest.raises(tbm.PostgresReplayV3PersistenceError):
            postgres_replay_v3.PostgresReplayV3Repository._stored_injection(row)

    manifest_fields = (
        "manifest_sha256",
        "session_id",
        "decision_id",
        "usage_decision_id",
        "injection_artifact_id",
        "completeness",
        "descriptor",
    )
    manifest_row = dict(
        zip(
            manifest_fields,
            postgres_replay_v3.PostgresReplayV3Repository._manifest_values(manifest),
            strict=True,
        )
    )
    for row in (
        {**manifest_row, "descriptor": 1},
        {**manifest_row, "descriptor": "{}"},
        {**manifest_row, "session_id": "different"},
    ):
        with pytest.raises(tbm.PostgresReplayV3PersistenceError):
            postgres_replay_v3.PostgresReplayV3Repository._stored_manifest(row)

    for rows in (
        [{"value": 1}, {"value": 2}],
        [("not", "a", "mapping")],
    ):
        with pytest.raises(
            tbm.PostgresReplayV3PersistenceError,
            match="invalid result",
        ):
            postgres_replay_v3.PostgresReplayV3Repository._select_one(
                _RowsCursor(rows),
                "SELECT 1",
                (),
            )


def test_postgres_replay_repository_defensive_input_and_load_bounds(
    monkeypatch: pytest.MonkeyPatch,
):
    injection, content = _injection()
    repository = tbm.PostgresReplayV3Repository(object())
    with pytest.raises(ValueError, match="exactly"):
        repository._put_artifact(  # type: ignore[arg-type]
            object(),
            object(),  # type: ignore[arg-type]
            content,
        )
    with pytest.raises(ValueError, match="bytes"):
        repository._put_artifact(
            object(),
            injection.artifact,
            "not-bytes",  # type: ignore[arg-type]
        )
    sensitive = create_injection_artifact(
        "secret",
        session_id="session_sensitive",
        decision_id="decision_sensitive",
        usage_decision_id="usage_sensitive",
        memory_revision_ids=("revision_001",),
        renderer_id="renderer_001",
        renderer_version="1.0.0",
        policy_bundle_sha256=DIGEST,
        rendered_at=NOW,
        classification="confidential",
        encryption_key_id="key_001",
    )
    with pytest.raises(ValueError, match="encryption provider"):
        repository._put_artifact(
            object(),
            sensitive.artifact,
            b"secret",
        )
    with pytest.raises(KeyError):
        repository._load_artifact(
            _RowsCursor([]),
            injection.artifact.artifact_id,
        )
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="bounded load",
    ):
        repository._load_artifact(
            _RowsCursor([{"size_bytes": -1, "content_size": -1}]),
            injection.artifact.artifact_id,
        )
    with pytest.raises(KeyError):
        repository._load_injection(
            _RowsCursor([]),
            injection.artifact.artifact_id,
        )
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="bounded load",
    ):
        repository._load_injection(
            _RowsCursor([{"descriptor_size": 0}]),
            injection.artifact.artifact_id,
        )
    oversized = (
        injection.artifact.artifact_id,
        injection.session_id,
        injection.decision_id,
        injection.usage_decision_id,
        "x" * (postgres_replay_v3.REPLAY_JSON_MAX_BYTES + 1),
    )
    monkeypatch.setattr(repository, "_injection_values", lambda _: oversized)
    with pytest.raises(ValueError, match="exceeds storage limit"):
        repository._put_injection(object(), injection)


def test_postgres_replay_repository_defensive_write_conflicts_and_disappears(
    monkeypatch: pytest.MonkeyPatch,
):
    injection, content = _injection()
    manifest = _manifest(injection)
    repository = tbm.PostgresReplayV3Repository(object())
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="artifact disappeared",
    ):
        repository._put_artifact(
            _RowsCursor([], []),
            injection.artifact,
            content,
        )
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="injection disappeared",
    ):
        repository._put_injection(_RowsCursor([], []), injection)
    conflicting_injection = replace(
        injection,
        renderer_version="2.0.0",
    )
    injection_fields = (
        "artifact_id",
        "session_id",
        "decision_id",
        "usage_decision_id",
        "descriptor",
    )
    conflicting_injection_row = dict(
        zip(
            injection_fields,
            repository._injection_values(conflicting_injection),
            strict=True,
        )
    )
    with pytest.raises(
        tbm.PostgresReplayV3ConflictError,
        match="injection has conflicting",
    ):
        repository._put_injection(
            _RowsCursor(
                [{"artifact_id": injection.artifact.artifact_id}],
                [conflicting_injection_row],
            ),
            injection,
        )

    with pytest.raises(
        tbm.PostgresReplayV3ConflictError,
        match="unknown injection",
    ):
        repository._put_manifest(_RowsCursor([]), manifest)
    linked_elsewhere = replace(
        injection,
        session_id="session_elsewhere",
    )
    linked_elsewhere_row = dict(
        zip(
            injection_fields,
            repository._injection_values(linked_elsewhere),
            strict=True,
        )
    )
    with pytest.raises(
        tbm.PostgresReplayV3ConflictError,
        match="linkage conflicts",
    ):
        repository._put_manifest(
            _RowsCursor([linked_elsewhere_row]),
            manifest,
        )
    legacy = _legacy_manifest()
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="manifest disappeared",
    ):
        repository._put_manifest(_RowsCursor([], []), legacy)
    oversized = (
        legacy.manifest_sha256,
        legacy.session_id,
        legacy.decision_id,
        legacy.usage_decision_id,
        legacy.injection_artifact_id,
        legacy.completeness,
        "x" * (postgres_replay_v3.REPLAY_JSON_MAX_BYTES + 1),
    )
    monkeypatch.setattr(repository, "_manifest_values", lambda _: oversized)
    with pytest.raises(ValueError, match="exceeds storage limit"):
        repository._put_manifest(object(), legacy)


def test_postgres_replay_repository_defensive_second_phase_loads():
    injection, content = _injection()
    repository = tbm.PostgresReplayV3Repository(object())
    descriptor_row = dict(
        zip(
            (
                "artifact_id",
                "content_sha256",
                "size_bytes",
                "media_type",
                "classification",
                "created_at",
                "encryption_key_id",
                "redaction_policy_id",
            ),
            repository._artifact_descriptor_values(injection.artifact),
            strict=True,
        )
    )
    descriptor_row["content_size"] = len(content)
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="artifact disappeared",
    ):
        repository._load_artifact(
            _RowsCursor(
                [descriptor_row],
                [],
            ),
            injection.artifact.artifact_id,
        )
    descriptor_size = len(repository._injection_values(injection)[4].encode("utf-8"))
    with pytest.raises(
        tbm.PostgresReplayV3PersistenceError,
        match="injection disappeared",
    ):
        repository._load_injection(
            _RowsCursor(
                [{"descriptor_size": descriptor_size}],
                [],
            ),
            injection.artifact.artifact_id,
        )


def test_postgres_replay_repository_rejects_conflicts_and_bad_bytes(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        assert repository.store_artifact(injection.artifact, content) is True
        with pytest.raises(ValueError, match="content does not match"):
            repository.store_artifact(injection.artifact, b"different")
        conflicting = replace(
            injection.artifact,
            media_type="text/markdown; charset=utf-8",
        )
        with pytest.raises(
            tbm.PostgresReplayV3ConflictError,
            match="conflicting",
        ):
            repository.store_artifact(conflicting, content)


def test_postgres_replay_repository_uses_caller_savepoint(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE caller_work (value integer)")
                cursor.execute("INSERT INTO caller_work VALUES (1)")
            with pytest.raises(ValueError, match="content does not match"):
                repository.store_artifact(injection.artifact, b"different")
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM caller_work")
                assert cursor.fetchone() == (1,)


def test_postgres_replay_repository_fails_closed_on_schema_drift(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        repository.store_artifact(injection.artifact, content)
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE "
                "trace_backed_memory_v3_replay.replay_artifacts "
                "DISABLE TRIGGER replay_artifacts_immutable"
            )
        connection.commit()
        with pytest.raises(tbm.PostgresReplayV3SchemaError):
            repository.load_artifact(injection.artifact.artifact_id)


def test_postgres_replay_repository_rehashes_loaded_bytes(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        repository = tbm.PostgresReplayV3Repository(connection)
        repository.store_artifact(injection.artifact, content)
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE "
                "trace_backed_memory_v3_replay.replay_artifacts "
                "DISABLE TRIGGER replay_artifacts_immutable"
            )
            cursor.execute(
                "UPDATE trace_backed_memory_v3_replay.replay_artifacts "
                "SET content = %s WHERE artifact_id = %s",
                (b"x" * len(content), injection.artifact.artifact_id),
            )
            cursor.execute(
                "ALTER TABLE "
                "trace_backed_memory_v3_replay.replay_artifacts "
                "ENABLE TRIGGER replay_artifacts_immutable"
            )
        connection.commit()
        with pytest.raises(
            tbm.PostgresReplayV3PersistenceError,
            match="bytes do not match",
        ):
            repository.load_artifact(injection.artifact.artifact_id)


def test_postgres_replay_repository_concurrent_bundle_is_idempotent(
    postgres_cluster: PostgresCluster,
):
    psycopg = pytest.importorskip("psycopg")
    _install(postgres_cluster)
    injection, content = _injection()
    manifest = _manifest(injection)

    def store_once() -> tbm.PostgresReplayV3StoreResult:
        with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
            return tbm.PostgresReplayV3Repository(connection).store_bundle(
                injection,
                content,
                manifest,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: store_once(), range(2)))
    assert sum(item.artifact_inserted for item in results) == 1
    assert sum(item.injection_inserted for item in results) == 1
    assert sum(item.manifest_inserted for item in results) == 1
