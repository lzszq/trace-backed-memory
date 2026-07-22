# Git Metadata Output Validation Design

**Status:** Accepted for Phase 68 implementation

## Problem

`capture_trace_metadata()` trusts every injected `CommandRunner` result to be
a string and strips it outside the command error wrapper. A blank `HEAD` result
therefore creates `TraceMetadata(commit_sha="")`, which fails only when a later
Store operation validates a Trace. A non-string output leaks `AttributeError`
instead of the documented `TraceMetadataCaptureError`. Oversized injected
commit, branch, or repository-name values can likewise outlive capture before
the Store's existing metadata limit rejects them.

## Output Contract

Keep the same four commands, order, runner signature, and `repo_path` argument.
Every runner result must be a string. Reject other result types at the
command boundary with `TraceMetadataCaptureError` and do not include the
returned value in the error.

Normalize string output with `strip()` and apply command-specific rules:

- `git rev-parse HEAD` rejects a blank commit SHA and accepts at most the existing
  512-character metadata limit;
- `git rev-parse --show-toplevel` rejects a blank repository root; its
  final basename is either `None` for a filesystem-root repository or at most
  512 characters;
- `git branch --show-current` may be blank for detached HEAD, otherwise it is
  at most 512 characters;
- `git status --porcelain` may be blank or whitespace for a clean tree and
  retains the existing dirty-state interpretation.

Output violations use the existing command-and-location error prefix. They do
not echo malformed output, start additional commands, or defer failure into
Trace construction or Store mutation.

## Compatibility

The default bounded Git runner already returns strings, successful Git outputs
fit these constraints, and real command failures keep their existing wrapping.
Injected runners conforming to `CommandRunner` are unchanged. Detached HEAD,
clean status, command order, status first-byte optimization, default timeout,
and stdout/stderr byte limits remain unchanged.

This phase changes no public signature, dataclass, Trace field, serialized
snapshot byte, JSON Schema, active-lessons YAML, packaged resource, PostgreSQL
DDL, snapshot version 2, or PostgreSQL schema version 1.

## Verification

Tests must cover blank required outputs, non-string results for each command,
metadata-length boundaries and overflow, detached/clean optional outputs,
filesystem-root repository normalization, command short-circuiting, and error
redaction. Existing real-Git, bounded-process, README, package, Windows, and
complete-suite tests remain green.
