from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import NoReturn


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = Path("docs/status/authority-registry.json")
AUTHORITY_REGISTRY_VERSION = "tbm.authority-registry.v1"
AUTHORITY_REGISTRY_MAX_BYTES = 512 * 1024
AUTHORITY_REGISTRY_MAX_ENTRIES = 128
AUTHORITY_REGISTRY_MAX_SCHEMAS_PER_ENTRY = 16

_ENTRY_FIELDS = frozenset(
    {
        "authority_id",
        "event_projection_impact",
        "module",
        "role",
        "schemas",
        "source_of_truth",
    }
)
_ROLES = frozenset(
    {
        "ledger",
        "projection",
        "compatibility-migration",
        "bundle-coordinator",
    }
)
_SOURCES_OF_TRUTH = frozenset(
    {"migration-asset", "artifact-authority", "none"}
)
_IMPACTS = frozenset(
    {
        "migration-asset-only",
        "artifact-authority",
        "replaceable-projection",
        "compatibility-migration",
        "bundle-coordination",
    }
)
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")
_PERSISTENCE_CANDIDATE_RE = re.compile(r"^(?:sqlite|postgres).*_v3\.py$")
_PERSISTENCE_MODULE_RE = re.compile(
    r"^(?:sqlite|postgres)(?:_[a-z0-9]+)*_v3\.py$"
)


