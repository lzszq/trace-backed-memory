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
