import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_exposes_dependency_free_cli_entry_points():
    metadata = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["build-system"] == {
        "requires": ["setuptools>=77"],
        "build-backend": "setuptools.build_meta",
    }
    assert metadata["project"]["dependencies"] == []
    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert metadata["project"]["scripts"] == {
        "tbm": "trace_backed_memory.cli:main"
    }
    assert "Typing :: Typed" in metadata["project"]["classifiers"]


def test_package_data_exactly_covers_canonical_resources_and_typing_marker():
    metadata = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    package_data = metadata["tool"]["setuptools"]["package-data"][
        "trace_backed_memory"
    ]
    canonical_names = sorted(
        path.relative_to(ROOT).as_posix()
        for directory in (
            ROOT / "examples",
            ROOT / "memory",
            ROOT / "schemas",
        )
        for path in directory.rglob("*")
        if path.is_file()
    )

    assert package_data[0] == "py.typed"
    assert sorted(
        path.removeprefix("_resources/")
        for path in package_data[1:]
    ) == canonical_names
    for name in canonical_names:
        assert (
            ROOT / "src" / "trace_backed_memory" / "_resources" / name
        ).read_bytes() == (ROOT / name).read_bytes()
