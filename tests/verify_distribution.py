from __future__ import annotations

import ast
import hashlib
import json
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any, NoReturn, TypeVar


ROOT = Path(__file__).resolve().parents[1]
T = TypeVar("T")


def _fail(message: str) -> NoReturn:
    raise AssertionError(message)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        assert key not in result, f"duplicate manifest JSON key: {key}"
        result[key] = value
    return result


def _manifest_bytes() -> bytes:
    return (ROOT / "resources" / "manifest.json").read_bytes()


def _canonical_resource_entries() -> tuple[dict[str, object], ...]:
    manifest = json.loads(
        _manifest_bytes(),
        object_pairs_hook=_reject_duplicates,
        parse_constant=lambda value: _fail(
            f"non-finite manifest JSON value: {value}"
        ),
    )
    assert type(manifest) is dict
    assert set(manifest) == {"manifest_version", "resources"}
    assert manifest["manifest_version"] == "tbm.resource-manifest.v1"
    entries = manifest["resources"]
    assert type(entries) is list and entries
    exact_fields = {
        "name",
        "kind",
        "media_type",
        "source",
        "installed",
        "size_bytes",
        "sha256",
    }
    for entry in entries:
        assert type(entry) is dict and set(entry) == exact_fields
        name = entry["name"]
        assert type(name) is str and name
        assert entry["source"] == name
        assert entry["installed"] == (
            "src/trace_backed_memory/_resources/" + name
        )
        assert "\\" not in name
        assert not name.startswith("/")
        assert ".." not in Path(name).parts
        assert name.split("/", 1)[0] in {"examples", "memory", "schemas"}
        assert entry["kind"] in {"schema", "memory", "example"}
        assert type(entry["media_type"]) is str and entry["media_type"]
        assert type(entry["size_bytes"]) is int and entry["size_bytes"] >= 0
        digest = entry["sha256"]
        assert type(digest) is str and len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)

    names = tuple(entry["name"] for entry in entries)
    assert names == tuple(sorted(names))
    assert len(names) == len(set(names))
    assert names == tuple(
        sorted(
            path.relative_to(ROOT).as_posix()
            for directory in (
                ROOT / "examples",
                ROOT / "memory",
                ROOT / "schemas",
            )
            for path in directory.rglob("*")
            if (
                path.is_file()
                and path.name != "AGENTS.md"
                and "__pycache__" not in path.parts
            )
        )
    )
    for entry in entries:
        data = (ROOT / entry["source"]).read_bytes()
        assert entry["size_bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    return tuple(entries)


def _declared_resource_names() -> tuple[str, ...]:
    metadata = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    package_data = metadata["tool"]["setuptools"]["package-data"][
        "trace_backed_memory"
    ]
    return tuple(
        sorted(
            path.removeprefix("_resources/")
            for path in package_data
            if path.startswith("_resources/")
        )
    )


def _runtime_resource_names(source: bytes) -> tuple[str, ...]:
    module = ast.parse(source)
    declarations = [
        node.value
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_RESOURCE_SPECS"
    ]
    assert len(declarations) == 1, "expected one _RESOURCE_SPECS declaration"
    declaration = declarations[0]
    assert isinstance(declaration, ast.Tuple)

    names: list[str] = []
    for entry in declaration.elts:
        assert isinstance(entry, ast.Tuple) and len(entry.elts) == 3
        name = ast.literal_eval(entry.elts[0])
        assert isinstance(name, str)
        names.append(name)
    assert len(names) == len(set(names)), "runtime resource names must be unique"
    return tuple(names)


def _only(items: list[T], description: str) -> T:
    assert len(items) == 1, f"expected one {description}, found: {items}"
    return items[0]


def verify_distributions(dist_directory: Path) -> None:
    resource_entries = _canonical_resource_entries()
    resource_names = tuple(str(entry["name"]) for entry in resource_entries)
    assert _declared_resource_names() == resource_names

    wheels = sorted(dist_directory.glob("*.whl"))
    source_distributions = sorted(dist_directory.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected one wheel, found: {wheels}"
    assert len(source_distributions) == 1, (
        "expected one source distribution, found: "
        f"{source_distributions}"
    )

    package_prefix = "trace_backed_memory/_resources/"
    package_manifest = "trace_backed_memory/_resource_manifest.json"
    expected_members = {package_prefix + name for name in resource_names}
    with zipfile.ZipFile(wheels[0]) as archive:
        archived_names = archive.namelist()
        assert len(archived_names) == len(set(archived_names)), (
            "wheel member names must be unique"
        )
        names = set(archived_names)
        actual_members = {
            name
            for name in names
            if name.startswith(package_prefix) and not name.endswith("/")
        }
        assert actual_members == expected_members
        for name in resource_names:
            assert archive.read(package_prefix + name) == (
                ROOT / name
            ).read_bytes()
        assert archive.read(package_manifest) == _manifest_bytes()
        assert _runtime_resource_names(
            archive.read("trace_backed_memory/resources.py")
        ) == resource_names
        assert archive.read("trace_backed_memory/py.typed") == b""
        metadata_name = _only(
            [
                name
                for name in names
                if name.endswith(".dist-info/METADATA")
            ],
            "wheel metadata member",
        )
        metadata = archive.read(metadata_name)
        assert b"Classifier: Typing :: Typed" in metadata
        assert b"Provides-Extra: mcp" in metadata
        assert any(
            line.startswith(b"Requires-Dist: mcp")
            and b'extra == "mcp"' in line
            for line in metadata.splitlines()
        )

    with tarfile.open(source_distributions[0], mode="r:gz") as archive:
        file_members = [
            member for member in archive.getmembers() if member.isfile()
        ]
        roots = {member.name.split("/", 1)[0] for member in file_members}
        assert len(roots) == 1, f"expected one source root, found: {roots}"
        source_root = next(iter(roots))
        package_root = f"{source_root}/src/trace_backed_memory"
        root_manifest = f"{source_root}/resources/manifest.json"
        installed_manifest = f"{package_root}/_resource_manifest.json"
        resource_prefix = f"{package_root}/_resources/"
        resource_members = [
            member
            for member in file_members
            if member.name.startswith(resource_prefix)
        ]
        relative_names = [
            member.name.removeprefix(resource_prefix)
            for member in resource_members
        ]
        assert len(relative_names) == len(set(relative_names)), (
            "source distribution resource names must be unique"
        )
        assert set(relative_names) == set(resource_names)
        actual_by_name = dict(zip(relative_names, resource_members, strict=True))
        for name in resource_names:
            extracted = archive.extractfile(actual_by_name[name])
            assert extracted is not None
            assert extracted.read() == (ROOT / name).read_bytes()

        resources_module = _only(
            [
                member
                for member in file_members
                if member.name == f"{package_root}/resources.py"
            ],
            "source distribution resources module",
        )
        extracted_module = archive.extractfile(resources_module)
        assert extracted_module is not None
        assert _runtime_resource_names(extracted_module.read()) == resource_names

        generated_contract_members = (
            (root_manifest, _manifest_bytes()),
            (
                f"{source_root}/tools/generate_resources.py",
                (ROOT / "tools" / "generate_resources.py").read_bytes(),
            ),
            (
                f"{source_root}/tools/generate_sqlite_v3_bundle.py",
                (
                    ROOT / "tools" / "generate_sqlite_v3_bundle.py"
                ).read_bytes(),
            ),
            (installed_manifest, _manifest_bytes()),
        )
        for member_name, expected in generated_contract_members:
            member = _only(
                [
                    candidate
                    for candidate in file_members
                    if candidate.name == member_name
                ],
                f"source distribution member {member_name}",
            )
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == expected

        marker = _only(
            [
                member
                for member in file_members
                if member.name == f"{package_root}/py.typed"
            ],
            "source distribution typing marker",
        )
        extracted_marker = archive.extractfile(marker)
        assert extracted_marker is not None
        assert extracted_marker.read() == b""
        package_metadata = _only(
            [
                member
                for member in file_members
                if member.name == f"{source_root}/PKG-INFO"
            ],
            "source distribution package metadata",
        )
        extracted_metadata = archive.extractfile(package_metadata)
        assert extracted_metadata is not None
        metadata = extracted_metadata.read()
        assert b"Classifier: Typing :: Typed" in metadata
        assert b"Provides-Extra: mcp" in metadata
        assert any(
            line.startswith(b"Requires-Dist: mcp")
            and b'extra == "mcp"' in line
            for line in metadata.splitlines()
        )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise SystemExit("usage: verify_distribution.py DIST_DIRECTORY")
    verify_distributions(Path(arguments[0]))
    print("wheel and source distribution resources verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
