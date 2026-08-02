# Finalization Event v1

**English** | [简体中文](finalization-event-v1.zh-CN.md)

`tbm.finalization-event.v1` is the canonical-event contract for the F2 final
decision and rendered-injection cutover increment. It links the existing
`tbm.usage_decision.finalized` GateSession transition to one
`tbm.injection.rendered` domain event. The transition event is the required
causation parent; the injection event cannot be created from identifiers or a
replay projection alone.

Each replay manifest owns one deterministic version-1 `finalization` stream.
The two events must retain the same trusted organization, tenant, repository,
environment, principal, agent-client, actor, and transition authorization
scope. Trusted context comes from the adapter and cannot be selected by request
JSON.

## Payload and Artifact references

The bounded payload retains the complete `UsageDecision` and
`InjectionArtifact` metadata, the replay-manifest digest, the fixed artifact
role mapping, and the finalized transition event ID. It never embeds snippet,
prompt, response, policy, retrieval, or renderer bytes.

Sorted, unique descriptor-only `EventArtifactRef` values cover the
UsageDecision artifact, all seven replay components, and the exact injection
artifact. Each reference binds its content digest, media type, byte size,
classification, retention policy, encryption-key metadata when required, and
availability. The role set, descriptors, UsageDecision, injection, and rebuilt
manifest must agree exactly.

## Event-first persistence

Explicit durable SQLite and PostgreSQL runtimes enable
`store_complete_finalization()`. One outer transaction performs:

```text
read exact DECIDED GateSession event head
→ append/read back tbm.usage_decision.finalized and publish FINALIZED
→ append/read back tbm.injection.rendered
→ write replay Artifact/injection/manifest projections
→ exact projection and event read-back
```

An event, transition, replay-projection, or read-back failure rolls back both
new events, the GateSession revision/head, and all replay rows. A committed
lease renewal from the earlier claim remains committed. PostgreSQL acquires the
event-ledger schema/global lock before replay schema validation and then the
GateSession transition/session-head locks. Caller transactions remain owned by
the caller.

Compatibility callers continue to use `store_complete_bundle()` followed by
the existing GateSession CAS. Event-first behavior requires explicit enablement
and a trusted bound event context.

## Reducer and parity

`final-decision-injection` version 1 consumes only
`tbm.usage_decision.finalized` and `tbm.injection.rendered`. It rebuilds the
`final_decision_injection_v1` projection with exact finalized-session,
UsageDecision, injection, replay-manifest, Artifact-role, authorization,
causation, and event-head linkage. Missing parents, duplicated decisions or
injections, scope drift, stale authorization, and incomplete Artifact sets fail
closed.

Parity verification compares the rebuilt projection with the transitional
GateSession and replay authorities. The generic reducer runtime is still not
the sole active rebuild path. Outcome/attribution and local completion-effect
reducers now cover delivery history and dead-letter parity. Provider receipts,
unknown-result reconciliation, durable compensation, complete transport
conformance, and the F2 crash matrix remain open; the product therefore keeps
`persistence_model="authority_graph"` and `full_persistence=false`.
