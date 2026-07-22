# Public Snapshot Write Lock Design

**Status:** Accepted for Phase 64 implementation

## Problem

Snapshot CLI mutations already serialize the complete load, mutate, and atomic
save transaction with a persistent sibling `.tbm.lock`. Python callers can use
thread-safe Store methods and atomic file replacement, but they cannot currently
coordinate two processes that independently load the same snapshot, mutate it,
and save it. The later publisher can silently lose the earlier update.

## Public API

Add this dependency-free root-package export:

```python
snapshot_write_lock(
    snapshot_path: str | Path,
    *,
    timeout_seconds: int | float = 30.0,
) -> ContextManager[None]
```

Callers must hold the context across the entire read-modify-write transaction:

```python
with snapshot_write_lock(path):
    store = TraceBackedMemoryStore.load_json(path)
    # mutate store
    store.save_json(path)
```

The context yields `None`. It is advisory: every cooperating writer must use the
same protocol. It is not a Store `RLock`, a PostgreSQL transaction, or a lock
inside `save_json()` alone.

## Timeout Contract

Accept exact built-in integers and floats that convert to a finite,
non-negative float. Reject booleans, numeric subclasses, strings, negative
values, infinities, NaN, and overflowing integers with:

`ValueError("timeout_seconds must be a non-negative finite number")`

Validation occurs before path resolution or sidecar creation. Zero performs one
immediate acquisition attempt. Contention through the deadline raises the
existing built-in `TimeoutError` with the exact message:

`timed out waiting for snapshot write lock: <canonical-sidecar-path>`

## Lock Identity And Lifecycle

Preserve the existing CLI protocol exactly:

- expand `~`, resolve the snapshot path non-strictly, normalize platform case,
  and use sibling `<snapshot-name>.tbm.lock`;
- relative, absolute, `..`, case-normalized Windows, and resolvable symlink path
  aliases converge; hard-link aliases are not promised to converge;
- open the persistent sidecar in `a+b`, initialize an empty file with the
  non-sensitive byte `0`, and lock its first byte/inode;
- use non-blocking `fcntl.flock()` on POSIX and `msvcrt.locking()` on Windows,
  retrying recognized contention at no more than 50 ms intervals;
- release in `finally`; descriptor close or process exit releases OS ownership;
- never delete or replace the persistent sidecar inode.

Independent acquisitions are non-reentrant. A holder attempting to acquire the
same canonical lock again may wait and time out. Callers must pass an existing
lock context down rather than nesting another acquisition.

## CLI Compatibility

Move the backend into `trace_backed_memory.locking`. Keep
`cli._snapshot_write_lock`, `cli._snapshot_lock_path`, and
`cli._SNAPSHOT_LOCK_TIMEOUT_SECONDS` as private compatibility wrappers/aliases
used by current tests and integrations. The CLI wrapper passes its mutable
timeout constant into the public function, preserving monkeypatch behavior.

The CLI still acquires before snapshot load, holds through mutation,
serialization, and `save_json()`, releases before stdout, maps `OSError`
(including `TimeoutError`) to write error/exit code 4, and leaves dry-run and
read-only commands lock-free.

## Compatibility

This is an additive API and internal extraction. It changes no Store signature,
serialized byte, snapshot version 2, JSON Schema, active-lessons YAML,
PostgreSQL schema version 1, or packaged resource.

## Verification

Tests must cover root export, timeout validation before sidecar creation,
canonical alias contention, immediate timeout and recovery, exception release,
persistent placeholder content, CLI delegation and transaction ordering, and
two real Python processes whose locked load-mutate-save operations preserve both
updates. The complete suite and remote Windows/POSIX jobs must remain green.

## Phase 67 Sidecar Safety Addendum

Before the persistent placeholder byte is written, the canonical sidecar must
be a single-link regular file. Absent paths use exclusive creation. Existing
paths use no-follow metadata checks plus pre-open, descriptor, and post-open
identity comparison. Symbolic links, Windows reparse points, hard links, and
special files raise `OSError` without modifying an alias target. Descriptor and
path identity are checked again after OS acquisition and before yielding; CLI
callers retain write error/exit code 4 before snapshot load. Advisory ownership,
placeholder content, timeouts, API shape, snapshot version 2, and PostgreSQL
schema version 1 remain unchanged.
