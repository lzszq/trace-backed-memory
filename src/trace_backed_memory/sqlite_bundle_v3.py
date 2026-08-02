from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, NoReturn

from .resources import PackagedResourceError, read_packaged_resource


SQLITE_V3_COMPONENT_MANIFEST_VERSION = (
    "tbm.sqlite-v3-component-manifest.v1"
)
SQLITE_V3_BUNDLE_CONTRACT_VERSION = "tbm.sqlite-bundle.v3"
SQLITE_V3_BUNDLE_SCHEMA_VERSION = 1
SQLITE_V3_COMPONENT_MANIFEST_RESOURCE = (
    "schemas/sqlite-v3.components.json"
)
SQLITE_V3_BUNDLE_RESOURCE = "schemas/sqlite-v3.sql"
SQLITE_V3_GATE_SESSION_TIMESTAMP_HOTFIX_RESOURCE = (
    "schemas/sqlite-v3-gate-session-timestamp-hotfix.sql"
)
SQLITE_V3_BUNDLE_METADATA_TABLE = (
    "trace_backed_memory_v3_bundle_schema"
)
_SHA256_PREFIX = "sha256:"
_LEGACY_GATE_SESSION_COMPONENT_SET_SHA256 = (
    "sha256:3b845f08d52c83705b55cb369758db23a344d9324a006d6541cae554bf921381"
)
_LEGACY_GATE_SESSION_VALIDATION_TRIGGER_SHA256 = (
    "sha256:b687332cce0e8e90b8cff81aaa95340d7c3ac3c0dc5258cb020c9862683e5160"
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
    """Stable failure while loading, installing, or verifying SQLite v3."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SQLiteV3Component:
    component_id: str
    resource: str
    metadata_table: str
    schema_version: int
    contract_version: str | None
    sha256: str


@dataclass(frozen=True)
class SQLiteV3BundleManifest:
    manifest_version: str
    bundle_contract_version: str
    bundle_resource: str
    schema_version: int
    component_set_sha256: str
    catalog_sha256: str
    bundle_sha256: str
    components: tuple[SQLiteV3Component, ...]


def _failed(code: str, message: str) -> NoReturn:
    raise SQLiteV3BundleError(code, message)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _failed(
                "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
                "SQLite v3 component manifest contains duplicate keys",
            )
        result[key] = value
    return result


def _sha256(data: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(data).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_SHA256_PREFIX)
        and len(value) == len(_SHA256_PREFIX) + 64
        and all(
            character in "0123456789abcdef"
            for character in value[len(_SHA256_PREFIX) :]
        )
    )


def _component_set_sha256(
    raw_components: list[dict[str, object]],
) -> str:
    canonical = json.dumps(
        raw_components,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256(canonical)


def load_sqlite_v3_bundle_manifest() -> SQLiteV3BundleManifest:
    """Load and authenticate the installed component and bundle bytes."""

    try:
        manifest_bytes = read_packaged_resource(
            SQLITE_V3_COMPONENT_MANIFEST_RESOURCE
        )
        value = json.loads(
            manifest_bytes,
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda _value: _failed(
                "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
                "SQLite v3 component manifest contains a non-finite value",
            ),
        )
    except SQLiteV3BundleError:
        raise
    except (
        PackagedResourceError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
            "SQLite v3 component manifest is invalid",
        ) from error
    if type(value) is not dict or set(value) != _EXACT_MANIFEST_FIELDS:
        _failed(
            "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
            "SQLite v3 component manifest fields are invalid",
        )
    if (
        value["manifest_version"]
        != SQLITE_V3_COMPONENT_MANIFEST_VERSION
        or value["bundle_contract_version"]
        != SQLITE_V3_BUNDLE_CONTRACT_VERSION
        or value["bundle_resource"] != SQLITE_V3_BUNDLE_RESOURCE
        or value["schema_version"] != SQLITE_V3_BUNDLE_SCHEMA_VERSION
        or not _valid_sha256(value["component_set_sha256"])
        or not _valid_sha256(value["catalog_sha256"])
        or not _valid_sha256(value["bundle_sha256"])
    ):
        _failed(
            "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
            "SQLite v3 component manifest header is invalid",
        )

    raw_components = value["components"]
    if type(raw_components) is not list or not raw_components:
        _failed(
            "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
            "SQLite v3 component manifest has no components",
        )
    components: list[SQLiteV3Component] = []
    component_ids: list[str] = []
    resources: list[str] = []
    metadata_tables: list[str] = []
    for raw in raw_components:
        if type(raw) is not dict or set(raw) != _EXACT_COMPONENT_FIELDS:
            _failed(
                "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
                "SQLite v3 component fields are invalid",
            )
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
            _failed(
                "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
                "SQLite v3 component entry is invalid",
            )
        try:
            component_bytes = read_packaged_resource(resource)
        except PackagedResourceError as error:
            raise SQLiteV3BundleError(
                "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
                "SQLite v3 component resource is unavailable",
            ) from error
        if _sha256(component_bytes) != digest:
            _failed(
                "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
                "SQLite v3 component resource digest is invalid",
            )
        component_ids.append(component_id)
        resources.append(resource)
        metadata_tables.append(metadata_table)
        components.append(
            SQLiteV3Component(
                component_id=component_id,
                resource=resource,
                metadata_table=metadata_table,
                schema_version=schema_version,
                contract_version=contract_version,
                sha256=digest,
            )
        )
    if (
        len(component_ids) != len(set(component_ids))
        or len(resources) != len(set(resources))
        or len(metadata_tables) != len(set(metadata_tables))
    ):
        _failed(
            "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
            "SQLite v3 component identities are not unique",
        )
    if value["component_set_sha256"] != _component_set_sha256(
        raw_components
    ):
        _failed(
            "TBM_SQLITE_V3_BUNDLE_MANIFEST_INVALID",
            "SQLite v3 component-set fingerprint is invalid",
        )
    try:
        bundle_bytes = read_packaged_resource(SQLITE_V3_BUNDLE_RESOURCE)
    except PackagedResourceError as error:
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite v3 bundle resource is unavailable",
        ) from error
    if _sha256(bundle_bytes) != value["bundle_sha256"]:
        _failed(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite v3 bundle resource digest is invalid",
        )
    return SQLiteV3BundleManifest(
        manifest_version=SQLITE_V3_COMPONENT_MANIFEST_VERSION,
        bundle_contract_version=SQLITE_V3_BUNDLE_CONTRACT_VERSION,
        bundle_resource=SQLITE_V3_BUNDLE_RESOURCE,
        schema_version=SQLITE_V3_BUNDLE_SCHEMA_VERSION,
        component_set_sha256=value["component_set_sha256"],
        catalog_sha256=value["catalog_sha256"],
        bundle_sha256=value["bundle_sha256"],
        components=tuple(components),
    )


def sqlite_v3_catalog_rows(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str | None], ...]:
    """Return normalized controlled objects from main and temporary catalogs."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
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


def sqlite_v3_catalog_sha256(connection: sqlite3.Connection) -> str:
    encoded = json.dumps(
        sqlite_v3_catalog_rows(connection),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _gate_session_validation_trigger_sha256(
    connection: sqlite3.Connection,
) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'gate_session_revisions_validate_insert'"
    ).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not str:
        _failed(
            "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_PRECONDITION",
            "SQLite GateSession validation trigger is unavailable",
        )
    return _sha256(" ".join(row[0].split()).encode("utf-8"))


def _gate_session_schema_definitions(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str, str], ...]:
    definitions: list[tuple[str, str, str, str, str]] = []
    for catalog in ("sqlite_master", "sqlite_temp_master"):
        rows = connection.execute(
            f"SELECT type, name, tbl_name, sql FROM {catalog} "
            "WHERE sql IS NOT NULL AND (tbl_name IN ("
            "'trace_backed_memory_v3_gate_session_schema', "
            "'gate_session_heads', 'gate_session_revisions') "
            "OR name = 'trace_backed_memory_v3_gate_session_schema') "
            "ORDER BY name"
        ).fetchall()
        for row in rows:
            if (
                len(row) != 4
                or any(type(value) is not str for value in row)
            ):
                _failed(
                    "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_PRECONDITION",
                    "SQLite GateSession schema definition is invalid",
                )
            definitions.append(
                (
                    catalog,
                    row[0],
                    row[1],
                    row[2],
                    " ".join(row[3].split()),
                )
            )
    return tuple(definitions)


