# Bounded Git Capture Plan

## Scope

Bound time, stdin, encoding, and retained output for the built-in Git metadata
and ancestry runners without changing injected runners or capture semantics.

## Steps

1. Replace default-runner tests with fake-`Popen` RED tests for binary pipes,
   `DEVNULL`, environment, UTF-8 replacement, timeout cleanup, and output caps.
2. Add a large-status test proving only dirty presence is retained and a real
   Git clean/untracked smoke test.
3. Implement one cross-platform threaded bounded process runner and a
   status-presence adapter while preserving public wrappers and call order.
4. Update README, architecture, usage policy, product status, roadmap, and
   executable documentation contracts for Phase 61.
5. Run focused and full tests, build and verify distributions, obtain
   independent review, merge to main, push, and require every CI job to pass.

## Compatibility

- Keep injected runner signatures, exact Git arguments and order, ancestry
  0/1 semantics, errors, and CLI state mapping.
- Keep `GIT_NO_LAZY_FETCH=1`, option termination, and the 1,000-anchor input
  boundary.
- Keep snapshot version 2, PostgreSQL schema version 1, and all packaged
  resources unchanged.
