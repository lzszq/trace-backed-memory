# Memory Run Execution Implementation Plan

## Goal

Provide one deep synchronous interface for the common
prepare/decide/finalize/execute/complete path while retaining the Store as the
only domain-state owner.

## Contract Tests

- Add `tests/test_execution.py` with real Store fixtures and fixed callbacks.
- Cover call order, public callback inputs, full measurement forwarding, and
  Store-produced linkage.
- Cover all four post-preparation failure phases and recoverable context.
- Cover wrong callback return types, raw preparation validation, retained Store
  causes, and atomic completion failure.
- Lock public exports, docs, and persistence compatibility.

## Implementation

- Add frozen `MemoryRunMeasurement` to the public model records.
- Add `execution.py` with callable aliases, `MemoryRunExecutionError`, and
  `run_memory_execution()`.
- Forward only non-`None` optional measurement fields to
  `complete_memory_run()`.
- Export the new interface from the package root without changing Store
  internals or persistence adapters.

## Documentation

- Add a concise callback-based runtime example to README.
- Update architecture, usage policy, product overview, repository layout, and
  roadmap Phase 26.
- State post-preparation failure and explicit recovery responsibilities.

## Verification

- Run focused execution, README, export, and schema tests.
- Run the complete suite and source compilation.
- Build wheel/sdist and smoke-test the installed public interface.
- Review diff, secret patterns, conflict markers, schema hashes, worktree
  cleanliness, remote synchronization, and CI before merge and push.
