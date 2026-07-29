# Authenticated Semantic Gate service v3

**English** | [简体中文](semantic-gate-service-v3.zh-CN.md)

`AuthenticatedSemanticGateService` is the storage-neutral service boundary for
one authenticated Semantic Gate provider call. It composes existing immutable
RetrievalSnapshot/SystemGateEvaluation evidence with either the SQLite or
PostgreSQL Semantic Gate artifact authority; it does not define a new storage
schema or wire format.

## Trusted inputs

Trusted bootstrap code owns `TrustedSemanticProvider` and
`SemanticGateServiceConfiguration`. They select the provider, authenticator,
credential identifier, model/version, endpoint, prompt-template version,
generation-config digest, media type, classification, and redaction policy.
The credential identifier is non-secret metadata; credentials and tokens must
never be placed in these records.

A transport authenticator produces `AuthenticatedSemanticProviderContext`.
The service requires an exact provider/authenticator/credential match before it
loads evidence, reads the retry chain, samples time, or calls the provider.
The caller supplies only the System Gate evaluation ID, exact bounded UTF-8
prompt bytes, and the expected durable parent attempt ID.

## Invocation order

For each invocation the service:

1. authenticates the provider context against the trusted registration;
2. loads and cross-verifies the exact System Gate evaluation and retrieval
   snapshot;
3. reads and fully verifies the durable attempt chain and rejects a stale
   expected parent before provider work;
4. samples the trusted service clock immediately before and after the provider
   callback and derives bounded latency;
5. constructs the content-addressed attempt plus exact prompt/response role
   bindings from server-owned provenance, reusing an existing immutable
   descriptor when retry bytes are identical;
6. atomically appends through the configured artifact authority and requires an
   exact durable read-back.

The provider callback receives only `SemanticProviderCall`. It may return a
`SemanticProviderResult`; it cannot choose provider/model/template/config
identity, timestamps, sequence, or parent. Existing cross-record verification
still enforces that the result covers every candidate and never reopens a
System Gate block.

## Failure and retry semantics

`SemanticProviderCallError` accepts only the closed stable taxonomy
`provider_authentication_failed`, `provider_content_rejected`,
`provider_rate_limited`, `provider_response_invalid`, `provider_timeout`,
`provider_unavailable`, or `provider_error`, plus optional provider
request/token metadata. Any other provider exception is
normalized to `provider_error`; its message is neither stored nor exposed by
the service error. The service persists a prompt-only failed attempt and then
raises `SemanticProviderInvocationFailedError` with the exact durable result.

An invalid clock, missing evidence, stale parent, invalid provider result,
storage conflict, or read-back mismatch fails closed. Concurrent calls may
both reach the external provider after reading the same parent, but the
authority CAS accepts at most one append; the rejected call is not silently
retried. A retry must reload the current head and name it explicitly.

## Remaining boundary

Current artifact authorities retain only `public` or `internal` plaintext, so
the service rejects sensitive classifications until an encryption-at-rest
provider exists. Authenticator/credential identity is checked in process but
is not a signed durable attestation. This single-call service still owns no
GateSession transition; the opt-in
[`AuthenticatedSemanticGateSessionService`](durable-semantic-gate-v3.md)
composes it through `DECIDED`. Replay-manifest/finalization linkage,
retention/access-control policy, external checkpoints, and active
Agent/MCP/HTTP/SDK emission remain separate work. The active snapshot-v2,
SQLite-v1, and PostgreSQL-v2 compatibility boundaries are unchanged.
