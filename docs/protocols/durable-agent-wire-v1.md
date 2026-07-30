# Durable Agent wire boundary v1

Status: opt-in adapter contract; no active HTTP, MCP, CLI, or SDK transport
selects it yet.

## Purpose

`tbm.durable-agent-wire.v1` is the strict, adapter-neutral request and response
boundary over `AuthenticatedDurableAgentMemory`. It maps the complete durable
facade without changing `tbm.agent.v1`, snapshot version 2, SQLite schema
version 1, or PostgreSQL schema version 2.

The dispatcher is not a transport authenticator. An embedding adapter must
authenticate the live caller and construct these objects outside request JSON:

- `AuthenticatedServiceContext`;
- `AuthenticatedSemanticProviderContext` for `decide`;
- `AuthenticatedOutcomeEvaluatorContext` for `complete`.

The adapter must also provide a server-owned canonical repository resolver and
trusted evaluator resolver. Startup selectors, a local bearer token, and
caller-supplied identity fields are not substitutes for transport
authentication.

Install the optional request-model dependency with:

```bash
python -m pip install -e ".[service]"
```

## Operations

The dispatcher maps:

- `prepare`;
- `decide`;
- `finalize`;
- `start`;
- `resume`;
- `abandon`;
- `complete`;
- `cancel`;
- `get_session`;
- optionally `export_replay`.

Every operation success has:

```json
{
  "protocol_version": "tbm.durable-agent-wire.v1",
  "operation": "get_session",
  "result": {}
}
```

Every public failure uses the same protocol version and a bounded error with a
stable code, category, operation, message, and retryable flag. Unknown
exceptions are sanitized as `TBM_DURABLE_WIRE_INTERNAL_ERROR`.

## Request trust boundary

Strict Pydantic request models forbid unknown fields. No request model contains
principal, AgentClient, tenant, repository, environment, authorization-event,
Semantic Gate provider authentication identity or credential, evaluator
authentication identity or credential, or server authority fields.

For preparation, the caller supplies task/retrieval facts, exact query bytes as
canonical base64, idempotency, TTL, and lease. The dispatcher builds
`RetrievalPreparationContext` from the trusted service context plus a
server-owned canonical repository ID. The existing authorization and retrieval
services still verify that resolved context against the current registry and
durable decision. Caller-supplied `retriever_id`/`retriever_version` and
semantic-query provider/version are retrieval algorithm descriptors, not
authenticated transport or Semantic Gate provider identities; managed-index
retrieval verifies those descriptors against the selected bundle.

For a Semantic Gate decision, prompt and response bytes use canonical base64.
Provider identity and credentials come only from the trusted provider context.
The dispatcher builds the provider callback and compares retained prompt and
response bytes after the durable service returns. A decided-session replay with
different submitted response bytes fails as
`TBM_DURABLE_WIRE_DECISION_REPLAY_MISMATCH`.

For completion, the operation-specific fields contain only measurement facts
and artifact hashes, in addition to the session ID and expected version shared
by state transitions. The trusted evaluator resolver supplies evaluator ID and
version from the authenticated evaluator context. The durable execution
service performs its own current-registration authentication again before
completing the session.

## Content exposure

`DurableAgentWireConfiguration` has two independent fail-closed profiles:

- `expose_injection_content=False` returns the injection descriptor and
  manifest but replaces the runtime snippet with null;
- `expose_replay_content=False` removes `export_replay` from capabilities and
  rejects the operation before replay authorization or storage reads.

Replay exposure requires injection exposure. When explicitly enabled, replay
still requires the durable facade's fresh repository-scoped `artifact:read`
decision, exact session revision, classification allowlist, byte limit, full
descriptor preflight, and unchanged-session recheck.

These switches are defense in depth, not authorization. An adapter that enables
content must already have an authenticated peer boundary appropriate for the
data classification.

## Durability and replay

The wire dispatcher stores no pending handles. Clients continue by
`session_id` plus exact GateSession version. Idempotency, expiry, lease,
cancellation, recovery state, finalization replay, completion replay, and
session-bound replay export remain owned by the durable domain services and
repositories.

This module is the common contract for future durable HTTP, MCP, CLI-daemon,
Python, and TypeScript adapters. Until one of those adapters constructs the
complete authority graph and trusted contexts, it must not be described as an
active or transport-authenticated service.
