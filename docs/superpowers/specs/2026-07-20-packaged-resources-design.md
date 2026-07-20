# Packaged Resources Design

## Problem

The repository documents JSON Schemas, the PostgreSQL fresh-install schema,
failure taxonomy, and executable examples as part of the product. Current wheel
and source distributions contain none of those files. A caller installed from
PyPI therefore cannot follow the documented PostgreSQL setup or load the
canonical taxonomy without also cloning the repository.

Phase 27 makes the existing 18 canonical files part of every distribution and
adds one small resource interface that works for wheels, source distributions,
editable installs, and zip importers. The top-level repository files remain the
authoring source of truth.

## Resource Set

The public resource names are the canonical POSIX-style repository paths:

- every file under `schemas/`;
- `memory/failure_taxonomy.yaml` and `memory/lessons.example.yaml`;
- every JSON file under `examples/`.

The allowlist is explicit and lexicographically ordered. Unknown names,
absolute paths, backslashes, empty segments, and traversal segments are never
resolved against the package. Package copies live under
`trace_backed_memory/_resources/` and must be byte-identical to the canonical
top-level files.

## Public Interface

Add `trace_backed_memory.resources` with three entry points and one immutable
description value:

```python
@dataclass(frozen=True)
class PackagedResource:
    name: str
    kind: Literal["schema", "memory", "example"]
    media_type: str
    size_bytes: int
    sha256: str


def packaged_resources() -> tuple[PackagedResource, ...]: ...


def read_packaged_resource(name: str) -> bytes: ...


def export_packaged_resource(
    name: str,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> Path: ...
```

`packaged_resources()` returns every allowlisted resource in name order with
metadata derived from the packaged bytes. `read_packaged_resource()` returns a
new bytes value. `export_packaged_resource()` writes those exact bytes, refuses
an existing destination unless overwrite is explicit, uses a same-directory
temporary file plus replacement, and removes its temporary file on failure.
It does not create the destination parent.

Expose one `PackagedResourceError(RuntimeError)` carrying `operation`
(`lookup`, `read`, or `export`), `name`, and optional `destination`. Original
filesystem/resource failures remain available as `__cause__`. An unknown name
is a lookup error; missing or unreadable installed package data is a read error;
destination and replacement failures are export errors.

Use `importlib.resources.files()` internally. Do not expose a filesystem path,
depend on the current directory, or use `Path(__file__)`, because those choices
would break zip-safe access.

## Failure Taxonomy

Keep explicit path loading compatible and make the common installed-package
case trivial:

```python
load_failure_taxonomy()        # packaged canonical taxonomy
load_failure_taxonomy(path)    # caller-owned file, unchanged behavior
```

The existing parser remains the only taxonomy parser. The resource module
returns bytes and does not learn taxonomy semantics.

## CLI Adapter

Add these dependency-free commands:

```text
tbm resource list
tbm resource read NAME
tbm resource export NAME DESTINATION [--overwrite]
```

All commands preserve the CLI's one-value canonical JSON contract. `list`
returns ordered descriptions. `read` returns the description plus UTF-8 text.
`export` returns the description, destination, and whether an existing file was
replaced. Unknown names are input errors (exit 2), installed resource failures
are internal errors (exit 1), and export failures use exit 4. If stdout closes
after export succeeds, return success so a caller does not retry a completed
write.

The CLI is an adapter over the public resource interface. It must not scan the
checkout or package directories itself.

## Packaging And Typing

Declare the exact package-data patterns for `_resources/` and `py.typed` in
`pyproject.toml`, and add the `Typing :: Typed` classifier. The runtime remains
dependency-free.

Committed package copies intentionally duplicate the small canonical files so
standard setuptools builds need no custom build backend. Tests make drift
unshippable by comparing the complete name set and every byte against the
top-level source files.

## Rejected Alternatives

- A migration-aware resource catalog adds speculative methods and error types
  before any resource migration exists.
- Reader/writer ports introduce a hypothetical seam; the only runtime adapter
  is `importlib.resources`.
- `data-files` produce installation-layout-dependent paths and are not zip-safe.
- Raw CLI output is convenient for pipes but violates the established
  deterministic JSON command contract. Explicit export covers PostgreSQL setup.
- Filesystem fallbacks hide broken packages and make behavior depend on the
  caller's working directory.

## Verification

Tests must cover deterministic descriptions, exact bytes and digests, strict
name rejection, defensive reads, default taxonomy loading, explicit taxonomy
paths, no-overwrite and overwrite export behavior, cleanup after injected write
failure, all CLI commands and exit classes, and stdout failure after export.

Build wheel and source distribution. Verify both contain exactly the allowlisted
package copies plus `py.typed`; install each independently with `PYTHONPATH`
cleared; list, read, export, and parse the taxonomy; and compare exported bytes
to the canonical source. No snapshot, JSON Schema, active-lesson YAML, or
PostgreSQL schema version changes.
