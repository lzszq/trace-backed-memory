# Serialized Snapshot CLI Writes Design

## Problem

Snapshot mutation commands currently load the input snapshot, mutate an
in-memory Store, and atomically replace the snapshot only after the command
succeeds. Atomic replacement prevents torn files, but it does not serialize
the full read-modify-write transaction.

Two `--write` processes can both load snapshot S, commit independent changes
to separate in-memory Stores, and publish one after the other. The second
replacement silently discards the first process's update even though both
commands report success.

## Lock Identity And Ownership

Every snapshot mutation with `--write` acquires one exclusive advisory lock
before loading the snapshot. The lock is represented by a sibling sidecar
whose name appends `.tbm.lock` to the canonical snapshot path. Relative paths,
parent-directory aliases, symlinks, and Windows path case are normalized before
the sidecar path is derived.

The sidecar is persistent and contains only one placeholder byte. It is not an
ownership sentinel: POSIX uses `fcntl.flock()` and Windows uses a one-byte
`msvcrt.locking()` region. The operating system releases ownership when the
descriptor closes or a process terminates, so a crash does not leave a stale
logical lock.

The sidecar must not be unlinked after use. Removing it can let an existing
waiter retain a lock on the unlinked inode while a new process creates and
locks a different inode at the same path. Keeping the non-sensitive sidecar
preserves one stable coordination object.

## Transaction Boundary

The exclusive lock covers:

1. `TraceBackedMemoryStore.load_json()`;
2. Store mutation and validation;
3. success-payload serialization;
4. same-path `save_json()` publication.

The lock is released before writing success JSON to stdout. A slow or closed
downstream pipe therefore cannot delay another snapshot writer after the
snapshot has committed.

Only commands that both accept `--write` and publish the input snapshot use
the lock: lessons import, outcome, obsolete, obsolete-batch, complete,
complete-batch, recover, recover-batch, and recover-ready. Dry runs and
read-only snapshot commands do not create a sidecar. Lessons export and
resource export publish different destinations and retain their existing
atomic destination behavior without taking the snapshot mutation lock.

## Platform Behavior

On POSIX, a blocking exclusive `flock` covers the lockfile descriptor. On
Windows, the lockfile is initialized to at least one byte and nonblocking
one-byte acquisition retries only lock-contention errors until ownership is
obtained. Invalid descriptors, paths, permissions, and other lock failures are
not retried.

Lock open or acquisition failure is a structured CLI write error with exit
code 4, and snapshot loading never begins. Exceptions and early command returns
still close the descriptor. Unlock is best-effort before close because close
is the final ownership-release boundary.

The lock coordinates cooperating local CLI processes that derive the same
canonical snapshot path. It does not turn advisory locks into mandatory
filesystem enforcement, and external programs that replace the snapshot
without this protocol remain outside the guarantee. Distinct hardlink entries
remain distinct publication paths, matching atomic replacement semantics.

## Compatibility

The Store API, mutation semantics, payloads, stdout/BrokenPipe behavior, exit
codes, and atomic writer remain unchanged. The sidecar contains no memory,
Trace, path, process, or user data. No dependency, snapshot field, JSON Schema,
active-lessons YAML field, packaged resource, PostgreSQL DDL, or schema version
changes. Snapshot version remains 2 and PostgreSQL schema version remains 1.

## Verification

Tests prove that the lock surrounds load through save, is released before
stdout, rejects acquisition failures before loading, releases ownership after
exceptions, serializes contenders, and is absent for dry-run and read-only
commands. Existing write-failure, atomic publication, and BrokenPipe tests
continue to protect their prior behavior on both Linux and Windows CI.
