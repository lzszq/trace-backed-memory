from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_bundle_v3 import (
    SQLITE_V3_BUNDLE_CONTRACT_VERSION,
    SQLITE_V3_BUNDLE_METADATA_TABLE,
    SQLiteV3BundleError,
    install_sqlite_v3_bundle,
    load_sqlite_v3_bundle_manifest,
    sqlite_v3_catalog_sha256,
    verify_sqlite_v3_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


def _connection(
    database: str | Path = ":memory:",
) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    return connection


def test_sqlite_v3_component_manifest_and_bundle_are_generated() -> None:
    manifest = load_sqlite_v3_bundle_manifest()

    assert manifest.bundle_contract_version == (
        SQLITE_V3_BUNDLE_CONTRACT_VERSION
    )
    assert len(manifest.components) == 15
    assert [item.component_id for item in manifest.components] == [
        "entity-registry",
        "authorization",
        "artifact-authority",
        "memory-revision",
        "memory-publication",
        "managed-index",
        "gate-session",
        "gate-evidence",
        "semantic-gate",
        "semantic-gate-artifacts",
        "replay",
        "outcome",
        "outcome-attribution",
        "completion-outbox",
        "audit",
    ]
    assert "schemas/sqlite-v3-migration.sql" not in {
        item.resource for item in manifest.components
    }

    completed = subprocess.run(
        [
            sys.executable,
            "tools/generate_sqlite_v3_bundle.py",
            "--check",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "15 components" in completed.stdout


def test_sqlite_v3_bundle_installs_the_exact_complete_catalog() -> None:
    connection = _connection()
    try:
        manifest = install_sqlite_v3_bundle(connection)

        assert connection.in_transaction is False
        assert sqlite_v3_catalog_sha256(connection) == (
            manifest.catalog_sha256
        )
        assert verify_sqlite_v3_bundle(connection) == manifest
        metadata = connection.execute(
            f"SELECT schema_version, contract_version, "
            "component_set_sha256, catalog_sha256 "
            f"FROM {SQLITE_V3_BUNDLE_METADATA_TABLE}"
        ).fetchone()
        assert metadata == (
            1,
            SQLITE_V3_BUNDLE_CONTRACT_VERSION,
            manifest.component_set_sha256,
            manifest.catalog_sha256,
        )
    finally:
        connection.close()


def test_sqlite_v3_bundle_installs_side_by_side_with_legacy_sqlite_v1(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-upgrade.sqlite3"
    connection = _connection(database)
    try:
        connection.executescript(
            read_packaged_resource("schemas/sqlite.sql").decode("utf-8")
        )
        legacy_tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'traces'"
        ).fetchone()

        manifest = install_sqlite_v3_bundle(connection)

        assert legacy_tables == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'traces'"
        ).fetchone() == (1,)
        assert verify_sqlite_v3_bundle(connection) == manifest
    finally:
        connection.close()


def test_sqlite_v3_bundle_rejects_partial_and_drifted_catalogs() -> None:
    partial = _connection()
    try:
        partial.executescript(
            read_packaged_resource(
                "schemas/sqlite-v3-authorization.sql"
            ).decode("utf-8")
        )
        with pytest.raises(SQLiteV3BundleError) as raised:
            verify_sqlite_v3_bundle(partial)
        assert raised.value.code == "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID"
    finally:
        partial.close()

    drifted = _connection()
    try:
        install_sqlite_v3_bundle(drifted)
        drifted.execute(
            "DROP TRIGGER v3_authorization_decisions_immutable_delete"
        )
        with pytest.raises(SQLiteV3BundleError) as raised:
            verify_sqlite_v3_bundle(drifted)
        assert raised.value.code == "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID"
    finally:
        drifted.close()


def test_sqlite_v3_bundle_rejects_extra_controlled_temp_objects() -> None:
    connection = _connection()
    try:
        install_sqlite_v3_bundle(connection)
        connection.execute(
            "CREATE TEMP TRIGGER v3_unexpected_temp_trigger "
            "AFTER INSERT ON v3_authorization_policies "
            "BEGIN SELECT 1; END"
        )

        with pytest.raises(SQLiteV3BundleError) as raised:
            verify_sqlite_v3_bundle(connection)

        assert raised.value.code == "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID"
    finally:
        connection.close()


def test_sqlite_v3_bundle_metadata_is_immutable() -> None:
    connection = _connection()
    try:
        install_sqlite_v3_bundle(connection)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {SQLITE_V3_BUNDLE_METADATA_TABLE} "
                "SET catalog_sha256 = ?",
                ("sha256:" + "0" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"DELETE FROM {SQLITE_V3_BUNDLE_METADATA_TABLE}"
            )
    finally:
        connection.close()


def test_sqlite_v3_bundle_install_rolls_back_on_component_collision() -> None:
    connection = _connection()
    try:
        connection.execute(
            "CREATE TABLE v3_entity_registry_snapshots (sentinel INTEGER)"
        )
        connection.commit()

        with pytest.raises(SQLiteV3BundleError) as raised:
            install_sqlite_v3_bundle(connection)

        assert raised.value.code == "TBM_SQLITE_V3_BUNDLE_INSTALL_FAILED"
        assert connection.in_transaction is False
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            f"WHERE name = '{SQLITE_V3_BUNDLE_METADATA_TABLE}'"
        ).fetchone() == (0,)
        assert connection.execute(
            "PRAGMA table_info(v3_entity_registry_snapshots)"
        ).fetchall()[0][1] == "sentinel"
    finally:
        connection.close()