def _canonical_gate_session_schema_definitions() -> tuple[
    tuple[str, str, str, str, str], ...
]:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                read_packaged_resource(
                    "schemas/sqlite-v3-gate-session.sql"
                ).decode("utf-8")
            )
            return _gate_session_schema_definitions(connection)
        finally:
            connection.close()
    except SQLiteV3BundleError:
        raise
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite GateSession schema resource is invalid",
        ) from error


def _legacy_gate_session_schema_matches(
    connection: sqlite3.Connection,
) -> bool:
    actual = {
        row[2]: row for row in _gate_session_schema_definitions(connection)
    }
    canonical = {
        row[2]: row for row in _canonical_gate_session_schema_definitions()
    }
    trigger_name = "gate_session_revisions_validate_insert"
    if set(actual) != set(canonical) or trigger_name not in actual:
        return False
    trigger = actual.pop(trigger_name)
    canonical.pop(trigger_name)
    return (
        actual == canonical
        and _sha256(trigger[4].encode("utf-8"))
        == _LEGACY_GATE_SESSION_VALIDATION_TRIGGER_SHA256
    )


def _gate_session_timestamp_hotfix_statements() -> tuple[str, ...]:
    try:
        script = read_packaged_resource(
            SQLITE_V3_GATE_SESSION_TIMESTAMP_HOTFIX_RESOURCE
        ).decode("utf-8")
    except (OSError, UnicodeError, PackagedResourceError) as error:
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite GateSession hotfix resource is invalid",
        ) from error
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        _failed(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite GateSession hotfix resource is incomplete",
        )
    normalized = tuple(" ".join(statement.split()) for statement in statements)
    if (
        len(statements) < 5
        or normalized[:3]
        != (
            "PRAGMA foreign_keys = ON;",
            "PRAGMA recursive_triggers = ON;",
            "BEGIN IMMEDIATE;",
        )
        or normalized[-1] != "COMMIT;"
    ):
        _failed(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite GateSession hotfix transaction boundary is invalid",
        )
    return tuple(statements[3:-1])


