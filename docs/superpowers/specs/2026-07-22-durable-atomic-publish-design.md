# Durable Atomic Publish Design

**Status:** Accepted for Phase 62 implementation

## Problem

`TraceBackedMemoryStore.save_json()` and `save_lessons_yaml()` write a sibling
temporary file, flush and `fsync()` its contents, close it, and publish it with
`os.replace()` or `os.link()`. That protects readers from partial bytes, but on
POSIX it does not make the changed directory entry durable across a power loss.
The parent directory must be synchronized after publication.

## Scope

Phase 62 changes only the shared private `_atomic_utf8_writer()` boundary.
It covers replacement snapshots, replacement lesson exports, and additive
`overwrite=False` lesson exports. Public methods, parameters, serialized bytes,
snapshot version 2, PostgreSQL schema version 1, and packaged resources remain
unchanged.

Cross-process read-modify-write locking, retrieval indexes, and general model
cardinality limits remain separate work.

## Publish Protocol

The writer performs these steps in order:

1. Create a sibling temporary file and serialize canonical LF UTF-8 text.
2. Flush and `fsync()` the temporary file, then close it.
3. Publish with `os.replace()` or, for no-replace output, `os.link()`.
4. After a successful link, remove the temporary name before directory sync
   when normal cleanup succeeds.
5. On POSIX, open the parent directory read-only with `O_DIRECTORY` when the
   flag exists, `fsync()` its descriptor, and close it in all cases.

Non-POSIX platforms retain the existing publication protocol because Python
does not expose one portable directory-`fsync` operation there.

The existing best-effort temporary cleanup remains in `finally`. A normal
successful no-replace publish removes the temporary name before synchronizing
the directory, so one directory sync persists both the target link and cleanup.

## Failure Semantics

Serialization, temporary-file flush/sync, and publication failures occur before
the destination changes and retain the existing preservation behavior.

A parent-directory open or sync failure occurs after publication. It is
propagated as the original `OSError`; the destination may already expose the new
bytes. Callers must treat this result as an indeterminate durability outcome and
inspect the destination before retrying. The implementation must not claim that
the previous destination is restored after a post-publication durability error.

The directory descriptor is closed even when `fsync()` raises. A close error is
propagated when no earlier error is active through the normal `try/finally`
behavior.

## Verification

Tests must prove:

- file sync precedes publish and parent-directory sync follows it;
- normal no-replace publication removes the temporary name before directory
  sync;
- the POSIX helper opens the expected directory flags, syncs, and always closes;
- the non-POSIX helper is a no-op;
- directory-sync errors propagate after publication without leaving a temporary
  sibling;
- JSON and lesson bytes, no-replace behavior, and earlier failure cleanup remain
  unchanged.

The complete test suite, distribution verification, and installed-wheel smoke
must remain green on Windows and the remote POSIX CI jobs.
