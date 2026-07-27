from __future__ import annotations

from pathlib import Path

from tests.postgres_support import PostgresCluster, assert_sql_succeeds


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-replay.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-replay-rollback.sql"
ARTIFACT_ID = "artifact_sha256_" + "a" * 64
DIGEST = "sha256:" + "a" * 64
ARTIFACT_ID_B = "artifact_sha256_" + "b" * 64
DIGEST_B = "sha256:" + "b" * 64


def _install(cluster: PostgresCluster) -> None:
    cluster.load_schema()
    installed = cluster.run_script(INSTALL)
    assert installed.returncode == 0, installed.stderr


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
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version, contract_version "
        "FROM trace_backed_memory_v3_replay.schema_metadata",
    ) == "1|tbm.replay.v3"
    assert assert_sql_succeeds(
        postgres_cluster,
        _insert_bundle_sql()
        + " SELECT pg_catalog.encode(content, 'hex') "
        "FROM trace_backed_memory_v3_replay.replay_artifacts;",
    ) == "6869"

    for statement in (
        "UPDATE trace_backed_memory_v3_replay.replay_artifacts "
        "SET media_type = 'application/octet-stream'",
        "DELETE FROM trace_backed_memory_v3_replay.replay_injections",
        "TRUNCATE trace_backed_memory_v3_replay.replay_manifests",
        "UPDATE trace_backed_memory_v3_replay.schema_metadata "
        "SET schema_version = 2",
    ):
        rejected = postgres_cluster.run(statement)
        assert rejected.returncode != 0
        assert "immutable" in rejected.stderr

    rolled_back = postgres_cluster.run_script(ROLLBACK)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT pg_catalog.to_regnamespace("
        "'trace_backed_memory_v3_replay') IS NULL",
    ) == "t"
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT schema_version FROM public.trace_backed_memory_schema",
    ) == "2"


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
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT count(*) FROM "
        "trace_backed_memory_v3_replay.replay_injections",
    ) == "0"

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
        "CREATE TABLE trace_backed_memory_v3_replay.unexpected "
        "(value integer)",
    )
    rejected = postgres_cluster.run_script(ROLLBACK)
    assert rejected.returncode != 0
    assert "catalog mismatch" in rejected.stderr
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT pg_catalog.to_regclass("
        "'trace_backed_memory_v3_replay.unexpected') IS NOT NULL",
    ) == "t"


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
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT pg_catalog.to_regnamespace("
        "'trace_backed_memory_v3_replay') IS NULL",
    ) == "t"

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
    assert assert_sql_succeeds(
        postgres_cluster,
        "SELECT pg_catalog.to_regnamespace("
        "'trace_backed_memory_v3_replay') IS NOT NULL",
    ) == "t"