def _canonical_gate_session_validation_trigger_sha256() -> str:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                read_packaged_resource(
                    "schemas/sqlite-v3-gate-session.sql"
                ).decode("utf-8")
            )
            return _gate_session_validation_trigger_sha256(connection)
        finally:
            connection.close()
    except SQLiteV3BundleError:
        raise
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_RESOURCE_DRIFT",
            "SQLite GateSession schema resource is invalid",
        ) from error


def verify_sqlite_v3_bundle(
    connection: sqlite3.Connection,
) -> SQLiteV3BundleManifest:
    """Fail closed unless the complete installed v3 graph is byte-canonical."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    manifest = load_sqlite_v3_bundle_manifest()
    try:
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            _failed(
                "TBM_SQLITE_V3_BUNDLE_PRAGMA_INVALID",
                "SQLite v3 requires foreign key enforcement",
            )
        if connection.execute("PRAGMA recursive_triggers").fetchone() != (
            1,
        ):
            _failed(
                "TBM_SQLITE_V3_BUNDLE_PRAGMA_INVALID",
                "SQLite v3 requires recursive triggers",
            )
        bundle_row = connection.execute(
            f"SELECT singleton, schema_version, contract_version, "
            "component_set_sha256, catalog_sha256 "
            f"FROM {SQLITE_V3_BUNDLE_METADATA_TABLE}"
        ).fetchall()
        expected_bundle_row = [
            (
                1,
                manifest.schema_version,
                manifest.bundle_contract_version,
                manifest.component_set_sha256,
                manifest.catalog_sha256,
            )
        ]
        if bundle_row != expected_bundle_row:
            _failed(
                "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID",
                "SQLite v3 bundle metadata is invalid",
            )
        for component in manifest.components:
            columns = (
                "singleton, schema_version, contract_version"
                if component.contract_version is not None
                else "singleton, schema_version"
            )
            expected = (
                (
                    1,
                    component.schema_version,
                    component.contract_version,
                )
                if component.contract_version is not None
                else (1, component.schema_version)
            )
            rows = connection.execute(
                f"SELECT {columns} FROM {component.metadata_table}"
            ).fetchall()
            if rows != [expected]:
                _failed(
                    "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID",
                    "SQLite v3 component metadata is invalid",
                )
        if sqlite_v3_catalog_sha256(connection) != manifest.catalog_sha256:
            _failed(
                "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID",
                "SQLite v3 catalog fingerprint is invalid",
            )
    except SQLiteV3BundleError:
        raise
    except sqlite3.Error as error:
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_SCHEMA_INVALID",
            "SQLite v3 schema is invalid",
        ) from error
    return manifest


def apply_sqlite_v3_gate_session_timestamp_hotfix(
    connection: sqlite3.Connection,
) -> SQLiteV3BundleManifest:
    """Atomically repair the legacy variable-precision timestamp trigger."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if connection.in_transaction:
        _failed(
            "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_TRANSACTION_ACTIVE",
            "SQLite GateSession hotfix requires an idle connection",
        )
    manifest = load_sqlite_v3_bundle_manifest()
    canonical_definitions = _canonical_gate_session_schema_definitions()
    canonical_trigger_sha256 = (
        _canonical_gate_session_validation_trigger_sha256()
    )
    hotfix_statements = _gate_session_timestamp_hotfix_statements()
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("BEGIN IMMEDIATE")
        metadata = connection.execute(
            f"SELECT schema_version, contract_version, "
            "component_set_sha256, catalog_sha256 "
            f"FROM {SQLITE_V3_BUNDLE_METADATA_TABLE} "
            "WHERE singleton = 1"
        ).fetchall()
        current = (
            manifest.schema_version,
            manifest.bundle_contract_version,
            manifest.component_set_sha256,
            manifest.catalog_sha256,
        )
        legacy = (
            manifest.schema_version,
            manifest.bundle_contract_version,
            _LEGACY_GATE_SESSION_COMPONENT_SET_SHA256,
            manifest.catalog_sha256,
        )
        trigger_sha256 = _gate_session_validation_trigger_sha256(connection)
        if metadata == [current]:
            if (
                trigger_sha256 != canonical_trigger_sha256
                or _gate_session_schema_definitions(connection)
                != canonical_definitions
            ):
                _failed(
                    "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_PRECONDITION",
                    "SQLite GateSession trigger does not match the current bundle",
                )
            verified = verify_sqlite_v3_bundle(connection)
            connection.commit()
            return verified
        if (
            metadata != [legacy]
            or sqlite_v3_catalog_sha256(connection)
            != manifest.catalog_sha256
            or trigger_sha256
            != _LEGACY_GATE_SESSION_VALIDATION_TRIGGER_SHA256
            or not _legacy_gate_session_schema_matches(connection)
        ):
            _failed(
                "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_PRECONDITION",
                "SQLite v3 bundle is not the exact supported hotfix source",
            )
        for statement in hotfix_statements:
            connection.execute(statement)
        if (
            _gate_session_validation_trigger_sha256(connection)
            != canonical_trigger_sha256
            or _gate_session_schema_definitions(connection)
            != canonical_definitions
        ):
            _failed(
                "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_FAILED",
                "SQLite GateSession timestamp hotfix was not retained",
            )
        verified = verify_sqlite_v3_bundle(connection)
        connection.commit()
        return verified
    except SQLiteV3BundleError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        if connection.in_transaction:
            connection.rollback()
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_GATE_SESSION_HOTFIX_FAILED",
            "SQLite GateSession timestamp hotfix failed",
        ) from error


