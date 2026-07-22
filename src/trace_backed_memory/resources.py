from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal


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
    ("examples/failure_case.example.json", "example", "application/json"),
    ("examples/lesson.example.json", "example", "application/json"),
    ("examples/memory_context.example.json", "example", "application/json"),
    ("examples/memory_decision.example.json", "example", "application/json"),
    ("examples/memory_usage_log.example.json", "example", "application/json"),
    ("examples/project_policy.example.json", "example", "application/json"),
    ("examples/trace.example.json", "example", "application/json"),
    ("memory/failure_taxonomy.yaml", "memory", "application/yaml"),
    ("memory/lessons.example.yaml", "memory", "application/yaml"),
    ("schemas/failure_case.schema.json", "schema", "application/schema+json"),
    ("schemas/lesson.schema.json", "schema", "application/schema+json"),
    ("schemas/memory_context.schema.json", "schema", "application/schema+json"),
    ("schemas/memory_decision.schema.json", "schema", "application/schema+json"),
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
    ("schemas/postgres.sql", "schema", "application/sql"),
    ("schemas/project_policy.schema.json", "schema", "application/schema+json"),
    ("schemas/sqlite.sql", "schema", "application/sql"),
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
