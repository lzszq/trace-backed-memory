# Explicit Failure Text Classification Implementation Plan

1. Add extraction regressions showing that keywords in successful and failed
   tool names cannot select a failure taxonomy entry.
2. Preserve recognized call/output error classification and errored-tool
   symptom labels in the same focused tests.
3. Change `_trace_text()` so only explicit trace, call, and output errors enter
   classifier text.
4. Publish the Phase 69 contract in README, architecture, usage policy,
   roadmap, product documentation, and the superseded Phase 28 design.
5. Run focused and full tests, compile sources, build wheel/sdist, verify
   packaged resources, and smoke-test the installed wheel.
6. Obtain independent code, test, and documentation reviews before merging.
7. Fast-forward `main`, push, and require every GitHub Actions job to pass.
