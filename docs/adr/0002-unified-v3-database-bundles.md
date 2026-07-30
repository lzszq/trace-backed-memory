# ADR-0002: unified version-3 database bundles

**Status:** Accepted
**Date:** 2026-07-30
**简体中文:** [0002-unified-v3-database-bundles.zh-CN.md](0002-unified-v3-database-bundles.zh-CN.md)

## Context

Version-3 SQLite and PostgreSQL authorities currently use isolated,
side-by-side component schemas. Those files are valuable implementation
sources, but their number, ordering, versions, and rollback dependencies are
not an acceptable operator interface.

## Decision

- Keep component SQL as reviewed implementation sources.
- Generate one SQLite install bundle and one PostgreSQL install bundle from an
  ordered component manifest.
- Provide one v2-to-v3 migration, one verifier, and one rollback plan per
  supported database profile.
- Record exact component names, contract versions, canonical byte digests, and
  installation order in the manifest.
- A runtime factory verifies the complete catalog before publishing a service
  bundle. Metadata rows alone are insufficient; required tables, columns,
  constraints, indexes, triggers, and PostgreSQL functions must match.
- Runtime startup never installs or migrates PostgreSQL implicitly.

## Consequences

The bundle version becomes a compatibility boundary and cannot change without
migration, rollback, documentation, and fixture coverage. Component drift
fails before any durable request is accepted.

## Exit evidence

Fresh install, upgrade, restart, drift rejection, failed-install rollback, and
operator rollback are tested for SQLite and PostgreSQL.
