import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.resources as resource_module


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_RESOURCE_NAMES = tuple(
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


def test_packaged_resources_match_every_canonical_file_byte_for_byte():
    descriptions = tbm.packaged_resources()

    assert tuple(item.name for item in descriptions) == CANONICAL_RESOURCE_NAMES
    assert len(descriptions) == 78
    for item in descriptions:
        canonical = (ROOT / item.name).read_bytes()
        assert tbm.read_packaged_resource(item.name) == canonical
        assert item.size_bytes == len(canonical)
        assert item.sha256 == hashlib.sha256(canonical).hexdigest()
        if item.name.startswith("schemas/"):
            assert item.kind == "schema"
        elif item.name.startswith("memory/"):
            assert item.kind == "memory"
        else:
            assert item.kind == "example"

    assert "schemas/AGENTS.md" not in {
        item.name for item in descriptions
    }

    with pytest.raises(FrozenInstanceError):
        descriptions[0].name = "changed"  # type: ignore[misc]


def test_resource_media_types_are_deterministic():
    by_name = {item.name: item for item in tbm.packaged_resources()}

    assert by_name["schemas/postgres.sql"].media_type == "application/sql"
    assert (
        by_name["schemas/postgres-v1-to-v2.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/postgres-v2-lock-order-hotfix.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/postgres-v3-audit.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name[
            "schemas/postgres-v3-audit-rollback.sql"
        ].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/postgres-v3-gate-session.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name[
            "schemas/postgres-v3-gate-session-rollback.sql"
        ].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/postgres-v3-replay.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name[
            "schemas/postgres-v3-replay-rollback.sql"
        ].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/postgres-v3-staging.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/postgres-v3-staging-rollback.sql"].media_type
        == "application/sql"
    )
    assert by_name["schemas/sqlite.sql"].media_type == "application/sql"
    assert (
        by_name["schemas/sqlite-v3-audit.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/sqlite-v3-gate-session.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/sqlite-v3-migration.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/sqlite-v3-replay.sql"].media_type
        == "application/sql"
    )
    assert (
        by_name["schemas/trace.schema.json"].media_type
        == "application/schema+json"
    )
    assert (
        by_name["schemas/gate_session_v3.schema.json"].media_type
        == "application/schema+json"
    )
    assert (
        by_name["schemas/authorization_policy_v3.schema.json"].media_type
        == "application/schema+json"
    )
    assert (
        by_name["schemas/authorization_decision_v3.schema.json"].media_type
        == "application/schema+json"
    )
    assert (
        by_name["schemas/snapshot_v3_migration_mapping.schema.json"].media_type
        == "application/schema+json"
    )
    assert (
        by_name["schemas/snapshot_v3_migration_bundle.schema.json"].media_type
        == "application/schema+json"
    )
    assert (
        by_name["examples/trace.example.json"].media_type
        == "application/json"
    )
    assert (
        by_name["examples/gate_session_v3.example.json"].media_type
        == "application/json"
    )
    assert (
        by_name["examples/authorization_policy_v3.example.json"].media_type
        == "application/json"
    )
    assert (
        by_name["examples/authorization_decision_v3.example.json"].media_type
        == "application/json"
    )
    assert by_name["examples/quickstart.py"].media_type == "text/x-python"
    assert (
        by_name["memory/failure_taxonomy.yaml"].media_type
        == "application/yaml"
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../schemas/postgres.sql",
        "/schemas/postgres.sql",
        "schemas\\postgres.sql",
        "schemas//postgres.sql",
        "schemas/not-packaged.sql",
    ],
)
def test_resource_names_are_strictly_allowlisted(name):
    with pytest.raises(tbm.PackagedResourceError) as raised:
        tbm.read_packaged_resource(name)

    assert raised.value.operation == "lookup"
    assert raised.value.name == name
    assert raised.value.destination is None
    assert raised.value.__cause__ is None


def test_export_refuses_existing_destination_then_replaces_explicitly(tmp_path):
    name = "schemas/postgres.sql"
    destination = tmp_path / "postgres.sql"
    expected = (ROOT / name).read_bytes()

    returned = tbm.export_packaged_resource(name, destination)

    assert returned == destination
    assert destination.read_bytes() == expected
    assert list(tmp_path.glob(".postgres.sql.*.tmp")) == []

    destination.write_bytes(b"caller-owned")
    with pytest.raises(tbm.PackagedResourceError) as raised:
        tbm.export_packaged_resource(name, destination)

    assert raised.value.operation == "export"
    assert raised.value.destination == destination
    assert isinstance(raised.value.__cause__, FileExistsError)
    assert destination.read_bytes() == b"caller-owned"
    assert list(tmp_path.glob(".postgres.sql.*.tmp")) == []

    tbm.export_packaged_resource(name, destination, overwrite=True)
    assert destination.read_bytes() == expected
    assert list(tmp_path.glob(".postgres.sql.*.tmp")) == []


