# Authorization v3 contract

**English** | [简体中文](authorization-v3.zh-CN.md)

Status: published preparation contract with opt-in isolated SQLite and
PostgreSQL authorization authorities. They are not wired into the active
snapshot-v2 Store, local Agent, MCP adapter, or GateSession repositories. Those
paths remain process-local or explicitly opt-in as documented elsewhere.

Authorization v3 defines the identities, repository registry, role bindings,
requests, and content-derived decisions needed before a future service may
retrieve any tenant or repository data. It does not turn applicability matching
into authorization.

## Trust boundary

`principal_id`, `agent_client_id`, tenant context, and repository reference must
come from authenticated, server-owned request context. A caller-supplied JSON
field is not proof of identity. The contract deliberately contains no password,
bearer token, session secret, or credential reconstruction material.

`authorization_event_id`, `request_sha256`, and `policy_sha256` are deterministic
content identities. They detect accidental mismatch and enable exact linkage;
they are not signatures, MACs, or proof that an untrusted producer is genuine.
Consumers must call `verify_authorization_decision(policy, request, decision)`
against the exact trusted policy and request and must not accept an isolated
decision document as an authorization credential.

An allowed decision is a point-in-time evaluation, not a durable capability.
Before a protected operation, evaluate again against the current policy or
verify that the exact trusted policy is still authoritative. Revocation and
expiry must not be bypassed by replaying an older decision.

`SQLiteAuthorizationV3Repository` and
`PostgresAuthorizationV3Repository` persist immutable policy bundles and linked
decisions in isolated `schemas/*-v3-authorization.sql` schemas. Their
`authorize_and_record()` path evaluates before storage; `append_decision()`
requires the exact policy, request, and decision and calls
`verify_authorization_decision()` before one atomic append. Request identity is
unique, exact replay is idempotent, conflicting reevaluation is rejected, stored
descriptors are revalidated, schema drift fails closed, and nested callers use
a savepoint. PostgreSQL install and rollback are separately version-gated,
atomic resources; rollback rejects catalog drift and external dependencies.
These repositories do not authenticate the supplied context and are not yet an
active retrieval boundary.

## Registries and bindings

`AuthorizationPolicyBundle` contains:

- principals keyed by stable ID, with an issuer, hashed subject, optional tenant,
  and active/disabled status;
- agent clients keyed by stable ID, with an explicit client kind, optional
  tenant, and active/disabled status;
- canonical repositories, exactly one repository-to-tenant binding per
  repository, and explicit tenant-scoped aliases;
- unique role bindings joining one principal and one agent client to a global,
  tenant, or repository scope, an explicit permission set, a status, and a
  validity interval.

Every cross-record reference is validated when the policy is constructed.
Aliases are exact and case-sensitive. No trimming, fuzzy match, path
normalization, or provider guess is performed. `CanonicalRepository.legacy_aliases`
is migration evidence only; it is never an authorization alias. Operators must
promote an accepted alias into `repository_aliases` with an explicit tenant and
source.

Binding time is inclusive at `valid_from` and exclusive at `expires_at`.
Revoked bindings never match. `platform:admin` is an explicit global-scope
superuser permission and matches every permission; it must be assigned and
audited accordingly.

## Evaluation order

A conforming service performs authorization before retrieval:

1. obtain authenticated server-owned principal, client, and target context;
2. reject unknown or disabled identities and tenant mismatch;
3. resolve an exact canonical repository ID or registered tenant alias;
4. reject repository/tenant mismatch;
5. find active, in-window bindings for the exact principal-client pair whose
   permission and scope cover the request;
6. emit a content-derived allow or deny decision and retain its request and
   policy linkage;
7. only after an allow may retrieval and applicability filtering begin.

An authorization scope may carry the existing bounded applicability attributes
such as branch, model family, or task type. The evaluator intentionally ignores
those attributes. They narrow applicability later; they never grant access.

Repository permissions require both tenant and repository targets.
`tenant:audit_read` requires only a tenant. Global policy and platform audit
permissions forbid tenant and repository targets.

## Wire resources and limits

Canonical resources:

- `schemas/authorization_policy_v3.schema.json`
- `schemas/authorization_decision_v3.schema.json`
- `examples/authorization_policy_v3.example.json`
- `examples/authorization_decision_v3.example.json`
- `schemas/sqlite-v3-authorization.sql`
- `schemas/postgres-v3-authorization.sql`
- `schemas/postgres-v3-authorization-rollback.sql`

JSON loaders reject duplicate keys, non-finite numbers, invalid UTF-8, unknown
or missing fields, more than 1 MiB, more than 25,000 nodes, or depth above 32.
Each registry is capped at 10,000 entries and each binding at 32 unique
permissions. Python validation additionally enforces cross-record identity,
tenant, alias, scope, time, sorted decision linkage, and content-derived IDs.

## Compatibility boundary

This contract does not increment snapshot version 2, SQLite schema version 1,
or active PostgreSQL schema version 2. The authorization schemas are isolated,
opt-in schema-version-1 authorities. They add no remote service, authentication
provider, SDK transport, or runtime injection path. Active adapter integration
requires explicit migrations, authenticated server context, negative
authorization tests, and cross-adapter conformance before activation.
