# Authenticated retrieval service boundary

**English** | [简体中文](authenticated-service-v3.zh-CN.md)

`AuthenticatedRetrievalService` is the first active orchestration boundary
that applies the version-3 entity registry and authorization contracts before
calling retrieval. It is storage-neutral and accepts either the SQLite or
PostgreSQL authorization authority through `AuthorizationDecisionWriter`.

## Trust boundary

`AuthenticatedServiceContext` must be constructed by trusted service code
after transport authentication. It contains exact `PrincipalIdentity` and
`AgentClientIdentity` records plus the server-owned tenant, repository
reference, and environment. This module does not validate OAuth tokens,
signatures, operating-system credentials, or caller JSON, and the context
object is not a reusable capability.

For every protected retrieval, the service:

1. loads the current immutable `EntityRegistrySnapshot`;
2. requires authenticated principal/client records to match the current
   registry byte-for-byte when those identities exist;
3. creates a `memory:retrieve` request with a server clock and request-ID
   factory;
4. evaluates the exact current authorization policy;
5. appends the allow or deny decision and reads the exact decision back from
   the authority;
6. stops on denial or persistence failure;
7. reloads the registry and rejects any content-hash change during
   authorization;
8. requires an active environment bound to the same tenant and canonical
   repository; and
9. only then invokes the retrieval callback with `AuthorizedRetrievalScope`.

The returned scope carries the durable `authorization_event_id`, canonical
repository ID, environment ID, principal, client, and tenant. Stable service
errors sanitize registry, persistence, clock, request-factory, and retrieval
callback failures.

## Current integration boundary

`AuthenticatedLocalAgentMemory` is the opt-in local application integration.
It wraps one exact `LocalAgentMemory` instance and runs `prepare` through this
authorization boundary. Its `AuthenticatedAgentPrepareContext` intentionally
has no principal, client, tenant, repository, or environment fields. The
caller-facing Trace still has legacy `repo` and `tenant` fields, but this
facade ignores and overwrites both. After authorization, it binds the
canonical tenant and repository from `AuthorizedRetrievalScope` into both the
Trace and `MemoryContext`; denial or authorization-persistence failure occurs
before the Trace is registered. Private ownership indexes bind request and
decision handles to the facade that prepared them, so another authenticated
facade cannot finalize, complete, or cancel them even if both facades share a
runtime. These indexes remain process-local and are not durable sessions.

This opt-in facade does not authenticate a transport and is not yet selected
by `tbm-mcp`, CLI, HTTP, or an SDK. Trusted bootstrap code must still derive
the fixed `AuthenticatedServiceContext`; request JSON must never supply its
identity IDs. Durable GateSession, RetrievalSnapshot, audit actor linkage,
expiry/recovery workers, and one atomic cross-record service transaction
remain separate delivery steps.
