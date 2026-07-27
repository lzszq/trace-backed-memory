# Non-negotiable invariants

- Raw Trace data is evidence, not prompt memory.
- Verified memory retains source Trace, review, fix, and regression evidence.
- Models may propose and narrow; they may not verify or activate.
- System Gate output is monotonic: later stages can only remove candidates.
- Finalization rechecks live status and scope before rendering.
- Only final allowed memory reaches the injection renderer.
- Persisted identities, provenance links, and completed outcomes are immutable.
- Obsolescence is forward-only.
- Every injection is Trace-linked and auditable through a usage decision.
- Scope matching does not provide authorization.
- Inputs are bounded, duplicate-key rejecting, and finite-number checked.
- Mutations are staged and atomic; partial documents and batches never commit.
- Canonical resources and installed copies remain byte-identical.
- Current compatibility boundary is snapshot 2, SQLite 1, PostgreSQL 2.
- Pending gate requests remain process-local until a versioned durable-session
  migration is implemented.
