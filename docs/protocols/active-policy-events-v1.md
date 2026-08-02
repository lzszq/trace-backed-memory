# Active policy events v1

`tbm.active-policy-event.v1` is the opt-in event/reducer boundary for the
active retrieval policy selected in one organization/tenant/repository/
environment ledger partition. It does not change the compatibility Store or
select a default Agent, MCP, HTTP, or durable runtime profile.

## Content-addressed policy bundle

`ActivePolicyBundle` binds all eight F4-05 dimensions:

- a minimum trust tier, mandatory active revision, and permanent exclusion of
  `legacy_unstructured` evidence;
- the exact five task-mode memory rules from `RetrievalPolicyBundle`;
- required/disabled ancestry with an explicit bypass reason;
- the allowed classification set;
- mandatory eval-leakage blocking;
- monotonically narrowing discovery, System Gate, Semantic Gate, injection,
  and payload candidate budgets within kernel hard caps;
- a content-addressed renderer descriptor with modes, per-item limits, memory
  count, character and UTF-8 byte limits, output format, and media type;
- `semantic_gate_required=true`.

The bundle embeds the exact existing content-addressed
`RetrievalPolicyBundle`; task-mode, ancestry, classification, fusion, score,
payload, and eval-leakage validation are not reimplemented by the reducer.
Canonical bounded JSON, duplicate-key rejection, finite numbers, exact fields,
and content-derived IDs fail closed.

The trust tier is a policy declaration for the event-first catalog. Candidate
trust-tier production/enforcement remains blocked on the unfinished
F4-03/F4-04 acceptance and later default cutover; confidence values and
attestation-verifier IDs are not silently treated as trust tiers.

## Registration, activation, and active head

One ledger partition has one `active_policy_<partition-sha256>` stream:

- `tbm.policy.bundle_registered` stores the exact bundle plus a verified
  `policy:create_global` policy/request/decision and verifier identity;
- `tbm.policy.bundle_activated` stores an independently performed
  `policy:approve_global` decision, exact registration, predecessor bundle,
  actor, client, target partition, and activation time.

Event envelopes must bind the full partition, principal/actor, client,
authorization event, occurrence time, record JSON, and record digest. The
reducer requires trusted attestation verifiers, registrar/activator
independence, a known immutable registration, the exact current predecessor,
and strictly forward activation time. Prior bundles and activations remain
immutable when the head advances.

`ActivePolicyHead` is content-addressed and retains bundle/retrieval/renderer,
registration/activation, both authorization events, both attestation
verifiers, actor, time, predecessor, and exact source-event hashes.

## Durable use

`append_active_policy_records()` pre-reduces, atomically appends through the
shared `EventLedgerPort`, verifies the receipt, rereads, verifies the stream,
and requires the durable projection to equal the predicted projection.
`rebuild_active_policy_from_ledger()` reads the single partition stream twice,
requires an unchanged verified head, and returns a content-addressed snapshot
that binds the reducer descriptor and trusted-verifier configuration. The same
code has SQLite and PostgreSQL conformance coverage.

`ActivePolicyProjection` and `DurableActivePolicySnapshot` are callable exact
`RetrievalPolicyBundle` providers, so an explicit durable composition can use
the event-derived head without changing the existing preparation/finalization
kernel. The full `ActivePolicyBundle` remains available for later renderer,
trust-tier, and Semantic Gate enforcement wiring.

## Open boundaries

- No default adapter selects this source; F5 owns default cutover.
- Current finalization still uses its fixed renderer descriptor and mandatory
  Semantic Gate path. This bundle records the governed source but does not
  claim default enforcement.
- F4-03/F4-04 acceptance blockers, F4-06 indexes, and F4-07 outcome/harm
  projection remain open; `full_persistence=false` remains correct.

Canonical resources:

- `schemas/active_policy_bundle_v1.schema.json`
- `schemas/active_policy_event_payload_registry_v1.schema.json`
- `examples/active_policy_bundle_v1.example.json`
- `examples/active_policy_event_type_registry_v1.example.json`

See also the [Chinese reference](active-policy-events-v1.zh-CN.md).