class AuthorityRegistryError(RuntimeError):
    """Stable repository-governance failure for persistence authority drift."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AuthorityRegistration:
    authority_id: str
    event_projection_impact: str
    module: str
    role: str
    schemas: tuple[str, ...]
    source_of_truth: str


def discover_persistence_modules(root: Path = ROOT) -> tuple[str, ...]:
    source = root / "src" / "trace_backed_memory"
    if not source.is_dir() or source.is_symlink():
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVENTORY_INVALID",
            "persistence module directory is missing or unsafe",
        )
    discovered: list[str] = []
    for path in source.iterdir():
        if _PERSISTENCE_CANDIDATE_RE.fullmatch(path.name) is None:
            continue
        if _PERSISTENCE_MODULE_RE.fullmatch(path.name) is None:
            _fail(
                "TBM_AUTHORITY_REGISTRY_INVENTORY_INVALID",
                "persistence module name is not canonical",
            )
        if path.is_symlink() or not path.is_file():
            _fail(
                "TBM_AUTHORITY_REGISTRY_INVENTORY_INVALID",
                "persistence module candidate is not a regular file",
            )
        discovered.append(path.relative_to(root).as_posix())
    modules = tuple(sorted(discovered))
    if not modules:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVENTORY_INVALID",
            "no persistence modules were discovered",
        )
    return modules


def load_authority_registry(
    root: Path = ROOT,
    registry_path: Path | None = None,
) -> tuple[AuthorityRegistration, ...]:
    relative = DEFAULT_REGISTRY_PATH if registry_path is None else registry_path
    path = relative if relative.is_absolute() else root / relative
    _require_regular_file(root, path, label="authority registry")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AuthorityRegistryError(
            "TBM_AUTHORITY_REGISTRY_READ_FAILED",
            "authority registry could not be read",
        ) from error
    if not 1 <= len(data) <= AUTHORITY_REGISTRY_MAX_BYTES:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority registry byte size is out of bounds",
        )
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except AuthorityRegistryError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuthorityRegistryError(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority registry is not strict UTF-8 JSON",
        ) from error
    if type(document) is not dict or set(document) != {
        "registry_version",
        "entries",
    }:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority registry root fields are invalid",
        )
    if document["registry_version"] != AUTHORITY_REGISTRY_VERSION:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority registry version is unsupported",
        )
    raw_entries = document["entries"]
    if (
        type(raw_entries) is not list
        or not 1 <= len(raw_entries) <= AUTHORITY_REGISTRY_MAX_ENTRIES
    ):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority registry entries are invalid or unbounded",
        )
    entries = tuple(_parse_entry(root, item) for item in raw_entries)
    authority_ids = tuple(entry.authority_id for entry in entries)
    modules = tuple(entry.module for entry in entries)
    if authority_ids != tuple(sorted(authority_ids)):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority entries must be sorted by authority_id",
        )
    if len(authority_ids) != len(set(authority_ids)):
        _fail(
            "TBM_AUTHORITY_REGISTRY_DUPLICATE",
            "authority_id values must be unique",
        )
    if len(modules) != len(set(modules)):
        _fail(
            "TBM_AUTHORITY_REGISTRY_DUPLICATE",
            "persistence modules must be registered exactly once",
        )
    return entries


def verify_authority_registry(
    root: Path = ROOT,
    registry_path: Path | None = None,
) -> tuple[AuthorityRegistration, ...]:
    entries = load_authority_registry(root, registry_path)
    declared = {entry.module for entry in entries}
    discovered = set(discover_persistence_modules(root))
    unregistered = sorted(discovered - declared)
    missing = sorted(declared - discovered)
    if unregistered:
        _fail(
            "TBM_AUTHORITY_REGISTRY_UNREGISTERED_AUTHORITY",
            "unregistered persistence authority modules: "
            + ", ".join(unregistered),
        )
    if missing:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVENTORY_DRIFT",
            "registered persistence authority modules are missing: "
            + ", ".join(missing),
        )
    return entries


def _parse_entry(root: Path, value: object) -> AuthorityRegistration:
    if type(value) is not dict or set(value) != _ENTRY_FIELDS:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority entry fields are invalid",
        )
    authority_id = value["authority_id"]
    if (
        type(authority_id) is not str
        or _IDENTIFIER_RE.fullmatch(authority_id) is None
    ):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority_id is not a bounded canonical identifier",
        )
    module = _relative_path(
        value["module"],
        prefix="src/trace_backed_memory/",
        suffixes=(".py",),
        label="module",
    )
    module_name = PurePosixPath(module).name
    if _PERSISTENCE_MODULE_RE.fullmatch(module_name) is None:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "registered module is not a persistence-v3 module",
        )
    _require_regular_file(root, root / module, label="registered module")
    storage_prefix = module_name.split("_", 1)[0]
    if not authority_id.startswith(storage_prefix + "."):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority_id storage prefix does not match its module",
        )
    role = value["role"]
    source_of_truth = value["source_of_truth"]
    impact = value["event_projection_impact"]
    if type(role) is not str or role not in _ROLES:
        _fail(
            "TBM_AUTHORITY_REGISTRY_ROLE_INVALID",
            "authority role is not registered",
        )
    if (
        type(source_of_truth) is not str
        or source_of_truth not in _SOURCES_OF_TRUTH
    ):
        _fail(
            "TBM_AUTHORITY_REGISTRY_SOURCE_INVALID",
            "authority source_of_truth is not registered",
        )
    if type(impact) is not str or impact not in _IMPACTS:
        _fail(
            "TBM_AUTHORITY_REGISTRY_IMPACT_INVALID",
            "event/projection impact is not registered",
        )
    _verify_role_boundary(module_name, role, source_of_truth, impact)
    raw_schemas = value["schemas"]
    if (
        type(raw_schemas) is not list
        or not 1
        <= len(raw_schemas)
        <= AUTHORITY_REGISTRY_MAX_SCHEMAS_PER_ENTRY
    ):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority schemas are invalid or unbounded",
        )
    schemas = tuple(
        _relative_path(
            item,
            prefix="schemas/",
            suffixes=(".sql", ".json"),
            label="schema",
        )
        for item in raw_schemas
    )
    if schemas != tuple(sorted(schemas)) or len(schemas) != len(set(schemas)):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            "authority schemas must be sorted and unique",
        )
    for schema in schemas:
        _require_regular_file(root, root / schema, label="authority schema")
    return AuthorityRegistration(
        authority_id=authority_id,
        event_projection_impact=impact,
        module=module,
        role=role,
        schemas=schemas,
        source_of_truth=source_of_truth,
    )


def _verify_role_boundary(
    module_name: str,
    role: str,
    source_of_truth: str,
    impact: str,
) -> None:
    expected = {
        "ledger": {
            ("migration-asset", "migration-asset-only"),
            ("artifact-authority", "artifact-authority"),
        },
        "projection": {("none", "replaceable-projection")},
        "compatibility-migration": {
            ("migration-asset", "compatibility-migration")
        },
        "bundle-coordinator": {("none", "bundle-coordination")},
    }
    if (source_of_truth, impact) not in expected[role]:
        _fail(
            "TBM_AUTHORITY_REGISTRY_BOUNDARY_INVALID",
            "authority role, source_of_truth, and impact disagree",
        )
    if source_of_truth == "artifact-authority" and module_name not in {
        "sqlite_artifact_v3.py",
        "postgres_artifact_v3.py",
    }:
        _fail(
            "TBM_AUTHORITY_REGISTRY_SOURCE_INVALID",
            "only the protected Artifact Authority may own artifact bytes",
        )
    if role == "projection" and not module_name.endswith("managed_index_v3.py"):
        _fail(
            "TBM_AUTHORITY_REGISTRY_BOUNDARY_INVALID",
            "only managed-index v3 modules are current projections",
        )
    if role == "compatibility-migration" and module_name != "sqlite_v3.py":
        _fail(
            "TBM_AUTHORITY_REGISTRY_BOUNDARY_INVALID",
            "only sqlite_v3.py is the compatibility migration repository",
        )
    if role == "bundle-coordinator" and module_name != "sqlite_bundle_v3.py":
        _fail(
            "TBM_AUTHORITY_REGISTRY_BOUNDARY_INVALID",
            "only sqlite_bundle_v3.py coordinates the current bundle",
        )


def _relative_path(
    value: object,
    *,
    prefix: str,
    suffixes: tuple[str, ...],
    label: str,
) -> str:
    if type(value) is not str or not 1 <= len(value) <= 512:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            f"authority {label} path is invalid",
        )
    if "\\" in value:
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            f"authority {label} path must use POSIX separators",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
        or not value.startswith(prefix)
        or not value.endswith(suffixes)
    ):
        _fail(
            "TBM_AUTHORITY_REGISTRY_INVALID",
            f"authority {label} path is outside its canonical root",
        )
    return value


def _require_regular_file(root: Path, path: Path, *, label: str) -> None:
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
    except OSError as error:
        raise AuthorityRegistryError(
            "TBM_AUTHORITY_REGISTRY_FILE_MISSING",
            f"{label} is missing",
        ) from error
    if root_resolved not in path_resolved.parents or not path_resolved.is_file():
        _fail(
            "TBM_AUTHORITY_REGISTRY_PATH_INVALID",
            f"{label} is outside the repository or not a file",
        )
    root_absolute = root.absolute()
    current = path.absolute()
    if root_absolute not in current.parents:
        _fail(
            "TBM_AUTHORITY_REGISTRY_PATH_INVALID",
            f"{label} is outside the repository",
        )
    while current != root_absolute:
        if current.is_symlink():
            _fail(
                "TBM_AUTHORITY_REGISTRY_PATH_INVALID",
                f"{label} cannot traverse a symbolic link",
            )
        current = current.parent


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(
                "TBM_AUTHORITY_REGISTRY_DUPLICATE",
                "authority registry contains a duplicate JSON key",
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    _fail(
        "TBM_AUTHORITY_REGISTRY_INVALID",
        "authority registry contains a non-finite number",
    )


def _fail(code: str, message: str) -> NoReturn:
    raise AuthorityRegistryError(code, message)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every persistence-v3 module declares one governed "
            "ledger, projection, migration, or coordinator role."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root to verify",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="registry path, relative to --root unless absolute",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        entries = verify_authority_registry(args.root, args.registry)
    except AuthorityRegistryError as error:
        print(
            f"authority registry verification failed [{error.code}]: {error}",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "entries": len(entries),
                "modules": len({entry.module for entry in entries}),
                "registry_version": AUTHORITY_REGISTRY_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
