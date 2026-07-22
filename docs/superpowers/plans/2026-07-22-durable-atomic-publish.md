# Durable Atomic Publish Implementation Plan

1. Add failing Store tests for post-publication directory sync ordering,
   no-replace cleanup ordering, POSIX descriptor cleanup, non-POSIX no-op, and
   post-publication failure semantics.
2. Add one private parent-directory sync helper and invoke it from the shared
   atomic writer after successful publication and normal temporary cleanup.
3. Run focused Store tests, then update README, architecture, usage policy,
   product status, roadmap, and executable documentation contracts.
4. Run the full suite, build wheel and source distribution, verify packaged
   resources, and smoke-test the installed wheel.
5. Review the complete diff, merge the phase branch into `main`, push, and wait
   for every remote CI job before cleanup.
