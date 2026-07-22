# Snapshot Lock Sidecar Safety Design

**Status:** Accepted for Phase 67 implementation

## Problem

`snapshot_write_lock()` currently opens the persistent sibling `.tbm.lock`
with `a+b`. That operation follows an existing symbolic link. The empty-file
initializer can therefore write the placeholder byte to an unrelated target,
and the advisory lock can be redirected away from the canonical sidecar.
Hard-linked sidecars have the same write-redirection problem.

The lock remains advisory, so an actor that can continuously unlink or replace
files can still refuse cooperation. A stable, pre-existing filesystem alias
must not, however, redirect a cooperating writer's file mutation or ownership.

## Safety Contract

The canonical sidecar must be a single-link regular file at acquisition time.
Reject symbolic links, Windows reparse points, hard links, directories, FIFOs,
devices, and sockets with `OSError` before placeholder initialization or lock
acquisition. The CLI continues mapping that error to a write failure with exit
code 4 before snapshot load.

Creation and existing-file opening use separate paths:

- create an absent sidecar with an exclusive create, then verify the opened
  descriptor still names a single-link regular file at the canonical path;
- for an existing sidecar, inspect it without following links, reject unsafe
  metadata, open read/write with no-follow where the platform provides it, and
  compare pre-open, descriptor, and post-open identities before any write;
- close the descriptor on every validation or wrapping failure;
- retain the persistent inode after successful use.

The identity checks protect against stable aliases and ordinary replacement
races without adding a platform dependency. They do not make an advisory
protocol mandatory or defend against a privileged actor continuously deleting
the canonical sidecar while ownership is held.

## Compatibility

Keep the public signature, finite non-negative timeout validation, immediate
zero-timeout attempt, canonical snapshot alias normalization, placeholder byte,
POSIX `flock`, Windows one-byte locking, retry interval, release behavior, and
non-reentrant semantics unchanged. Legitimate sidecars created by earlier
versions remain valid. Sidecar links and special files become explicit errors.

This phase changes no Store or CLI command signature, successful payload,
serialized snapshot byte, snapshot version 2, JSON Schema, active-lessons YAML,
packaged resource, PostgreSQL DDL, or PostgreSQL schema version 1.

## Verification

Tests must prove that symbolic-link and hard-linked sidecars are rejected
without modifying their targets, normal creation and reuse still preserve the
single `0` byte, the CLI rejects an unsafe sidecar before snapshot loading and
reports exit code 4, contention and exception release remain intact, and the
complete Windows/POSIX plus package suites stay green.
