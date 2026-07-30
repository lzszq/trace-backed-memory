# ADR-0003: transport identity ownership

**Status:** Accepted
**Date:** 2026-07-30
**简体中文:** [0003-transport-identity-ownership.zh-CN.md](0003-transport-identity-ownership.zh-CN.md)

## Context

The durable lifecycle requires authenticated caller, provider, evaluator,
repository, tenant, client, and environment identities. Accepting any of these
from operation JSON would let an untrusted caller choose the authorization
scope. Conversely, the durable wire dispatcher is transport-neutral and cannot
authenticate a peer itself.

## Decision

- Authentication occurs before route/tool selection and before request-body
  parsing.
- A trusted transport adapter derives immutable service contexts outside
  operation JSON. Unknown identity fields are rejected by strict schemas.
- Authorization is evaluated and durably read back for the exact operation,
  repository, tenant, environment, principal, and client.
- Provider/evaluator credentials never enter request JSON, response bodies,
  exception text, or logs.
- Local STDIO may use trusted startup configuration but must not claim peer
  authentication or shared multi-tenant isolation.
- Loopback HTTP may use a process bearer secret. Non-loopback HTTP requires
  TLS and a trusted authenticator; production OIDC/mTLS can be supplied by a
  gateway or adapter without changing gate policy.
- Transport code never implements retrieval, Gate, rendering, replay, or
  publication policy.

## Consequences

Identity injection tests, duplicate-header tests, credential sanitization,
revocation/recheck tests, and authorization-before-retrieval tests are release
requirements for every durable transport.

## Exit evidence

All supported transports derive the same authenticated contexts, reject
caller-selected identity, and pass negative tenant/repository/environment and
provider/evaluator isolation tests.
