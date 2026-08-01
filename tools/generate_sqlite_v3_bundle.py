from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "schemas" / "sqlite-v3.components.json"
BUNDLE_PATH = ROOT / "schemas" / "sqlite-v3.sql"
MANIFEST_VERSION = "tbm.sqlite-v3-component-manifest.v1"
BUNDLE_CONTRACT_VERSION = "tbm.sqlite-bundle.v3"
BUNDLE_SCHEMA_VERSION = 1
BUNDLE_METADATA_TABLE = "trace_backed_memory_v3_bundle_schema"
SHA256_PREFIX = "sha256:"
BOOTSTRAP_COMPONENTS = (
    (
        "entity-registry",
        "schemas/sqlite-v3-entity-registry.sql",
        "trace_backed_memory_v3_entity_registry_schema",
        "tbm.entity-registry.v3",
    ),
    (
        "authorization",
        "schemas/sqlite-v3-authorization.sql",
        "trace_backed_memory_v3_authorization_schema",
        "tbm.authorization.v3",
    ),
    (
        "artifact-authority",
        "schemas/sqlite-v3-artifact-authority.sql",
        "trace_backed_memory_v3_artifact_authority_schema",
        None,
    ),
    (
        "memory-revision",
        "schemas/sqlite-v3-memory-revision.sql",
        "trace_backed_memory_v3_memory_revision_schema",
        None,
    ),
    (
        "memory-publication",
        "schemas/sqlite-v3-memory-publication.sql",
        "trace_backed_memory_v3_memory_publication_schema",
        None,
    ),
    (
        "migration",
        "schemas/sqlite-v3-migration.sql",
        "trace_backed_memory_v3_migration_schema",
        None,
    ),
    (
        "managed-index",
        "schemas/sqlite-v3-managed-index.sql",
        "trace_backed_memory_v3_managed_index_schema",
        "tbm.managed-index-bundle.v3",
    ),
    (
        "gate-session",
        "schemas/sqlite-v3-gate-session.sql",
        "trace_backed_memory_v3_gate_session_schema",
        "tbm.gate-session.v3",
    ),
    (
        "gate-evidence",
        "schemas/sqlite-v3-gate-evidence.sql",
        "trace_backed_memory_v3_gate_evidence_schema",
        None,
    ),
    (
        "semantic-gate",
        "schemas/sqlite-v3-semantic-gate.sql",
        "trace_backed_memory_v3_semantic_gate_schema",
        "tbm.semantic-gate-attempt.v3",
    ),
    (
        "semantic-gate-artifacts",
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
        "trace_backed_memory_v3_semantic_gate_artifacts_schema",
        "tbm.semantic-gate-artifact.v3",
    ),
    (
        "replay",
        "schemas/sqlite-v3-replay.sql",
        "trace_backed_memory_v3_replay_schema",
        None,
    ),
    (
        "outcome",
        "schemas/sqlite-v3-outcome.sql",
        "trace_backed_memory_v3_outcome_schema",
        "tbm.run-outcome.v3",
    ),
    (
        "outcome-attribution",
        "schemas/sqlite-v3-outcome-attribution.sql",
        "trace_backed_memory_v3_outcome_attribution_schema",
        "tbm.outcome-attribution.v3",
    ),
    (
        "completion-outbox",
        "schemas/sqlite-v3-completion-outbox.sql",
        "trace_backed_memory_v3_completion_outbox_schema",
        "tbm.completion-outbox.v3",
    ),
    (
        "audit",
        "schemas/sqlite-v3-audit.sql",
        "trace_backed_memory_v3_audit_schema",
        "tbm.audit-event.v3",
    ),
)
_EXACT_MANIFEST_FIELDS = {
    "manifest_version",
    "bundle_contract_version",
    "bundle_resource",
    "schema_version",
    "component_set_sha256",
    "catalog_sha256",
    "bundle_sha256",
    "components",
}
_EXACT_COMPONENT_FIELDS = {
    "component_id",
    "resource",
    "metadata_table",
    "schema_version",
    "contract_version",
    "sha256",
}


class SQLiteV3BundleError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify the unified SQLite v3 schema bundle."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify component bytes, generated bundle, and catalog fingerprint",
    )
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="refresh component digests, bundle, and fingerprints",
    )
    mode.add_argument(
        "--bootstrap",
        action="store_true",
        help="create the first component manifest and generated bundle",
    )
    return parser


