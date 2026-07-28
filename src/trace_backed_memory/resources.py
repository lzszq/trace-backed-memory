from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal


_POSIX_DIRECTORY_SYNC_SUPPORTED = os.name == "posix"


@dataclass(frozen=True)
class PackagedResource:
    name: str
    kind: Literal["schema", "memory", "example"]
    media_type: str
    size_bytes: int
    sha256: str


class PackagedResourceError(RuntimeError):
    """Reports a resource operation without exposing package internals."""

    def __init__(
        self,
        operation: Literal["lookup", "read", "export"],
        *,
        name: str,
        destination: Path | None = None,
        detail: str | None = None,
    ) -> None:
        self.operation = operation
        self.name = name
        self.destination = destination
        if operation == "lookup":
            message = f"unknown packaged resource: {name}"
        elif operation == "read":
            message = f"could not read packaged resource: {name}"
        else:
            message = f"could not export packaged resource {name} to {destination}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


_RESOURCE_SPECS: tuple[
    tuple[str, Literal["schema", "memory", "example"], str],
    ...,
] = (
    ("examples/agent_capabilities.example.json", "example", "application/json"),
    ("examples/agent_completed.example.json", "example", "application/json"),
    ("examples/agent_error.example.json", "example", "application/json"),
    ("examples/agent_finalized.example.json", "example", "application/json"),
    ("examples/agent_prepared.example.json", "example", "application/json"),
    ("examples/audit_event_v3.example.json", "example", "application/json"),
    (
        "examples/authorization_decision_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/authorization_policy_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/decision_replay_manifest_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/entity_registry_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/failure_case.example.json", "example", "application/json"),
    (
        "examples/fix_evidence_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/gate_session_v3.example.json", "example", "application/json"),
    (
        "examples/injection_artifact_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/lesson.example.json", "example", "application/json"),
    ("examples/memory_context.example.json", "example", "application/json"),
    ("examples/memory_decision.example.json", "example", "application/json"),
    (
        "examples/memory_revision_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/memory_usage_log.example.json", "example", "application/json"),
    (
        "examples/outcome_attribution_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/project_policy.example.json", "example", "application/json"),
    ("examples/quickstart.py", "example", "text/x-python"),
    (
        "examples/recovery_action_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/retrieval_snapshot_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/run_outcome_v3.example.json", "example", "application/json"),
    (
        "examples/semantic_gate_artifact_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/semantic_gate_attempt_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/snapshot_v3_migration_bundle.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/snapshot_v3_migration_mapping.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/snapshot_v3_migration_plan.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/structured_regression_evidence_v3.example.json",
        "example",
        "application/json",
    ),
    (
        "examples/system_gate_evaluation_v3.example.json",
        "example",
        "application/json",
    ),
    ("examples/trace.example.json", "example", "application/json"),
    ("memory/failure_taxonomy.yaml", "memory", "application/yaml"),
    ("memory/lessons.example.yaml", "memory", "application/yaml"),
    (
        "schemas/agent_capabilities.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/agent_completed.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/agent_error.schema.json", "schema", "application/schema+json"),
    (
        "schemas/agent_finalized.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/agent_prepared.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/audit_event_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/authorization_decision_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/authorization_policy_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/decision_replay_manifest_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/entity_registry_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/failure_case.schema.json", "schema", "application/schema+json"),
    (
        "schemas/fix_evidence_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/gate_session_v3.schema.json", "schema", "application/schema+json"),
    (
        "schemas/injection_artifact_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/lesson.schema.json", "schema", "application/schema+json"),
    ("schemas/memory_context.schema.json", "schema", "application/schema+json"),
    ("schemas/memory_decision.schema.json", "schema", "application/schema+json"),
    (
        "schemas/memory_revision_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/memory_store_snapshot.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/memory_usage_log.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/outcome_attribution_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/postgres-v1-to-v2.sql", "schema", "application/sql"),
    (
        "schemas/postgres-v2-lock-order-hotfix.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-audit-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-audit.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-authorization-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-authorization.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-entity-registry-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-entity-registry.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-gate-evidence-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-gate-evidence.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-gate-session-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-gate-session.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-memory-revision-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-memory-revision.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-replay-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-replay.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-semantic-gate-artifacts-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-semantic-gate-artifacts.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-semantic-gate-rollback.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-semantic-gate.sql",
        "schema",
        "application/sql",
    ),
    (
        "schemas/postgres-v3-staging-rollback.sql",
        "schema",
        "application/sql",
    ),
    ("schemas/postgres-v3-staging.sql", "schema", "application/sql"),
    ("schemas/postgres.sql", "schema", "application/sql"),
    ("schemas/project_policy.schema.json", "schema", "application/schema+json"),
    (
        "schemas/recovery_action_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/retrieval_snapshot_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/run_outcome_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/semantic_gate_artifact_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/semantic_gate_attempt_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/snapshot_v3_migration_bundle.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/snapshot_v3_migration_mapping.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/snapshot_v3_migration_plan.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/sqlite-v3-audit.sql", "schema", "application/sql"),
    ("schemas/sqlite-v3-authorization.sql", "schema", "application/sql"),
    (
        "schemas/sqlite-v3-entity-registry.sql",
        "schema",
        "application/sql",
    ),
    ("schemas/sqlite-v3-gate-evidence.sql", "schema", "application/sql"),
    ("schemas/sqlite-v3-gate-session.sql", "schema", "application/sql"),
    ("schemas/sqlite-v3-memory-revision.sql", "schema", "application/sql"),
    ("schemas/sqlite-v3-migration.sql", "schema", "application/sql"),
    ("schemas/sqlite-v3-outcome.sql", "schema", "application/sql"),
    ("schemas/sqlite-v3-replay.sql", "schema", "application/sql"),
    (
        "schemas/sqlite-v3-semantic-gate-artifacts.sql",
        "schema",
        "application/sql",
    ),
    ("schemas/sqlite-v3-semantic-gate.sql", "schema", "application/sql"),
    ("schemas/sqlite.sql", "schema", "application/sql"),
    (
        "schemas/structured_regression_evidence_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    (
        "schemas/system_gate_evaluation_v3.schema.json",
        "schema",
        "application/schema+json",
    ),
    ("schemas/trace.schema.json", "schema", "application/schema+json"),
)
_RESOURCE_SPEC_BY_NAME = {
    name: (kind, media_type) for name, kind, media_type in _RESOURCE_SPECS
}


