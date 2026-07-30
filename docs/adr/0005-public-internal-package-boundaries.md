# ADR-0005: public and internal package boundaries

**Status:** Accepted
**Date:** 2026-07-30
**简体中文:** [0005-public-internal-package-boundaries.zh-CN.md](0005-public-internal-package-boundaries.zh-CN.md)

## Context

The source package contains many top-level v3 contracts, services, and
SQLite/PostgreSQL authorities. Re-exporting every implementation from the
package root makes internal composition look stable, hides dependency
direction, and increases import-cycle and optional-dependency risk.

## Decision

- Preserve existing documented package-root exports for compatibility.
- New internal code imports from owning modules, never from the package root.
- Organize future moves around compatibility, contracts, services,
  authorities, transports, SDKs, migrations, and resources.
- Expose a small public durable surface through explicit transport contracts,
  client types, and runtime factories; repositories and service graph details
  remain internal unless independently documented.
- Optional transport dependencies must not become mandatory merely because a
  root module is imported.
- Add dependency-direction and import-smoke tests before physical package moves.

## Consequences

Package reorganization is incremental and uses compatibility re-exports rather
than a flag day. A source file's existence does not make it public API.

## Exit evidence

Supported root imports remain stable, internal modules avoid root imports,
optional extras remain optional, and dependency-direction tests reject cycles
or transport-to-policy inversion.
