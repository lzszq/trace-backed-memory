from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import trace_backed_memory as tbm
from tools import generate_resources


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "resources" / "manifest.json"


def _entries() -> list[dict[str, object]]:
    payload = json.loads(
        MANIFEST.read_text(encoding="utf-8"),
        object_pairs_hook=generate_resources._reject_duplicates,
        parse_constant=lambda value: generate_resources._fail(
            f"non-finite JSON value: {value}"
        ),
    )
    assert payload["manifest_version"] == "tbm.resource-manifest.v1"
    entries = payload["resources"]
    assert isinstance(entries, list)
    return entries


def test_resource_manifest_is_the_exact_runtime_allowlist() -> None:
    entries = _entries()
    names = [entry["name"] for entry in entries]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert names == [item.name for item in tbm.packaged_resources()]

    for entry in entries:
        name = entry["name"]
        assert isinstance(name, str)
        source = ROOT / str(entry["source"])
        installed = ROOT / str(entry["installed"])
        data = source.read_bytes()
        assert b"\r\n" not in data, f"canonical resource is not LF-only: {name}"
        assert installed.read_bytes() == data
        assert tbm.read_packaged_resource(name) == data
        assert entry["size_bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resource_manifest.json"
    ).read_bytes() == MANIFEST.read_bytes()


def test_resource_manifest_generator_reports_a_clean_tree() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/generate_resources.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "resource manifest verified" in completed.stdout


def test_resource_manifest_and_generator_ship_in_the_source_distribution() -> None:
    manifest_in = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "include resources/manifest.json" in manifest_in
    assert "include tools/generate_resources.py" in manifest_in
    assert "include tools/generate_sqlite_v3_bundle.py" in manifest_in
    package_data = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"_resource_manifest.json"' in package_data


@pytest.mark.parametrize(
    "content, expected",
    [
        (
            '{"manifest_version":"tbm.resource-manifest.v1",'
            '"manifest_version":"tbm.resource-manifest.v1","resources":[]}',
            "duplicate JSON key",
        ),
        (
            '{"manifest_version":"tbm.resource-manifest.v1",'
            '"resources":[],"unexpected":NaN}',
            "non-finite JSON value",
        ),
    ],
)
def test_resource_manifest_loader_rejects_ambiguous_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected: str,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(content, encoding="utf-8")
    monkeypatch.setattr(generate_resources, "MANIFEST_PATH", manifest)

    with pytest.raises(
        generate_resources.ResourceManifestError,
        match=expected,
    ):
        generate_resources._load_manifest()


def test_resource_manifest_entry_rejects_path_traversal() -> None:
    manifest = {
        "manifest_version": "tbm.resource-manifest.v1",
        "resources": [
            {
                "name": "schemas/../outside.sql",
                "kind": "schema",
                "media_type": "application/sql",
                "source": "schemas/../outside.sql",
                "installed": (
                    "src/trace_backed_memory/_resources/"
                    "schemas/../outside.sql"
                ),
                "size_bytes": 0,
                "sha256": "0" * 64,
            }
        ],
    }

    with pytest.raises(
        generate_resources.ResourceManifestError,
        match="entry is invalid",
    ):
        generate_resources._manifest_entries(
            manifest,
            verify_digests=False,
        )
