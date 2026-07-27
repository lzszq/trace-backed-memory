from __future__ import annotations

import ast
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import TypeVar


ROOT = Path(__file__).resolve().parents[1]
T = TypeVar("T")


def _canonical_resource_names() -> tuple[str, ...]:
    return tuple(
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
    resource_names = _canonical_resource_names()
    assert _declared_resource_names() == resource_names

    wheels = sorted(dist_directory.glob("*.whl"))
    source_distributions = sorted(dist_directory.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected one wheel, found: {wheels}"
    assert len(source_distributions) == 1, (
        "expected one source distribution, found: "
        f"{source_distributions}"
    )

    package_prefix = "trace_backed_memory/_resources/"
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
