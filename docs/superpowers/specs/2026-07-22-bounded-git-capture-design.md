# Bounded Git Capture Design

## Problem

The default trace-metadata and commit-ancestry runners use
`subprocess.run(..., capture_output=True, text=True)` without a timeout or
stdin isolation. A Git command, filesystem monitor, or helper can therefore
wait indefinitely. Captured stdout and stderr are accumulated without a byte
limit; `git status --porcelain` is especially risky because its output grows
with the number and length of changed paths even though the caller only needs
one dirty-state bit.

The public injected-runner interfaces are useful deterministic boundaries and
must remain unchanged. This phase hardens only the built-in runners.

## Bounded Process Runner

Replace the two default `subprocess.run()` paths with one private binary
`Popen` runner. Every process uses:

- `stdin=subprocess.DEVNULL`;
- `stdout=subprocess.PIPE` and `stderr=subprocess.PIPE`;
- a 30-second wall-clock timeout;
- explicit UTF-8 decoding with replacement for malformed bytes;
- two daemon reader threads so stdout and stderr are drained concurrently on
  Windows and POSIX.

Each reader consumes fixed 8 KiB chunks and retains at most its configured
byte limit. Ordinary metadata and ancestry commands retain at most 64 KiB for
each stream. If either stream exceeds that limit, the process is killed and
reaped and the default runner raises an output-limit error. The public capture
boundary wraps it in the existing `TraceMetadataCaptureError` or
`CommitAncestryCaptureError` with the existing command and repository context.

The main thread polls process completion and reader overflow until the
deadline. Timeout or overflow kills the process and waits for bounded cleanup;
normal completion joins both readers before inspecting output. Reader buffers
never grow beyond the configured limit, plus one fixed read chunk held by the
thread. Pipes are closed after readers finish.

## Dirty-State Capture

Keep the exact `git status --porcelain` command and its existing semantics.
For the default runner only, retain the first stdout byte and continue draining
and discarding all remaining stdout until Git exits or times out. Any retained
byte returns an internal non-whitespace marker, so a tracked modification whose
porcelain record begins with a space is still dirty. Clean output remains
empty. Stderr retains the ordinary 64 KiB error boundary.

Continuing to drain avoids unbounded memory without terminating a read-only
status command early. It also preserves tracked, staged, untracked, rename,
submodule, repository-config, and command-order behavior. Injected runners
still receive the same four commands and their returned string continues to
drive `bool(status_output.strip())`.

## Ancestry And Error Semantics

`GIT_NO_LAZY_FETCH=1`, the `--` revision terminator, sorted/deduplicated anchor
order, 0/1 result meaning, the 1,000-input preflight, and no-Git overflow
behavior remain unchanged. Nonzero ancestry results other than 1 still become
`CalledProcessError` before the public wrapper.

Timeouts, output overflows, process-start failures, read failures, and cleanup
failures are command failures. They retain the existing public exception
classes and CLI state-error exit code 3. Small stderr details remain visible;
malformed bytes are represented deterministically rather than causing a locale
decode failure.

## Compatibility

`CommandRunner` and `AncestryRunner` remain two-argument callables. Public
signatures, injected call arity and order, command arguments, return models,
error prefixes, dependencies, snapshots, JSON Schemas, active-lessons YAML,
packaged resources, PostgreSQL DDL, snapshot version 2, and PostgreSQL schema
version 1 do not change.

## Verification

Deterministic fake-`Popen` tests assert binary pipes, `DEVNULL`, inherited
environment preservation, lazy-fetch override, UTF-8 replacement, normal
return codes, timeout kill/reap, stdout/stderr overflow wrapping, and status
first-byte retention while a large remainder is discarded. A real Git smoke
test covers clean and untracked worktrees. Existing injected-runner, ancestry
DAG, CLI state-error, README, full-suite, distribution, and Windows CI tests
protect compatibility.
