# ADR-0004: canonical resource manifest

**Status:** Accepted
**Date:** 2026-07-30
**简体中文:** [0004-canonical-resource-manifest.zh-CN.md](0004-canonical-resource-manifest.zh-CN.md)

## Context

Canonical schemas, examples, SQL, and policy files are mirrored into installed
package resources. The strict allowlist is a security and distribution
guarantee, but manually synchronizing `pyproject.toml`, `_RESOURCE_SPECS`,
documentation, and fixture counts is error-prone.

## Decision

- Introduce one canonical, deterministic resource manifest.
- The manifest records public resource name, canonical source path, installed
  path, media type, and SHA-256.
- A repository generator/checker derives or verifies package-data,
  `_RESOURCE_SPECS`, the documentation resource index, and distribution
  expectations.
- Canonical files remain authored outside the installed resource tree; the
  installed copies remain byte-identical.
- Generation is explicit and offline. Runtime import, tests, and verification
  never fetch network resources.
- CI fails when generated outputs or copied bytes drift.

## Consequences

Adding a public resource requires one manifest update plus canonical bytes;
the checker identifies every affected generated representation. The manifest
does not relax the allowlist.

## Exit evidence

Wheel, sdist, editable install, public resource API, and a clean regeneration
all report the same ordered resource set and exact digests.
