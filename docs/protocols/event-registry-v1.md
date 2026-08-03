# Event Type Registry v1

**English** | [简体中文](event-registry-v1.zh-CN.md)

`tbm.event-registry.v1` is the storage-neutral typed-consumption boundary for
canonical `tbm.event.v1` envelopes. The canonical envelope remains appendable
and preservable even when its type or version is unknown. A reducer, projector,
or other typed consumer must resolve the event through a sealed registry and
must fail explicitly when no matching registration exists.

## Registration and sealing

Each `EventPayloadRegistration` binds one unique `(event_type, event_version)`
to its event kind, versioned payload-schema name, strict root-object JSON
Schema, and domain-separated schema hash. Root payload schemas must reject
additional properties. The supported schema subset is dependency-free and
covers strict objects, arrays, scalar types, enums, constants, `oneOf`, regular
expressions, length/cardinality constraints, uniqueness, and numeric bounds.

Duplicate type/version pairs and duplicate payload-schema names fail. A
registry is mutable only while being assembled. `seal()` freezes its
registration/upcaster topology; inspection, typed consumption, compatibility
reporting, and schema generation require a sealed non-empty registry.
Version 1 bounds the catalog to 32 event types, 32 versions per type, 2,048
upcaster edges, and 32,768 compatibility rows; the published catalog schema
uses the same limits.

Schema keyword types and their object/array/string/number contexts are checked
strictly. Property names and every upcaster result also pass the canonical
event payload bounds and forbidden secret-metadata policy, so typed evolution
cannot bypass the base envelope's safety boundary.

## Unknown events

`inspect()` returns the original immutable `CanonicalEvent` with one of:

- `known`;
- `unknown_type`;
- `unknown_version`.

Unknown events are therefore retained exactly and remain available for export,
migration, or later software. `consume()` never treats them as an empty or
generic payload: it raises stable `TBM_EVENT_REGISTRY_UNKNOWN_EVENT` and keeps
the original event on the error for controlled operator handling. This is the
required unknown-event behavior for future reducers.

## Upcasters and compatibility

An `EventPayloadUpcaster` advances exactly one version and binds an explicit
upcaster ID and producer version. Both endpoint registrations must already
exist and retain the same event kind. Multi-version conversion follows only a
complete adjacent edge chain. Every intermediate output is copied, bounded,
and revalidated against its target payload schema. Failures are sanitized; the
canonical source event is never rewritten.

The generated compatibility matrix marks every registered source/target pair
as `native`, `upcast`, or `unsupported`. Downcasts are always unsupported.
Upcaster metadata participates in the content-addressed registry catalog;
deployed code provenance remains a release/distribution responsibility.

## Deterministic artifacts

The sealed default registry publishes the canonical memory proposal plus the
typed GateSession, retrieval/System Gate evidence, Semantic Gate attempt,
finalization, outcome/attribution, ordered Trace observation, and local effect
families. The current catalog contains 31 version-1 event types, including the
Trace family described by [Ordered Trace Event v1](trace-event-v1.md) and all
nine `tbm.effect.*` events described by [Effect Event v1](effect-event-v1.md).
It deterministically generates:

- `examples/event_type_registry_v1.example.json` — content-addressed catalog;
- `schemas/event_type_registry_v1.schema.json` — catalog preflight schema;
- `schemas/event_payload_registry_v1.schema.json` — typed payload dispatch
  schema generated from registrations.

Canonical and installed copies are byte-identical packaged resources. Adding a
production event type requires its strict payload schema, compatibility
decision, focused invalid-payload tests, and regenerated resource bytes in the
same change.

This registry does not append events, authorize callers, select a ledger,
execute reducers, or make unknown events safe to consume.
