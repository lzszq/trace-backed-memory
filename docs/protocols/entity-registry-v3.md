# Entity registry v3

[简体中文](entity-registry-v3.zh-CN.md) | **English**

`tbm.entity-registry.v3` is the storage-neutral identity hierarchy that closes
the authorization-v3 tenant namespace. It introduces immutable Organization,
Tenant, and Environment records while reusing the existing authorization
policy's Principal, AgentClient, canonical Repository, RepositoryAlias, and
RoleBinding records.

## Invariants

- Every tenant belongs to one known organization.
- Every tenant referenced by a principal, client, repository binding, alias,
  or role-binding scope exists in the registry and is active under an active
  organization.
- Authorization-v3 continues to require exactly one tenant binding for every
  canonical repository and unambiguous tenant-scoped aliases.
- An environment belongs to one tenant. If it names a repository, that
  repository must be known and bound to the same tenant.
- Entity identifiers are unique within each collection. Status is explicit and
  forward-only persistence is expected to preserve immutable identity fields.

The registry is a versioned, content-addressed snapshot. Its
`registry_sha256` is derived from canonical JSON and the nested authorization
policy keeps its independent `policy_sha256`.

## Trust boundary

The contract validates referential integrity; it does not authenticate a
caller or authorize an operation. A service must derive Principal and
AgentClient identities from trusted authentication, load an accepted registry
snapshot, authorize before retrieval, and record the resulting decision.
Scope matching alone is not tenant security.

The opt-in `SQLiteEntityRegistryV3Repository` installs an isolated, side-by-side
schema. Its normalized snapshot namespace stores every entity, binding,
permission, and scope/environment attribute under composite foreign keys.
Canonical JSON is an integrity witness: every load compares every normalized
row back to the descriptor. Rows are immutable, exact replay is idempotent,
version/hash conflicts fail closed, schema drift and required PRAGMAs are
checked per operation, and caller transactions are preserved with savepoints.

The active v2 Store, Agent, and MCP adapters do not yet consume this registry.
PostgreSQL persistence and authenticated service integration are separate
delivery steps.

## Resources

- `schemas/entity_registry_v3.schema.json`
- `examples/entity_registry_v3.example.json`
- `schemas/authorization_policy_v3.schema.json`
- `schemas/sqlite-v3-entity-registry.sql`