def _fail(message: str) -> NoReturn:
    raise SQLiteV3BundleError(message)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _sha256(data: bytes) -> str:
    return SHA256_PREFIX + hashlib.sha256(data).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(SHA256_PREFIX)
        and len(value) == len(SHA256_PREFIX) + 64
        and all(
            character in "0123456789abcdef"
            for character in value[len(SHA256_PREFIX) :]
        )
    )


def _load_manifest() -> dict[str, object]:
    try:
        value = json.loads(
            MANIFEST_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: _fail(
                f"non-finite JSON value: {value}"
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SQLiteV3BundleError(
            "SQLite v3 component manifest could not be read"
        ) from error
    if type(value) is not dict:
        _fail("SQLite v3 component manifest must be an object")
    return value


def _component(
    component_id: str,
    resource: str,
    metadata_table: str,
    contract_version: str | None,
) -> dict[str, object]:
    return {
        "component_id": component_id,
        "resource": resource,
        "metadata_table": metadata_table,
        "schema_version": 1,
        "contract_version": contract_version,
        "sha256": _sha256((ROOT / resource).read_bytes()),
    }


def _bootstrap_manifest() -> dict[str, object]:
    if MANIFEST_PATH.exists() or BUNDLE_PATH.exists():
        _fail("SQLite v3 component manifest or bundle already exists")
    components = [
        _component(component_id, resource, metadata_table, contract)
        for component_id, resource, metadata_table, contract in (
            BOOTSTRAP_COMPONENTS
        )
    ]
    return _complete_manifest(components)


def _validate_components(
    manifest: dict[str, object],
    *,
    verify_digests: bool,
) -> tuple[dict[str, object], ...]:
    if set(manifest) != _EXACT_MANIFEST_FIELDS:
        _fail("SQLite v3 component manifest fields are invalid")
    if manifest["manifest_version"] != MANIFEST_VERSION:
        _fail("SQLite v3 component manifest version is unsupported")
    if manifest["bundle_contract_version"] != BUNDLE_CONTRACT_VERSION:
        _fail("SQLite v3 bundle contract version is unsupported")
    if manifest["bundle_resource"] != "schemas/sqlite-v3.sql":
        _fail("SQLite v3 bundle resource is invalid")
    if manifest["schema_version"] != BUNDLE_SCHEMA_VERSION:
        _fail("SQLite v3 bundle schema version is unsupported")
    raw_components = manifest["components"]
    if type(raw_components) is not list or not raw_components:
        _fail("SQLite v3 components are invalid")

    components: list[dict[str, object]] = []
    component_ids: list[str] = []
    resources: list[str] = []
    metadata_tables: list[str] = []
    for raw in raw_components:
        if type(raw) is not dict or set(raw) != _EXACT_COMPONENT_FIELDS:
            _fail("SQLite v3 component fields are invalid")
        component_id = raw["component_id"]
        resource = raw["resource"]
        metadata_table = raw["metadata_table"]
        schema_version = raw["schema_version"]
        contract_version = raw["contract_version"]
        digest = raw["sha256"]
        if (
            type(component_id) is not str
            or not component_id
            or type(resource) is not str
            or not resource.startswith("schemas/sqlite-v3-")
            or not resource.endswith(".sql")
            or "\\" in resource
            or ".." in Path(resource).parts
            or type(metadata_table) is not str
            or not metadata_table.startswith(
                "trace_backed_memory_v3_"
            )
            or not metadata_table.endswith("_schema")
            or schema_version != 1
            or (
                contract_version is not None
                and (
                    type(contract_version) is not str
                    or not contract_version.startswith("tbm.")
                )
            )
            or not _valid_sha256(digest)
        ):
            _fail(f"SQLite v3 component is invalid: {component_id!r}")
        path = ROOT / resource
        if not path.is_file() or path.is_symlink():
            _fail(f"SQLite v3 component resource is invalid: {resource}")
        if verify_digests and _sha256(path.read_bytes()) != digest:
            _fail(f"SQLite v3 component digest drift: {resource}")
        component_ids.append(component_id)
        resources.append(resource)
        metadata_tables.append(metadata_table)
        components.append(raw)

    if (
        len(component_ids) != len(set(component_ids))
        or len(resources) != len(set(resources))
        or len(metadata_tables) != len(set(metadata_tables))
    ):
        _fail("SQLite v3 component identities must be unique")
    return tuple(components)


def _strip_component_wrapper(source: str, resource: str) -> str:
    lines = source.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and lines[0].strip() in {
        "PRAGMA foreign_keys = ON;",
        "PRAGMA recursive_triggers = ON;",
    }:
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if lines and lines[0].strip() == "BEGIN IMMEDIATE;":
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "COMMIT;":
        lines.pop()
    body = "\n".join(lines).strip()
    if (
        not body
        or "BEGIN IMMEDIATE;" in body
        or "\nCOMMIT;" in "\n" + body
    ):
        _fail(f"SQLite v3 component wrapper is invalid: {resource}")
    return body


def _component_set_sha256(
    components: tuple[dict[str, object], ...],
) -> str:
    canonical = json.dumps(
        list(components),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256(canonical)


def _render_bundle(
    components: tuple[dict[str, object], ...],
    *,
    component_set_sha256: str,
    catalog_sha256: str,
) -> bytes:
    lines = [
        "-- Generated by tools/generate_sqlite_v3_bundle.py; do not edit.",
        f"-- component-manifest: {MANIFEST_VERSION}",
    ]
    for component in components:
        lines.append(
            "-- component: {component_id} {resource} {sha256}".format(
                **component
            )
        )
    lines.extend(
        (
            "",
            "PRAGMA foreign_keys = ON;",
            "PRAGMA recursive_triggers = ON;",
            "",
            "BEGIN IMMEDIATE;",
            "",
            f"CREATE TABLE {BUNDLE_METADATA_TABLE} (",
            "    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),",
            "    schema_version INTEGER NOT NULL CHECK (schema_version = 1),",
            "    contract_version TEXT NOT NULL COLLATE BINARY",
            f"        CHECK (contract_version = '{BUNDLE_CONTRACT_VERSION}'),",
            "    component_set_sha256 TEXT NOT NULL COLLATE BINARY",
            "        CHECK (",
            "            component_set_sha256 GLOB 'sha256:*'",
            "            AND length(component_set_sha256) = 71",
            "            AND substr(component_set_sha256, 8)",
            "                NOT GLOB '*[^0-9a-f]*'",
            "        ),",
            "    catalog_sha256 TEXT NOT NULL COLLATE BINARY",
            "        CHECK (",
            "            catalog_sha256 GLOB 'sha256:*'",
            "            AND length(catalog_sha256) = 71",
            "            AND substr(catalog_sha256, 8)",
            "                NOT GLOB '*[^0-9a-f]*'",
            "        )",
            ");",
            "",
            f"INSERT INTO {BUNDLE_METADATA_TABLE} (",
            "    singleton,",
            "    schema_version,",
            "    contract_version,",
            "    component_set_sha256,",
            "    catalog_sha256",
            ") VALUES (",
            f"    1, 1, '{BUNDLE_CONTRACT_VERSION}',",
            f"    '{component_set_sha256}',",
            f"    '{catalog_sha256}'",
            ");",
            "",
            "CREATE TRIGGER "
            "trace_backed_memory_v3_bundle_schema_immutable_update",
            f"BEFORE UPDATE ON {BUNDLE_METADATA_TABLE}",
            "BEGIN",
            "    SELECT RAISE(ABORT, "
            "'SQLite v3 bundle metadata is immutable');",
            "END;",
            "",
            "CREATE TRIGGER "
            "trace_backed_memory_v3_bundle_schema_immutable_delete",
            f"BEFORE DELETE ON {BUNDLE_METADATA_TABLE}",
            "BEGIN",
            "    SELECT RAISE(ABORT, "
            "'SQLite v3 bundle metadata is immutable');",
            "END;",
        )
    )
    for component in components:
        resource = str(component["resource"])
        try:
            source = (ROOT / resource).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise SQLiteV3BundleError(
                f"SQLite v3 component could not be read: {resource}"
            ) from error
        lines.extend(
            (
                "",
                f"-- BEGIN COMPONENT {component['component_id']}",
                _strip_component_wrapper(source, resource),
                f"-- END COMPONENT {component['component_id']}",
            )
        )
    lines.extend(("", "COMMIT;", ""))
    return "\n".join(lines).encode("utf-8")


def catalog_rows(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str | None], ...]:
    rows: list[tuple[str, str, str, str | None]] = []
    for schema in ("sqlite_master", "sqlite_temp_master"):
        selected = connection.execute(
            "SELECT type, name, tbl_name, sql "
            f"FROM {schema} "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND ("
            "name GLOB 'v3_*' "
            "OR name GLOB 'trace_backed_memory_v3_*' "
            "OR tbl_name GLOB 'v3_*' "
            "OR tbl_name GLOB 'trace_backed_memory_v3_*'"
            ")"
        ).fetchall()
        for object_type, name, table_name, sql in selected:
            normalized = None if sql is None else " ".join(sql.split())
            rows.append((object_type, name, table_name, normalized))
    return tuple(sorted(rows))


def catalog_sha256(connection: sqlite3.Connection) -> str:
    encoded = json.dumps(
        catalog_rows(connection),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _catalog_sha256_for_bundle(bundle: bytes) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(bundle.decode("utf-8"))
        return catalog_sha256(connection)
    except (UnicodeError, sqlite3.Error) as error:
        raise SQLiteV3BundleError(
            "generated SQLite v3 bundle could not be installed"
        ) from error
    finally:
        connection.close()


def _complete_manifest(
    components: list[dict[str, object]],
) -> dict[str, object]:
    component_tuple = tuple(components)
    component_digest = _component_set_sha256(component_tuple)
    placeholder = SHA256_PREFIX + "0" * 64
    provisional = _render_bundle(
        component_tuple,
        component_set_sha256=component_digest,
        catalog_sha256=placeholder,
    )
    catalog_digest = _catalog_sha256_for_bundle(provisional)
    bundle = _render_bundle(
        component_tuple,
        component_set_sha256=component_digest,
        catalog_sha256=catalog_digest,
    )
    if _catalog_sha256_for_bundle(bundle) != catalog_digest:
        _fail("SQLite v3 catalog fingerprint is not deterministic")
    return {
        "manifest_version": MANIFEST_VERSION,
        "bundle_contract_version": BUNDLE_CONTRACT_VERSION,
        "bundle_resource": "schemas/sqlite-v3.sql",
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "component_set_sha256": component_digest,
        "catalog_sha256": catalog_digest,
        "bundle_sha256": _sha256(bundle),
        "components": components,
    }


def _refresh_manifest(
    manifest: dict[str, object],
) -> dict[str, object]:
    components = _validate_components(manifest, verify_digests=False)
    refreshed = [
        {
            **component,
            "sha256": _sha256(
                (ROOT / str(component["resource"])).read_bytes()
            ),
        }
        for component in components
    ]
    return _complete_manifest(refreshed)


def _render_manifest(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _expected_bundle(
    manifest: dict[str, object],
    components: tuple[dict[str, object], ...],
) -> bytes:
    return _render_bundle(
        components,
        component_set_sha256=str(manifest["component_set_sha256"]),
        catalog_sha256=str(manifest["catalog_sha256"]),
    )


def _verify_complete_manifest(
    manifest: dict[str, object],
) -> tuple[dict[str, object], ...]:
    components = _validate_components(manifest, verify_digests=True)
    if not all(
        _valid_sha256(manifest[field])
        for field in (
            "component_set_sha256",
            "catalog_sha256",
            "bundle_sha256",
        )
    ):
        _fail("SQLite v3 bundle fingerprints are invalid")
    if manifest["component_set_sha256"] != _component_set_sha256(components):
        _fail("SQLite v3 component-set fingerprint drift")
    expected_bundle = _expected_bundle(manifest, components)
    try:
        actual_bundle = BUNDLE_PATH.read_bytes()
    except OSError as error:
        raise SQLiteV3BundleError(
            "generated SQLite v3 bundle is missing"
        ) from error
    if actual_bundle != expected_bundle:
        _fail("generated SQLite v3 bundle byte drift")
    if _sha256(actual_bundle) != manifest["bundle_sha256"]:
        _fail("generated SQLite v3 bundle digest drift")
    if _catalog_sha256_for_bundle(actual_bundle) != manifest["catalog_sha256"]:
        _fail("generated SQLite v3 catalog fingerprint drift")
    return components


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.bootstrap:
            manifest = _bootstrap_manifest()
        else:
            manifest = _load_manifest()
            if args.refresh:
                manifest = _refresh_manifest(manifest)
        if args.bootstrap or args.refresh:
            components = _validate_components(
                manifest,
                verify_digests=True,
            )
            _atomic_write(
                BUNDLE_PATH,
                _expected_bundle(manifest, components),
            )
            _atomic_write(MANIFEST_PATH, _render_manifest(manifest))
        components = _verify_complete_manifest(manifest)
    except SQLiteV3BundleError as error:
        print(f"SQLite v3 bundle verification failed: {error}")
        return 1
    print(
        "SQLite v3 bundle verified: "
        f"{len(components)} components, "
        f"{manifest['catalog_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
