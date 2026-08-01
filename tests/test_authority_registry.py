from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools.verify_authority_registry import (
    AUTHORITY_REGISTRY_VERSION,
    AuthorityRegistryError,
    discover_persistence_modules,
    load_authority_registry,
    verify_authority_registry,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = Path("docs/status/authority-registry.json")


def _temporary_registry_repo(tmp_path: Path) -> Path:
    entries = load_authority_registry(ROOT)
    paths = {entry.module for entry in entries}
    paths.update(schema for entry in entries for schema in entry.schemas)
    paths.add(REGISTRY.as_posix())
    for relative in sorted(paths):
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return tmp_path


def _registry_document(root: Path) -> dict[str, object]:
    return json.loads((root / REGISTRY).read_text(encoding="utf-8"))


def _write_registry(root: Path, document: dict[str, object]) -> None:
    (root / REGISTRY).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_current_authority_registry_is_exact_and_preserves_f0_boundary() -> None:
    entries = verify_authority_registry(ROOT)

    assert len(entries) == 32
    assert len(discover_persistence_modules(ROOT)) == 32
    assert Counter(entry.role for entry in entries) == {
        "ledger": 28,
        "projection": 2,
        "bundle-coordinator": 1,
        "compatibility-migration": 1,
    }
    assert Counter(entry.source_of_truth for entry in entries) == {
        "migration-asset": 27,
        "artifact-authority": 2,
        "none": 3,
    }
    assert {
        entry.module
        for entry in entries
        if entry.source_of_truth == "artifact-authority"
    } == {
        "src/trace_backed_memory/postgres_artifact_v3.py",
        "src/trace_backed_memory/sqlite_artifact_v3.py",
    }
    assert all(
        "canonical-event-ledger" not in entry.source_of_truth
        for entry in entries
    )


def test_authority_registry_cli_reports_stable_inventory() -> None:
    result = subprocess.run(
        [sys.executable, "tools/verify_authority_registry.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "entries": 32,
        "modules": 32,
        "registry_version": AUTHORITY_REGISTRY_VERSION,
    }
    assert result.stderr == ""


def test_new_persistence_module_is_rejected_until_registered(
    tmp_path: Path,
) -> None:
    root = _temporary_registry_repo(tmp_path)
    module = root / "src/trace_backed_memory/sqlite_new_authority_v3.py"
    module.write_text("# deliberately unregistered\n", encoding="utf-8")

    with pytest.raises(AuthorityRegistryError) as error:
        verify_authority_registry(root)

    assert error.value.code == "TBM_AUTHORITY_REGISTRY_UNREGISTERED_AUTHORITY"
    assert "sqlite_new_authority_v3.py" in str(error.value)


def test_unsafe_or_noncanonical_persistence_candidates_fail_closed(
    tmp_path: Path,
) -> None:
    root = _temporary_registry_repo(tmp_path)
    candidate = root / "src/trace_backed_memory/sqlite_new_authority_v3.py"
    candidate.mkdir()
    with pytest.raises(AuthorityRegistryError) as unsafe:
        discover_persistence_modules(root)
    assert unsafe.value.code == "TBM_AUTHORITY_REGISTRY_INVENTORY_INVALID"

    candidate.rmdir()
    noncanonical = root / "src/trace_backed_memory/sqlitefoo_v3.py"
    noncanonical.write_text("# invalid authority name\n", encoding="utf-8")
    with pytest.raises(AuthorityRegistryError) as invalid_name:
        discover_persistence_modules(root)
    assert (
        invalid_name.value.code
        == "TBM_AUTHORITY_REGISTRY_INVENTORY_INVALID"
    )


def test_duplicate_json_keys_and_modules_fail_closed(tmp_path: Path) -> None:
    root = _temporary_registry_repo(tmp_path)
    path = root / REGISTRY
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            '"registry_version": "tbm.authority-registry.v1",',
            '"registry_version": "tbm.authority-registry.v1",\n'
            '  "registry_version": "tbm.authority-registry.v1",',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(AuthorityRegistryError) as duplicate_key:
        load_authority_registry(root)
    assert duplicate_key.value.code == "TBM_AUTHORITY_REGISTRY_DUPLICATE"

    shutil.copy2(ROOT / REGISTRY, path)
    document = _registry_document(root)
    entries = document["entries"]
    assert type(entries) is list
    assert type(entries[0]) is dict and type(entries[1]) is dict
    entries[1]["module"] = entries[0]["module"]
    _write_registry(root, document)
    with pytest.raises(AuthorityRegistryError) as duplicate_module:
        load_authority_registry(root)
    assert duplicate_module.value.code == "TBM_AUTHORITY_REGISTRY_DUPLICATE"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("role", "independent-authority", "TBM_AUTHORITY_REGISTRY_ROLE_INVALID"),
        (
            "source_of_truth",
            "independent",
            "TBM_AUTHORITY_REGISTRY_SOURCE_INVALID",
        ),
        (
            "event_projection_impact",
            "unreviewed",
            "TBM_AUTHORITY_REGISTRY_IMPACT_INVALID",
        ),
    ],
)
def test_unregistered_role_source_or_impact_is_rejected(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    root = _temporary_registry_repo(tmp_path)
    document = _registry_document(root)
    entries = document["entries"]
    assert type(entries) is list and type(entries[0]) is dict
    entries[0][field] = value
    _write_registry(root, document)

    with pytest.raises(AuthorityRegistryError) as error:
        load_authority_registry(root)

    assert error.value.code == code


def test_role_source_and_projection_impact_must_remain_aligned(
    tmp_path: Path,
) -> None:
    root = _temporary_registry_repo(tmp_path)
    document = _registry_document(root)
    entries = document["entries"]
    assert type(entries) is list and type(entries[0]) is dict
    entries[0]["role"] = "projection"
    entries[0]["source_of_truth"] = "none"
    entries[0]["event_projection_impact"] = "replaceable-projection"
    _write_registry(root, document)

    with pytest.raises(AuthorityRegistryError) as error:
        load_authority_registry(root)

    assert error.value.code == "TBM_AUTHORITY_REGISTRY_BOUNDARY_INVALID"


def test_authority_paths_are_existing_canonical_repository_files(
    tmp_path: Path,
) -> None:
    root = _temporary_registry_repo(tmp_path)
    document = _registry_document(root)
    entries = document["entries"]
    assert type(entries) is list and type(entries[0]) is dict
    entries[0]["schemas"] = ["schemas/../outside.sql"]
    _write_registry(root, document)

    with pytest.raises(AuthorityRegistryError) as traversal:
        load_authority_registry(root)
    assert traversal.value.code == "TBM_AUTHORITY_REGISTRY_INVALID"

    shutil.copy2(ROOT / REGISTRY, root / REGISTRY)
    (root / entries[0]["module"]).unlink()
    with pytest.raises(AuthorityRegistryError) as missing:
        verify_authority_registry(root)
    assert missing.value.code == "TBM_AUTHORITY_REGISTRY_FILE_MISSING"


def test_pull_request_template_requires_event_and_authority_declarations() -> None:
    template = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )
    template_lower = template.lower()

    assert "Event / projection impact" in template
    assert "Authority registry role" in template
    assert "source of truth" in template_lower
    assert "compatibility / migration" in template_lower