def packaged_resources() -> tuple[PackagedResource, ...]:
    """Return deterministic metadata for every installed canonical resource."""
    descriptions: list[PackagedResource] = []
    for name, kind, media_type in _RESOURCE_SPECS:
        data = _read_resource_bytes(name)
        descriptions.append(
            PackagedResource(
                name=name,
                kind=kind,
                media_type=media_type,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(descriptions)


def read_packaged_resource(name: str) -> bytes:
    """Read one allowlisted resource without requiring a filesystem path."""
    _resource_spec(name)
    return _read_resource_bytes(name)


def export_packaged_resource(
    name: str,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Export exact resource bytes with explicit replacement semantics."""
    _resource_spec(name)
    data = _read_resource_bytes(name)
    try:
        destination_path = Path(destination)
    except (TypeError, ValueError) as error:
        raise PackagedResourceError(
            "export",
            name=name,
            detail=str(error),
        ) from error

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_path.parent,
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        if overwrite:
            os.replace(temporary_path, destination_path)
        else:
            os.link(temporary_path, destination_path)
        _sync_parent_directory(destination_path.parent)
        return destination_path
    except (OSError, ValueError) as error:
        raise PackagedResourceError(
            "export",
            name=name,
            destination=destination_path,
            detail=str(error),
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _sync_parent_directory(directory: Path) -> None:
    if not _POSIX_DIRECTORY_SYNC_SUPPORTED:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(directory, flags)
    try:
        os.fsync(directory_descriptor)
    except BaseException as sync_error:
        try:
            os.close(directory_descriptor)
        except BaseException as close_error:
            sync_error.add_note(
                "also failed to close parent directory descriptor: "
                f"{close_error}"
            )
        raise
    else:
        os.close(directory_descriptor)


def _resource_spec(
    name: str,
) -> tuple[Literal["schema", "memory", "example"], str]:
    if type(name) is not str or name not in _RESOURCE_SPEC_BY_NAME:
        raise PackagedResourceError("lookup", name=str(name))
    return _RESOURCE_SPEC_BY_NAME[name]


def _read_resource_bytes(name: str) -> bytes:
    try:
        resource = files("trace_backed_memory").joinpath("_resources")
        for part in name.split("/"):
            resource = resource.joinpath(part)
        return resource.read_bytes()
    except PackagedResourceError:
        raise
    except Exception as error:
        raise PackagedResourceError(
            "read",
            name=name,
            detail=str(error),
        ) from error


__all__ = [
    "PackagedResource",
    "PackagedResourceError",
    "export_packaged_resource",
    "packaged_resources",
    "read_packaged_resource",
]