def test_export_failure_removes_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / "postgres.sql"

    def reject_link(_source, _destination):
        raise OSError("injected resource export failure")

    monkeypatch.setattr(resource_module.os, "link", reject_link)

    with pytest.raises(tbm.PackagedResourceError) as raised:
        tbm.export_packaged_resource("schemas/postgres.sql", destination)

    assert raised.value.operation == "export"
    assert isinstance(raised.value.__cause__, OSError)
    assert "injected resource export failure" in str(raised.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_export_value_error_is_structured_and_leaves_no_temporary_file(
    tmp_path,
    monkeypatch,
):
    def reject_temporary_file(**_kwargs):
        raise ValueError("injected invalid export path")

    monkeypatch.setattr(
        resource_module.tempfile,
        "NamedTemporaryFile",
        reject_temporary_file,
    )

    with pytest.raises(tbm.PackagedResourceError) as raised:
        tbm.export_packaged_resource(
            "schemas/postgres.sql",
            tmp_path / "postgres.sql",
        )

    assert raised.value.operation == "export"
    assert isinstance(raised.value.__cause__, ValueError)
    assert "injected invalid export path" in str(raised.value)
    assert list(tmp_path.iterdir()) == []


def test_export_does_not_report_false_failure_after_atomic_publication(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "postgres.sql"
    original_unlink = Path.unlink

    def reject_temporary_cleanup(path, *args, **kwargs):
        if path.name.startswith(".postgres.sql."):
            raise OSError("injected temporary cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as cleanup_failure:
        cleanup_failure.setattr(Path, "unlink", reject_temporary_cleanup)
        returned = tbm.export_packaged_resource(
            "schemas/postgres.sql",
            destination,
        )

    assert returned == destination
    assert destination.read_bytes() == (
        ROOT / "schemas" / "postgres.sql"
    ).read_bytes()
    for temporary_path in tmp_path.glob(".postgres.sql.*.tmp"):
        temporary_path.unlink()


def test_export_syncs_parent_directory_after_publication(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "postgres.sql"
    directory_descriptor = 987654
    original_open = resource_module.os.open
    original_fsync = resource_module.os.fsync
    original_close = resource_module.os.close
    calls = []

    def tracking_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            calls.append(("open-directory", Path(path)))
            return directory_descriptor
        return original_open(path, flags, *args, **kwargs)

    def tracking_fsync(descriptor):
        if descriptor == directory_descriptor:
            calls.append(("fsync-directory", descriptor))
            return None
        calls.append(("fsync-file", descriptor))
        return original_fsync(descriptor)

    def tracking_close(descriptor):
        if descriptor == directory_descriptor:
            calls.append(("close-directory", descriptor))
            return None
        return original_close(descriptor)

    monkeypatch.setattr(
        resource_module,
        "_POSIX_DIRECTORY_SYNC_SUPPORTED",
        True,
    )
    monkeypatch.setattr(resource_module.os, "open", tracking_open)
    monkeypatch.setattr(resource_module.os, "fsync", tracking_fsync)
    monkeypatch.setattr(resource_module.os, "close", tracking_close)

    tbm.export_packaged_resource("schemas/postgres.sql", destination)

    assert calls[-3:] == [
        ("open-directory", tmp_path),
        ("fsync-directory", directory_descriptor),
        ("close-directory", directory_descriptor),
    ]


def test_missing_installed_resource_has_structured_read_error(monkeypatch):
    class BrokenResource:
        def joinpath(self, *_parts):
            return self

        def read_bytes(self):
            raise FileNotFoundError("injected missing package data")

    monkeypatch.setattr(resource_module, "files", lambda _package: BrokenResource())

    with pytest.raises(tbm.PackagedResourceError) as raised:
        tbm.read_packaged_resource("schemas/postgres.sql")

    assert raised.value.operation == "read"
    assert raised.value.name == "schemas/postgres.sql"
    assert isinstance(raised.value.__cause__, FileNotFoundError)


def test_packaged_resource_interface_and_typing_marker_are_public():
    expected_names = {
        "PackagedResource",
        "PackagedResourceError",
        "export_packaged_resource",
        "packaged_resources",
        "read_packaged_resource",
    }

    assert expected_names <= set(tbm.__all__)
    for name in expected_names:
        assert getattr(tbm, name) is not None
    assert (ROOT / "src" / "trace_backed_memory" / "py.typed").is_file()
