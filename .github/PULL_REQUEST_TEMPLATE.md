## Scope

Describe the product or engineering outcome and the compatibility boundary.

## Event / projection impact

- Canonical event types added or changed: `none` / list each type and version.
- Reducers or replaceable projections added or changed: `none` / list each one.
- Replay, ordering, idempotency, and rebuild evidence: `not applicable` / links.

## Authority registry role

- Persistence modules added or changed: `none` / list each module.
- Declared role: `ledger` / `projection` / `compatibility-migration` / `bundle-coordinator`.
- Registered source of truth: `migration-asset` / `artifact-authority` / `none`.
- Authority registry updated and verified: `yes` / `not applicable`.

No persistence module may introduce an unregistered or independent source of truth.

## Compatibility / migration

- Snapshot v2, SQLite v1, and PostgreSQL v2 compatibility impact: `none` / explain.
- Migration, verifier, rollback, fixture, and cutover impact: `not applicable` / links.

## Verification

- [ ] Focused tests pass.
- [ ] `python tools/verify.py --fast` passes.
- [ ] The required full repository gate passes before merge.
- [ ] English and Simplified Chinese documentation are aligned.