def install_sqlite_v3_bundle(
    connection: sqlite3.Connection,
) -> SQLiteV3BundleManifest:
    """Atomically install and verify the complete v3 graph on one connection."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if connection.in_transaction:
        _failed(
            "TBM_SQLITE_V3_BUNDLE_TRANSACTION_ACTIVE",
            "SQLite v3 installation requires an idle connection",
        )
    manifest = load_sqlite_v3_bundle_manifest()
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        existing = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            f"AND name = '{SQLITE_V3_BUNDLE_METADATA_TABLE}'"
        ).fetchone()
        if existing == (1,):
            return apply_sqlite_v3_gate_session_timestamp_hotfix(connection)
        bundle = read_packaged_resource(manifest.bundle_resource).decode(
            "utf-8"
        )
        connection.executescript(bundle)
    except (
        OSError,
        UnicodeError,
        sqlite3.Error,
        PackagedResourceError,
    ) as error:
        if connection.in_transaction:
            connection.rollback()
        raise SQLiteV3BundleError(
            "TBM_SQLITE_V3_BUNDLE_INSTALL_FAILED",
            "SQLite v3 bundle installation failed",
        ) from error
    return verify_sqlite_v3_bundle(connection)


__all__ = [
    "SQLITE_V3_BUNDLE_CONTRACT_VERSION",
    "SQLITE_V3_BUNDLE_METADATA_TABLE",
    "SQLITE_V3_BUNDLE_RESOURCE",
    "SQLITE_V3_BUNDLE_SCHEMA_VERSION",
    "SQLITE_V3_COMPONENT_MANIFEST_RESOURCE",
    "SQLITE_V3_GATE_SESSION_TIMESTAMP_HOTFIX_RESOURCE",
    "SQLITE_V3_COMPONENT_MANIFEST_VERSION",
    "SQLiteV3BundleError",
    "SQLiteV3BundleManifest",
    "SQLiteV3Component",
    "apply_sqlite_v3_gate_session_timestamp_hotfix",
    "install_sqlite_v3_bundle",
    "load_sqlite_v3_bundle_manifest",
    "sqlite_v3_catalog_rows",
    "sqlite_v3_catalog_sha256",
    "verify_sqlite_v3_bundle",
]
